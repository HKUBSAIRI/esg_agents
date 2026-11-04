"""File-based orchestration: immutable A artifacts, independent B, existing review UI."""
from collections import Counter
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import shutil
import uuid
from urllib.parse import quote
import os
import re

from esg_extraction.extractor import atomic_json, canonical, file_hash, fingerprint, output_jsonl
from esg_semantic.filter import filter_evidence, VERSION as FILTER_VERSION
from esg_semantic.classifier import classify
from esg_semantic.audit.delivery import run_delivery
from . import VERSION
from .inputs import load_extraction, read_lines
from .llm import JsonClient, usage_summary
from .profiles import load_profile

ROOT = Path(__file__).resolve().parents[2]


def write_json(directory, name, value):
    atomic_json(Path(directory) / name, value)


def coverage(records, candidates, evidence, profile, run_id, document):
    by_field, hints = {}, []
    ev_index = {e["evidence_version_id"]: e for e in evidence}
    for field, spec in profile["fields"].items():
        assigned = [r for r in records if r["profile_field"] == field and r["classification_status"] == "ASSIGNED"]
        matches = [c for c in candidates if any(re.search(p, str(c.get("metric_label") or c.get("metric_label_raw") or ""), re.I) for p in spec["patterns"])]
        unresolved = [c for c in matches if not any(r["candidate_id"] == c["candidate_id"] for r in assigned)]
        by_field[field] = {"n_records": len(assigned), "n_keyword_hits": len(matches), "n_unclassified_hits": len(unresolved)}
        for c in unresolved[:8]:
            ev = ev_index[c["evidence_version_id"]]
            hints.append({"profile_field": field, "evidence_version_id": ev["evidence_version_id"], "page_no": ev["page_no"],
                          "keyword": c.get("metric_label") or c.get("metric_label_raw"), "excerpt": ev["content"][:500]})
    return {"schema_version": "local-a-1.0", "pid": document["pid"], "classification_run_id": run_id,
            "profile_version": profile["version"], "key_fields": profile["key_fields"], "by_field": by_field,
            "unclassified_hints": hints, "note": "关键词仅为复核线索，不等于漏提事实；字段记录数不是去重后的公司指标数。"}


def import_review(summary, document, database):
    from review_workbench.service import Application
    from review_workbench.store import Store
    from esg_semantic.audit.files import REQUIRED, OPTIONAL, OUTPUTS
    out = Path(summary["output_directory"])
    inputs = out.parents[1]
    files = {name: (inputs / name).read_text(encoding="utf-8") for name in REQUIRED + OPTIONAL + ("catalogue.json",) if (inputs / name).is_file()}
    files.update({name: (out / name).read_text(encoding="utf-8") for name in OUTPUTS})
    app = Application(Store(database))
    try:
        run_id = app.b.import_results(files)
        detail = app.store.detail(run_id)
        source = Path(document["pdf_path"])
        name = document["pdf_sha256"] + ".pdf"
        dest = Path(database) / "pdfs" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.is_file():
            shutil.copyfile(source, dest)
        if file_hash(dest) != document["pdf_sha256"]:
            raise ValueError("WORKBENCH_PDF_HASH_CHANGED")
        app.store.attach_pdf(detail["bundle_id"], name)
        return run_id
    finally:
        app.close()


