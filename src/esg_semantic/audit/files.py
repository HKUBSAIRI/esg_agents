"""Read-only input adapter; publish four files as one versioned directory."""
from collections import Counter
from pathlib import Path
import hashlib
import os
import re
import shutil
import uuid

from .common import RESOURCE_DIR, canonical, mapping, read_json, utcnow, problem
from .indexes import InputError
from .events import capture_events
from .integration import current, validate_as
from .review_items import build_review_items

REQUIRED = ("records.jsonl", "evidence.jsonl", "semantic_filter.jsonl", "document_context.json")
OPTIONAL = ("filter_audit_sample.jsonl", "coverage.json")
OUTPUTS = ("audit_findings.jsonl", "audit_results.jsonl", "review_items.jsonl", "audit_report.json")


def file_sha(path):
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def load_file(path, *, jsonl=False):
    if path.stat().st_size > 32 * 1024 * 1024:
        raise InputError("Input file exceeds 32 MB")
    text = path.read_text(encoding="utf-8-sig")
    if jsonl:
        rows = []
        for line in text.splitlines():
            if line.strip():
                value = read_json(line)
                if not isinstance(value, dict):
                    raise InputError("JSONL lines must be objects")
                rows.append(value)
        return rows
    return read_json(text)


def _safe_root(pid_dir):
    root = Path(pid_dir).resolve()
    if not root.is_dir():
        raise InputError("Report directory missing", "ERR_AUDIT_INPUT_MISSING")
    for name in REQUIRED:
        if not (root / name).is_file():
            raise InputError("Missing required input: " + name, "ERR_AUDIT_INPUT_MISSING")
    return root


def _write_json(path, value, *, lines=False):
    content = "\n".join(canonical(row) for row in value) if lines else canonical(value)
    with path.open("xb") as target:
        target.write((content + "\n" if content else "").encode("utf-8"))
        target.flush(); os.fsync(target.fileno())


def run_files(pid_dir, *, policy_path, llm, run_id=None, dry_run=False):
    from ..auditor import audit_classification
    root = _safe_root(pid_dir)
    run_id = run_id or "ar_" + uuid.uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", run_id):
        raise InputError("Unsafe audit run ID")
    final = root / "audit_runs" / run_id
    if final.exists():
        raise InputError("Run already exists; outputs are immutable")
    if dry_run:
        return {"dry_run": True, "inputs": [str(root / n) for n in REQUIRED + OPTIONAL if (root / n).exists()],
                "planned_outputs": [str(final / n) for n in OUTPUTS]}
    if not current().fixture and not callable(current().validator):
        raise InputError("A public contracts have not been bound", "ERR_CONTRACT_NOT_READY")
    paths = [root / n for n in REQUIRED + OPTIONAL if (root / n).exists()]
    paths += [Path(policy_path).resolve()] + list(RESOURCE_DIR.glob("*"))
    paths = [p for p in paths if p.is_file()]
    before = {str(p): file_sha(p) for p in paths}
    inputs = {name: load_file(root / name, jsonl=name.endswith(".jsonl")) if (root / name).exists() else None for name in REQUIRED + OPTIONAL}
    document = inputs["document_context.json"]
    if not isinstance(document, dict):
        raise InputError("Document context must be an object")
    document = dict(document)
    if document.get("pdf_path"):
        pdf = Path(document["pdf_path"])
        document["pdf_path"] = str((root / pdf).resolve() if not pdf.is_absolute() else pdf.resolve())
        if Path(document["pdf_path"]).is_file():
            before[document["pdf_path"]] = file_sha(Path(document["pdf_path"]))
    policy = load_file(Path(policy_path))
    with capture_events() as captured:
        outcome = mapping(audit_classification(inputs["records.jsonl"], inputs["evidence.jsonl"], inputs["semantic_filter.jsonl"],
                                             inputs["filter_audit_sample.jsonl"] or [], inputs["coverage.json"], document,
                                             policy=policy, run={"audit_run_id": run_id}, llm=llm))
    changed = [p for p, fingerprint in before.items() if not Path(p).is_file() or file_sha(Path(p)) != fingerprint]
    if changed:
        outcome["status"] = "FAILED"
        outcome["problems"].append(problem("ERR_AUDIT_MUTATED_UPSTREAM", "实际输入文件或资源指纹发生变化，禁止成功发布。", level="error"))
        for result in outcome["record_results"]:
            result["review_status"] = "INSUFFICIENT"
    evidence_by_id = {e["evidence_version_id"]: e for e in inputs["evidence.jsonl"]}
    review_items = build_review_items(outcome["findings"], outcome["record_results"], inputs["records.jsonl"], evidence_by_id)
    validate_as("AuditOutcome", outcome)
    for item in review_items: validate_as("ReviewItem", item)
    final.parent.mkdir(parents=True, exist_ok=True)
    temporary = final.parent / ("." + run_id + ".tmp-" + uuid.uuid4().hex)
    temporary.mkdir()
    try:
        _write_json(temporary / OUTPUTS[0], outcome["findings"], lines=True)
        _write_json(temporary / OUTPUTS[1], outcome["record_results"], lines=True)
        _write_json(temporary / OUTPUTS[2], review_items, lines=True)
        _write_json(temporary / OUTPUTS[3], outcome)
        temporary.rename(final)
    finally:
        if temporary.exists():
            # Only this invocation's staging directory, checked against its exact parent.
            resolved = temporary.resolve()
            if resolved.parent == final.parent.resolve() and resolved.name.startswith("." + run_id + ".tmp-"):
                shutil.rmtree(resolved)
    # Operational logs are separate from the four public outputs. No prompts, values or secrets.
    pid = str(document.get("pid") or "unknown")
    safe_pid = re.sub(r"[^A-Za-z0-9_-]", "_", pid)[:80] or "unknown"
    if safe_pid != pid:
        safe_pid += "_" + hashlib.sha256(pid.encode()).hexdigest()[:8]
    log_root = root / "logs" / "audit" / safe_pid
    log_root.mkdir(parents=True, exist_ok=True)
    events = captured or [{"ts": utcnow(), "level": "INFO", "event": "audit_start", "stage": "audit", "audit_run_id": run_id,
               "pid": document.get("pid"), "message": "Audit invocation", "context": {"fixture": current().fixture,
               "contract_fingerprint": current().contract_fingerprint}}]
    for finding in outcome["findings"]:
        events.append({"ts": finding["created_at"], "level": "INFO", "event": "audit_finding", "stage": "audit", "audit_run_id": run_id,
                       "pid": document.get("pid"), "record_id": finding["record_id"], "code": finding["code"],
                       "message": "Finding produced", "context": {"severity": finding["severity"], "model_call_id": finding["model_call_id"]}})
    for item in outcome["problems"]:
        events.append({"ts": utcnow(), "level": item["level"].upper(), "event": "audit_problem", "stage": "audit", "audit_run_id": run_id,
                       "pid": document.get("pid"), "error_code": item["error_code"], "message": "See structured audit report", "context": {}})
    events.append({"ts": utcnow(), "level": "INFO", "event": "audit_end", "stage": "audit", "audit_run_id": run_id,
                   "pid": document.get("pid"), "message": outcome["status"], "context": {"input_sha256": before}})
    _write_json(log_root / (run_id + ".jsonl"), events, lines=True)
    return outcome
