"""Explicit, provisional adapter for A's delivered water_v1 files.

This is a B-private compatibility view, not a replacement A public contract.
Original IDs, logical keys, values, units, and upstream bytes remain unchanged.
"""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import uuid

from jsonschema import Draft202012Validator

from .common import RESOURCE_DIR, canonical, read_json, sha, utcnow
from .files import load_file, file_sha, REQUIRED, OPTIONAL, OUTPUTS
from .integration import integration_context

VERSION = "A-DELIVERY-WATER-V1-B1"
SOURCE_FILES = REQUIRED + OPTIONAL + ("identity.json", "adapt_report.json", "filter_report.json", "classification_report.json", "classification_outcome.jsonl", "domain_export.jsonl")
FIELDS = {
    "revenue_million": ("营业收入金额；币种须另行对应，不把收入强度当收入。", "million"),
    "revenue_currency": ("营业收入币种，文本属性，不是数值指标。", None),
    "water_withdrawal_m3": ("实际取水总量；区分耗水、供水、处理能力、强度和未来目标。", "m3"),
    "water_consumption_m3": ("实际耗水量；注意water use与取水或消耗的口径歧义，不能仅凭名称判断。", "m3"),
    "water_recycling_rate_pct": ("水回用比例；必须明确分子分母，不混用其他回收率。", "%"),
    "water_recycled_reused_m3": ("实际回用或循环利用水量；区分比例与处理能力。", "m3"),
    "water_discharge_m3": ("实际排水或废水排放体积；区分处理量、供水量、设计能力和排放去向。", "m3"),
    "cod_discharge_t": ("COD排放质量，区分浓度、去除量与排放量。", "t"),
    "ammonia_n_discharge_t": ("氨氮排放质量，区分总氮、浓度及去除量。", "t"),
    "total_n_discharge_t": ("总氮排放质量，区分氨氮和浓度。", "t"),
    "total_p_discharge_t": ("总磷排放质量，区分浓度与去除量。", "t"),
}
KEY_FIELDS = ("water_withdrawal_m3", "water_consumption_m3", "water_discharge_m3")


def iid(field):
    return "water_v1:" + (field or "UNMAPPED")


def catalogue():
    return {iid(field): {"definition": definition + " 本定义为B本地适配口径，尚未验收A正式profile。", "variables": [field], "expected_unit": unit}
            for field, (definition, unit) in FIELDS.items()}


def schema(required, properties=None):
    return {"type": "object", "required": required, "properties": properties or {}}


class DeliveryValidator:
    """B local minimum-shape/integrity validation. Never called an A Schema."""
    def __init__(self):
        string = {"type": "string", "minLength": 1}
        self.schemas = {
            "DocumentContext": schema(["pid", "report_year", "page_count", "pdf_path"], {"pid": string}),
            "EvidenceRecord": schema(["evidence_version_id", "content", "page_no", "source_format"], {"evidence_version_id": string, "content": {"type": "string"}, "page_no": {"type": "integer", "minimum": 1}, "source_format": {"enum": ["v1_numeric_blocks", "v2"]}}),
            "FilterDecision": schema(["evidence_version_id", "status", "policy_version"], {"evidence_version_id": string, "status": {"enum": ["ACCEPT", "REVIEW", "EXCLUDE"]}}),
            "FilterAuditSample": schema(["evidence_version_id", "status"], {"evidence_version_id": string}),
            "CoverageSignal": schema(["by_indicator"], {"by_indicator": {"type": "object"}}),
            "ClassificationRecord": schema(["record_id", "pid", "indicator_id", "variable", "profile_id", "profile_field", "logical_group_key"], {"record_id": string, "pid": string, "profile_id": {"const": "water_v1"}, "profile_field": {"enum": [*FIELDS, None]}, "indicator_id": string, "variable": string}),
            "AuditFinding": schema(["finding_id", "record_id", "evidence_version_ids", "code", "severity"], {"finding_id": string, "evidence_version_ids": {"type": "array", "minItems": 1, "items": string}, "severity": {"enum": ["BLOCK", "WARN", "INFO"]}}),
            "AuditRecordResult": schema(["record_id", "review_status", "finding_ids"], {"record_id": string, "review_status": {"enum": ["CONFIRMED", "REJECTED", "SUSPECT", "INSUFFICIENT", "NOT_REVIEWED"]}}),
            "AuditOutcome": schema(["pid", "audit_run_id", "record_results", "findings", "status", "validation_scope", "execution_mode"], {"validation_scope": {"const": "local_delivery"}, "execution_mode": {"enum": ["model", "rules_only"]}}),
            "ReviewItem": schema(["finding_id", "item_id", "record_id", "evidence_version_ids"]),
        }

    def __call__(self, name, value):
        if name not in self.schemas:
            raise ValueError("Unknown local validation target")
        Draft202012Validator(self.schemas[name]).validate(value)

    @property
    def fingerprint(self):
        return "LOCAL-NOT-A-FORMAL:" + sha({"version": VERSION, "schemas": self.schemas, "catalogue": catalogue()})