def preview(directory, records, evidence, audit_report, summary):
    esc = lambda v: html.escape("未知" if v is None else str(v))
    ev_index = {e["evidence_version_id"]: e for e in evidence}
    results = {r["record_id"]: r["review_status"] for r in audit_report["record_results"]}
    names = {"ASSIGNED":"已归类", "AMBIGUOUS":"归类有歧义", "INSUFFICIENT_CONTEXT":"缺少上下文", "UNMAPPED":"未映射", "INSUFFICIENT":"需补充核对", "NOT_REVIEWED":"尚未审查", "SUSPECT":"有疑点", "REJECTED":"存在阻断问题", "CONFIRMED":"已审查确认"}
    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ESG全流程结果</title><style>body{font:16px/1.6 system-ui,"Microsoft YaHei";margin:32px;color:#183c3d}h1{overflow-wrap:anywhere}table{border-collapse:collapse;width:100%;font-size:14px}td,th{border:1px solid #ccd;padding:8px;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{background:#e6f1ed}.scroll{overflow-x:auto}a{color:#076e62}</style>']
    parts.append(f'<h1>{esc(summary["source_pdf"])}：提取 → A归类 → B审查</h1><p>模式：{esc(summary["mode"])}。所有候选均保留；未独立核对原页，不得直接入库或评分。以下优先展示水领域记录，最多150条；完整数据见<a href="a/records.jsonl">records.jsonl</a>。</p>')
    parts.append(f'<p>候选{summary["candidate_count"]}；生成记录{len(records)}；ASSIGNED {summary["classification_states"].get("ASSIGNED",0)}（不等于审查通过）。<a href="http://127.0.0.1:8765/#{esc(summary.get("workbench_run_id") or "")}">在审查工作台查看</a>（需启动工作台）。</p>')
    parts.append('<div class="scroll"><table><tr><th>PDF页</th><th>指标原文</th><th>值/单位</th><th>年份/范围</th><th>A字段/状态</th><th>B状态</th><th>原页</th></tr>')
    ordered = sorted(records, key=lambda r: (r["profile_field"] is None, r["source_page"], r["record_id"]))
    for row in ordered[:150]:
        ev = ev_index[row["evidence_version_id"]]
        asset = quote(os.path.relpath(ev["source_ref"]["asset_path"], directory).replace("\\", "/"), safe="/.")
        parts.append(f'<tr><td>{row["source_page"]}</td><td>{esc(row["metric_label_raw"])}</td><td>{esc(row["raw_value"])} {esc(row["raw_unit"])}</td><td>{esc(row["year"])} / {esc(row["scope_note"])}</td><td>{esc(row["profile_field"])}<br>{esc(names.get(row["classification_status"],row["classification_status"]))}<br>{esc(row["classification_reason"])}</td><td>{esc(names.get(results.get(row["record_id"]),results.get(row["record_id"])))}</td><td><a href="{asset}">截图</a></td></tr>')
    parts.append('</table></div><p>未知、破折号、不适用不补0；吨不自动转换成立方米；不同统计范围不相加。</p></html>')
    (Path(directory) / "结果预览.html").write_text("".join(parts), encoding="utf-8")


def run_pipeline(extraction, output_root, *, recovered=None, pdf=None, mode="rules_only", key_file=None,
                 a_calls=8, b_calls=6, profile_path=None, database=None, cached_a=False, progress=None, cancel_check=None, display_name=None):
    if mode not in ("rules_only", "model"):
        raise ValueError("PIPELINE_MODE_INVALID")
    if mode == "model" and not key_file:
        raise ValueError("MODEL_KEY_FILE_REQUIRED")
    if not 0 <= a_calls <= 20 or not 0 <= b_calls <= 20 or mode == "model" and a_calls + b_calls < 1:
        raise ValueError("MODEL_BUDGET_INVALID")
    profile = load_profile(profile_path)
    data = load_extraction(extraction, recovered=recovered, pdf=pdf)
    run_id = "pl_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:6]
    directory = Path(output_root).resolve() / run_id
    source_dir = Path(extraction).resolve()
    if source_dir == directory or source_dir in directory.parents:
        raise ValueError("OUTPUT_MUST_BE_SEPARATE_FROM_EXTRACTION")
    directory.mkdir(parents=True, exist_ok=False)
    a_dir = directory / "a"
    a_dir.mkdir()
    client = JsonClient(directory, key_file=key_file, max_calls=max(1, b_calls if cached_a else a_calls + b_calls),
                        stage_budgets={"classify": 0, "filter": 0, "review": b_calls} if cached_a else None) if mode == "model" else None
    if client and cancel_check:
        client.before_call = cancel_check
    state = {"run_id": run_id, "stage": "FILTER", "status": "RUNNING", "mode": mode}
    def advance(stage):
        if cancel_check:
            cancel_check()
        state["stage"] = stage
        write_json(directory, "task.json", state)
        if progress:
            progress(stage, {"pipeline_directory": str(directory)})
    write_json(directory, "task.json", state)
    try:
        advance("FILTER")
        document = data["document"]
        if display_name:
            document["company_name"] = Path(display_name).stem
            document["company_name_source"] = "DISPLAY_FILENAME_NOT_ENTITY_VERIFIED"
        decisions = filter_evidence(data["evidence"])
        fr = "fr_" + run_id
        cr = "cr_" + run_id
        advance("CLASSIFY")
        records, outcomes, receipts = classify(data["candidates"], data["evidence"], decisions, document, profile, cr, fr,
                                               llm=client, max_model_calls=a_calls)
        assert len(outcomes) == len(data["candidates"])
        output_jsonl(a_dir / "quantitative_candidates.jsonl", data["candidates"])
        output_jsonl(a_dir / "evidence.jsonl", data["evidence"])
        output_jsonl(a_dir / "semantic_filter.jsonl", decisions)
        output_jsonl(a_dir / "filter_audit_sample.jsonl", [d for d in decisions if d["status"] == "REVIEW"][:30])
        output_jsonl(a_dir / "records.jsonl", records)
        output_jsonl(a_dir / "candidate_outcome.jsonl", outcomes)
        by_evidence = []
        for ev in data["evidence"]:
            rows = [r for r in records if r["evidence_version_id"] == ev["evidence_version_id"]]
            by_evidence.append({"evidence_version_id": ev["evidence_version_id"], "classification_run_id": cr,
                                "record_ids": [r["record_id"] for r in rows], "n_records": len(rows),
                                "status": "HAS_RECORD" if rows else "NO_RECORD_RETAINED"})
        output_jsonl(a_dir / "classification_outcome.jsonl", by_evidence)
        write_json(a_dir, "profile.json", profile)
        write_json(a_dir, "document_context.json", document)
        write_json(a_dir, "coverage.json", coverage(records, data["candidates"], data["evidence"], profile, cr, document))
        write_json(a_dir, "filter_report.json", {"filter_run_id": fr, "status": "SUCCEEDED", "policy_version": FILTER_VERSION,
                                               "stats": dict(Counter(d["status"] for d in decisions)), "auto_exclude_enabled": False,
                                               "execution": "RULES_HARD_KEEP_FOR_QUANTITATIVE_REGIONS"})
        write_json(a_dir, "classification_report.json", {"classification_run_id": cr, "status": "PARTIAL", "mode": mode,
                                                       "input": {"filter_run_id": fr}, "profile_version": profile["version"],
                                                       "model_batches": receipts, "formal_original_catalogue_verified": False,
                                                       "stats": dict(Counter(r["classification_status"] for r in records))})
        advance("AUDIT")
        b_result = run_delivery(a_dir, directory / "b", mode=mode, llm=client.review if client else None, max_model_calls=b_calls)
        audit_report = json.loads((Path(b_result["output_directory"]) / "audit_report.json").read_text(encoding="utf-8"))
        if b_result["status"] == "FAILED":
            raise ValueError("B_AUDIT_FAILED")
        advance("VALIDATE")
        source_ok = all(file_hash(path) == value for path, value in data["source_hashes"].items()) and file_hash(document["pdf_path"]) == document["pdf_sha256"]
        if not source_ok:
            raise ValueError("SOURCE_CHANGED_DURING_PIPELINE")
        record_ids = {r["record_id"] for r in records}
        assert record_ids == {rid for outcome in outcomes for rid in outcome["record_ids"]}
        assert record_ids == {r["record_id"] for r in audit_report["record_results"]}
        validation = {"status": "REVIEW_REQUIRED", "production_ready": False, "scoring_enabled": False,
                      "records": len(records), "records_with_safe_unit_normalization": sum(r["normalization"]["status"] == "OK" for r in records),
                      "records_ready_for_storage": 0, "source_unchanged": True,
                      "note": "仅进行本地结构、溯源和单位验证；未实现原总方案完整S5质量分档与生产入库。所有候选待独立原页核验。"}
        write_json(directory, "validation_report.json", validation)
        advance("IMPORT_REVIEW")
        ui_run = import_review(b_result, document, database) if database else None
        summary = {"run_id": run_id, "version": VERSION, "status": "PARTIAL", "mode": mode, "source_pdf": display_name or Path(document["pdf_path"]).name,
                   "source_sha256": document["pdf_sha256"], "source_unchanged": True, "profile_version": profile["version"],
                   "candidate_count": len(data["candidates"]), "evidence_count": len(data["evidence"]), "record_count": len(records),
                   "candidate_outcome_count": len(outcomes), "classification_states": dict(Counter(r["classification_status"] for r in records)),
                   "b_review_states": dict(Counter(r["review_status"] for r in audit_report["record_results"])),
                   "b_findings": len(audit_report["findings"]), "b_result": b_result, "usage": usage_summary(directory),
                   "workbench_run_id": ui_run, "output_directory": str(directory), "upstream_extraction_status": data["extraction_report"]["status"],
                   "page_coverage": dict(Counter(p["visual_status"] for p in data["page_coverage"])), "production_ready": False}
        write_json(directory, "run_summary.json", summary)
        write_json(directory, "manifest.json", {"version": VERSION, "source_hashes": data["source_hashes"], "profile_sha256": fingerprint(profile),
                                               "pdf_sha256": document["pdf_sha256"], "source_directory": str(source_dir),
                                               "a_artifact_hashes": {p.name: file_hash(p) for p in a_dir.iterdir() if p.is_file()}})
        from .annual import build_collection, persist
        collection = build_collection([summary])
        annual_path = persist(collection, directory / "annual")
        summary["annual_collection"] = {"path":str(annual_path), "statistics":collection["statistics"], "scoring_enabled":False}
        write_json(directory, "run_summary.json", summary)
        preview(directory, records, data["evidence"], audit_report, summary)
        text = [f'# {summary["source_pdf"]} 全流程结果', '', f'运行：{run_id}；模式：{mode}；整体状态：PARTIAL（待复核）。', '',
                f'保留 {summary["candidate_count"]} 条候选、{summary["evidence_count"]} 条证据，生成 {len(records)} 条记录。候选数不等于独立指标数。',
                f'A归类状态：{summary["classification_states"]}。B状态：{summary["b_review_states"]}；发现 {summary["b_findings"]} 项。',
                f'本次新增模型用量：{summary["usage"]}。不包含之前PDF视觉提取用量。', '',
                '没有报告年、数据年、单位或业务范围时保留未知；吨不自动换成立方米，不进行评分。S2保留全部定量区域，S3按本地水领域profile归类，S4独立审查，验证门禁保留人工核对。', '',
                '原提取失败页和模型未发现数据的页面均不能视为完整覆盖；未独立核对的视觉转录不得自动确认。其余主题仍保留候选，等待新profile扩展。', '',
                '[查看提取、归类及审查对照](结果预览.html) · [A记录](a/records.jsonl) · [逐候选处理台账](a/candidate_outcome.jsonl) · [本地验证结果](validation_report.json)', '',
                f'工作台运行ID：{ui_run or "未导入"}。本总结为结构化结果的确定性模板，不冒充S6模型总结Agent。']
        (directory / "处理总结.md").write_text("\n".join(text) + "\n", encoding="utf-8")
        state.update(stage="FINISHED", status="PARTIAL", summary=str(directory / "run_summary.json"))
        write_json(directory, "task.json", state)
        return summary
    except Exception as exc:
        state.update(status="FAILED", error_type=type(exc).__name__)
        write_json(directory, "task.json", state)
        raise