def _unique(rows, key):
    result = {}
    for row in rows:
        value = row.get(key)
        if not isinstance(value, str) or not value or value in result:
            raise ValueError("Missing/duplicate identity: " + key)
        result[value] = row
    return result


def adapt(data):
    """Pure transformation; original A record IDs/keys/value semantics are retained."""
    records, evidence, decisions = (deepcopy(data[n]) for n in ("records.jsonl", "evidence.jsonl", "semantic_filter.jsonl"))
    document = deepcopy(data["document_context.json"])
    identity = data.get("identity.json") or {}
    source_context = deepcopy(document)
    if document.get("company_name") in (None, "", "UNKNOWN") and identity.get("company_name"):
        document["company_name"] = identity["company_name"]
        document["company_name_source"] = "DELIVERY_IDENTITY_VIEW"
    pid = document["pid"]
    index = _unique(evidence, "evidence_version_id")
    _unique(records, "record_id")
    filters = _unique(decisions, "evidence_version_id")
    if set(index) != set(filters):
        raise ValueError("Evidence/filter identities differ")
    for ev in evidence:
        if ev.get("pid") != pid or ev.get("source_format") not in ("v1_numeric_blocks", "v2"):
            raise ValueError("Only same-report v1/v2 delivery is supported")
        if hashlib.sha256(ev["content"].encode("utf-8")).hexdigest() != ev.get("content_sha256"):
            raise ValueError("Evidence content hash mismatch")
    for record in records:
        if record.get("pid") != pid or record.get("evidence_version_id") not in index or record.get("profile_id") != "water_v1":
            raise ValueError("Unsupported record identity/profile")
        field = record.get("profile_field")
        if field is not None and field not in FIELDS:
            raise ValueError("Unknown profile field; explicit adapter update required")
        record["indicator_id"], record["variable"] = iid(field), field or "UNMAPPED"
        normalization = record.get("normalization")
        if not isinstance(normalization, dict):
            raise ValueError("Normalization object missing")
        normalization["from"] = normalization.get("from_unit")
        normalization["to"] = normalization.get("to_unit")
    coverage = deepcopy(data.get("coverage.json") or {})
    coverage["by_indicator"] = {}
    hints = coverage.get("unclassified_hints", [])
    for field in coverage.get("key_fields", KEY_FIELDS):
        if field not in FIELDS:
            raise ValueError("Unknown key field")
        signal = coverage.get("by_field", {}).get(field, {})
        coverage["by_indicator"][iid(field)] = {**signal, "unclassified_hints": [h for h in hints if h.get("profile_field") == field]}
    converted = {"records.jsonl": records, "evidence.jsonl": evidence, "semantic_filter.jsonl": decisions,
                 "document_context.json": document, "filter_audit_sample.jsonl": deepcopy(data.get("filter_audit_sample.jsonl") or []),
                 "coverage.json": coverage, "catalogue.json": catalogue()}
    report = {"adapter_version": VERSION, "validation_scope": "local_delivery", "a_formal_contract_verified": False,
              "pid": pid, "source_document_context": source_context, "display_document_context": document,
              "projection": {"indicator_id": "water_v1:<profile_field>, internal namespace only", "variable": "profile_field or UNMAPPED", "normalization": "from_unit/to_unit aliases; values and units unchanged", "logical_group_key": "original A key unchanged"},
              "records": len(records), "evidence": len(evidence), "warnings": ["A正式Schema/profile/单位注册表尚未交付。", "报告年和披露期差异保留，未自动覆盖。", "未映射项不自动归类，吨转体积等未核验规则不自动确认。"]}
    return converted, report


def prepare(source, destination, *, max_model_calls=6):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination == source or source in destination.parents or destination in source.parents:
        raise ValueError("Outputs must be separate from A delivery tree")
    if destination.exists():
        raise ValueError("Destination already exists; use a new run directory")
    for name in REQUIRED:
        if not (source / name).is_file():
            raise ValueError("Missing input: " + name)
    paths = [source / name for name in SOURCE_FILES if (source / name).is_file()]
    before = {p.name: file_sha(p) for p in paths}
    data = {p.name: load_file(p, jsonl=p.suffix == ".jsonl") for p in paths}
    for name in ("filter_report.json", "classification_report.json"):
        if name in data and (data[name].get("fatal") or data[name].get("status") not in ("SUCCEEDED", "PARTIAL")):
            raise ValueError("Upstream stage failed")
    cr, fr = data.get("classification_report.json", {}), data.get("filter_report.json", {})
    if cr and fr and cr.get("input", {}).get("filter_run_id") != fr.get("filter_run_id"):
        raise ValueError("Mixed filter/classification runs")
    if cr and any(r.get("classification_run_id") != cr.get("classification_run_id") for r in data["records.jsonl"]):
        raise ValueError("Mixed classification records")
    converted, report = adapt(data)
    if before != {p.name: file_sha(p) for p in paths}:
        raise ValueError("Source changed during adaptation")
    destination.mkdir(parents=True)
    upstream = destination / "upstream"
    inputs = destination / "inputs"
    upstream.mkdir(); inputs.mkdir()
    for p in paths:
        raw = p.read_bytes()
        if hashlib.sha256(raw).hexdigest() != before[p.name]:
            raise ValueError("Source changed while copying")
        (upstream / p.name).write_bytes(raw)
    for name, value in converted.items():
        text = "\n".join(canonical(row) for row in value) if name.endswith(".jsonl") else canonical(value)
        (inputs / name).write_text(text + "\n", encoding="utf-8")
    policy = read_json((RESOURCE_DIR / "policy.json").read_text(encoding="utf-8"))
    policy.update(review_policy_version=VERSION, key_indicators=[iid(f) for f in KEY_FIELDS], max_model_calls=max_model_calls,
                  visual_sample_rate=0, sample_rate=1.0)
    (destination / "policy.json").write_text(canonical(policy), encoding="utf-8")
    report.update(source_directory=str(source), source_sha256=before, projected_sha256={p.name: file_sha(p) for p in inputs.iterdir()}, created_at=utcnow())
    (destination / "adapter_report.json").write_text(canonical(report), encoding="utf-8")
    return inputs, report


def run_delivery(source, output_root, *, mode="rules_only", llm=None, max_model_calls=6):
    from ..auditor import run_audit_file
    if mode not in ("rules_only", "model") or mode == "model" and llm is None:
        raise ValueError("Explicit rules_only or configured model mode required")
    report_id = Path(source).name
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", report_id):
        raise ValueError("Unsafe report directory name")
    run_id = "ad_" + uuid.uuid4().hex[:16]
    destination = Path(output_root) / run_id / report_id
    inputs, adaptation = prepare(source, destination, max_model_calls=max_model_calls)
    if os.name == "nt":
        absolute = str(inputs.resolve())
        if not absolute.startswith("\\\\?\\"):
            inputs = Path("\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\") else "\\\\?\\" + absolute)
    validator = DeliveryValidator()
    with integration_context(validator=validator, contract_fingerprint=validator.fingerprint,
                             validation_scope="local_delivery", record_format="profile_delivery_v1", execution_mode=mode,
                             catalogue=catalogue()):
        outcome = run_audit_file(inputs, policy_path=destination / "policy.json", llm=llm, run_id=run_id)
    source_root = Path(source)
    unchanged = all((source_root / name).is_file() and file_sha(source_root / name) == fingerprint for name, fingerprint in adaptation["source_sha256"].items())
    if not unchanged:
        raise ValueError("Source changed during audit; run cannot be accepted")
    published = inputs / "audit_runs" / run_id
    result = {"pid": outcome["pid"], "audit_run_id": run_id, "mode": mode, "status": outcome["status"],
              "stats": outcome["stats"], "source_unchanged": True, "output_directory": str(published.resolve()),
              "adaptation_directory": str(destination.resolve()), "formal_a_contract_verified": False}
    (destination / "run_summary.json").write_text(canonical(result), encoding="utf-8")
    return result
