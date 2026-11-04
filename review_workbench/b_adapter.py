"""B application adapter. Public B files remain immutable; UI projections are private.

Importing results never calls a model. Formal runs use an administrator-configured
A factory, never a module name supplied by a browser request.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import sys
import zipfile

from .contracts import ContractError, canonical, digest, loads
from .store import Conflict

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ("records.jsonl", "evidence.jsonl", "semantic_filter.jsonl", "document_context.json")
OUTPUTS = ("audit_findings.jsonl", "audit_results.jsonl", "audit_report.json", "review_items.jsonl")
OPTIONAL = ("filter_audit_sample.jsonl", "coverage.json", "catalogue.json")
STATUSES = {"CONFIRMED", "SUSPECT", "REJECTED", "INSUFFICIENT", "NOT_REVIEWED"}

APPLICABILITY_VERSION = "NUMERIC-REVIEW-APPLICABILITY-1.0"
QUALITATIVE_METRICS = {"water_policy":"水资源管理政策", "water_risk":"水资源风险叙述"}
NUMERIC_ONLY_PROBLEMS = {"DELIVERY_CLASSIFICATION_UNRESOLVED", "INDEPENDENT_SOURCE_REVIEW_REQUIRED",
    "UNIT_CONVERSION_UNVERIFIED", "RECORD_INPUT_INSUFFICIENT", "RECORD_REVIEW_INSUFFICIENT"}


def qualitative_outside_numeric_review(record,bundle,result):
    """A narrow applicability rule, never a blanket exemption for UNMAPPED."""
    upstream=record.get("upstream") or {}
    if not isinstance(upstream,dict):return False
    if (record.get("upstream_metric_id") not in QUALITATIVE_METRICS
            or upstream.get("classification_status")!="CLASSIFIED"
            or upstream.get("raw_value") is not None or not str(upstream.get("raw_text") or "").strip()
            or upstream.get("ambiguity_flags") or record.get("value") is not None
            or record.get("normalized_value") is not None or result.get("status")=="FAILED"):
        return False
    rid=record.get("record_uid")
    if any(f.get("record_uid")==rid for f in result.get("findings",[])):return False
    allowed=set(NUMERIC_ONLY_PROBLEMS)
    expected="|".join(str(v or "UNKNOWN") for v in (record.get("pid"),"water_v1",record.get("profile_field"),record.get("year"),record.get("scope_type")))
    if record.get("profile_id")=="water_v1" and record.get("logical_group_key")==expected:
        # Delivery preserves the original UNKNOWN key while its numeric-only
        # indicator projection says UNMAPPED. This is not a policy-text defect.
        allowed.add("LOGICAL_KEY_MISMATCH")
    if any(p.get("record_id")==rid and p.get("error_code",p.get("code")) not in allowed
           for p in result.get("b_report",{}).get("problems",[])):return False
    ids=upstream.get("evidence_ids") or []
    evidence={e["evidence_id"]:e for e in bundle.get("evidence",[])}
    return isinstance(ids,list) and bool(ids) and all(isinstance(eid,str) and eid in evidence and str(evidence[eid].get("content") or "").strip() for eid in ids)


def apply_numeric_applicability(bundle,result):
    for record in bundle["records"]:
        exempt=qualitative_outside_numeric_review(record,bundle,result)
        record["numeric_audit_applicability"]="NOT_APPLICABLE" if exempt else "APPLICABLE"
        if exempt:
            record["display_indicator_id"]=QUALITATIVE_METRICS[record["upstream_metric_id"]]
    return {r["record_uid"] for r in bundle["records"] if r["numeric_audit_applicability"]=="NOT_APPLICABLE"}


def core():
    source = str(ROOT / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from esg_semantic.auditor import run_audit_file
    from esg_semantic.audit.integration import integration_context
    return run_audit_file, integration_context


def parse_files(files, required):
    if not isinstance(files, dict) or set(files) - set(INPUTS + OUTPUTS + OPTIONAL):
        raise ContractError("请选择约定的B输入或结果文件；不接受目录路径和其他文件。")
    if any(not isinstance(v, str) for v in files.values()):
        raise ContractError("文件内容必须为UTF-8文本。")
    if sum(len(v.encode("utf-8")) for v in files.values()) > 7 * 1024 * 1024:
        raise ContractError("所选文本文件合计不能超过7 MB。较大数据请用B命令行处理。")
    missing = set(required) - set(files)
    if missing:
        raise ContractError("缺少文件：" + "、".join(sorted(missing)))
    parsed = {}
    for name, content in files.items():
        try:
            value = [loads(line) for line in content.lstrip("\ufeff").splitlines() if line.strip()] if name.endswith(".jsonl") else loads(content.lstrip("\ufeff"))
        except (ValueError, TypeError):
            raise ContractError("文件格式无效：" + name) from None
        if name.endswith(".jsonl"):
            if len(value) > 5000 or any(not isinstance(row, dict) for row in value):
                raise ContractError("JSONL须为对象列表，每个文件最多5000条：" + name)
        elif not isinstance(value, dict):
            raise ContractError("JSON须为对象：" + name)
        parsed[name] = value
    return parsed


def unique(rows, key):
    result = {}
    for row in rows:
        identity = row.get(key)
        if not isinstance(identity, str) or not identity.strip() or identity in result:
            raise ContractError("标识缺失或重复：" + key)
        result[identity] = row
    return result


def validate_results(data):
    """Local display/integrity checks, explicitly NOT authoritative A validation."""
    report = data["audit_report.json"]
    if not isinstance(report.get("problems", []), list) or any(not isinstance(p, dict) for p in report.get("problems", [])):
        raise ContractError("报告的problems须为对象列表。")
    if not isinstance(report.get("stats", {}), dict):
        raise ContractError("报告的stats须为对象。")
    if not isinstance(report.get("status"), str) or report["status"] not in {"SUCCEEDED", "PARTIAL", "FAILED"}:
        raise ContractError("B报告的运行状态无效。")
    if not all(isinstance(report.get(k), str) and report[k] for k in ("pid", "audit_run_id")):
        raise ContractError("B报告缺少pid或audit_run_id。")
    findings, results, items = (data[n] for n in ("audit_findings.jsonl", "audit_results.jsonl", "review_items.jsonl"))
    if report.get("findings") != findings or report.get("record_results") != results:
        raise ContractError("四类文件不一致：汇总中的发现或记录结果与JSONL不符。请选取同一次运行。")
    by_record = unique(results, "record_id")
    by_finding = unique(findings, "finding_id")
    unique(items, "item_id")
    for row in results + findings + items:
        if row.get("pid") != report["pid"] or row.get("audit_run_id") != report["audit_run_id"]:
            raise ContractError("文件混入不同报告或不同审查批次。")
    for row in results:
        if not isinstance(row.get("review_status"), str) or row["review_status"] not in STATUSES or not isinstance(row.get("finding_ids"), list) or any(not isinstance(x, str) for x in row["finding_ids"]):
            raise ContractError("逐记录状态或问题引用无效。")
        own = {f["finding_id"] for f in findings if f.get("record_id") == row["record_id"]}
        if set(row["finding_ids"]) != own or len(row["finding_ids"]) != len(own):
            raise ContractError("逐记录结果的问题引用不一致。")
    for finding in findings:
        if finding.get("record_id") is not None and not isinstance(finding["record_id"], str):
            raise ContractError("问题的记录标识无效。")
        if finding.get("record_id") is not None and finding["record_id"] not in by_record:
            raise ContractError("问题引用了不存在的记录结果。")
        if not isinstance(finding.get("severity"), str) or finding["severity"] not in {"BLOCK", "WARN", "INFO"} or not isinstance(finding.get("code"), str):
            raise ContractError("问题码或严重度无效。")
        ids = finding.get("evidence_version_ids")
        if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids):
            raise ContractError("问题证据版本引用无效。")
    for item in items:
        if not isinstance(item.get("finding_id"), str):
            raise ContractError("复核队列的问题标识无效。")
        finding = by_finding.get(item.get("finding_id"))
        if not finding or any(item.get(k) != finding.get(k) for k in ("record_id", "code", "severity", "evidence_version_ids", "page_no", "bbox")):
            raise ContractError("复核队列与审查问题不一致。")
    records = data.get("records.jsonl", [])
    unique(records, "record_id")
    for record in records:
        if record.get("pid") != report["pid"]:
            raise ContractError("输入记录属于其他报告。")
        for key in ("indicator_id", "variable", "unit", "breakdown", "evidence_version_id"):
            if record.get(key) is not None and not isinstance(record[key], str):
                raise ContractError("记录显示字段类型无效：" + key)
        if record.get("value") is not None and type(record["value"]) not in (str, int, float):
            raise ContractError("记录value不是可展示的数值或null。")
    for finding in findings:
        if finding.get("indicator_id") is not None and not isinstance(finding["indicator_id"], str):
            raise ContractError("问题的指标标识无效。")
    for evidence in data.get("evidence.jsonl", []):
        if not isinstance(evidence.get("content", ""), str):
            raise ContractError("证据content须为文本。")
        bbox = evidence.get("bbox")
        if bbox is not None and (not isinstance(bbox, list) or len(bbox) != 4 or any(type(x) not in (int, float) for x in bbox)):
            raise ContractError("证据bbox须为四个数值或null。")
    unique(data.get("evidence.jsonl", []), "evidence_version_id")
    if data.get("document_context.json", {}).get("pid", report["pid"]) != report["pid"]:
        raise ContractError("报告信息与结果的pid不一致。")
    # Reject wrong optional source records instead of letting a user review unrelated data.
    if records and set(by_record) - {r["record_id"] for r in records}:
        raise ContractError("所选records.jsonl未包含全部审查记录。")
    return report


def project(files, *, provenance, catalogue_override=None):
    """Map B output into old UI views without changing either public contract."""
    data = parse_files(files, OUTPUTS)
    report = validate_results(data)
    source = unique(data.get("records.jsonl", []), "record_id")
    result_rows = unique(data["audit_results.jsonl"], "record_id")
    all_ids = sorted(set(source) | set(result_rows))
    # Display-only placeholders for absent source inputs are clearly marked and read-only.
    records = []
    for rid in all_ids:
        row = source.get(rid, {})
        linked = next((f for f in report["findings"] if f.get("record_id") == rid), {})
        records.append({**row, "record_uid": rid, "indicator_id": row.get("indicator_id") or linked.get("indicator_id") or "未提供指标",
                        "variable": row.get("variable") or rid, "value": row.get("value"), "unit": row.get("unit"),
                        "year": row.get("year"), "breakdown": row.get("breakdown"), "evidence_id": row.get("evidence_version_id"),
                        "source_available": rid in source})
    evidence = [{**e, "evidence_id": e["evidence_version_id"], "content": e.get("content") or "", "kind": e.get("kind") or "unknown"}
                for e in data.get("evidence.jsonl", [])]
    catalogue = catalogue_override if catalogue_override is not None else data.get("catalogue.json", {})
    indicators = []
    for iid in sorted({r["indicator_id"] for r in records} | {f.get("indicator_id") or "未提供指标" for f in report["findings"]}):
        definition = catalogue.get(iid, {})
        variables = definition.get("variables", []) if isinstance(definition, dict) else []
        indicators.append({"indicator_id": iid, "variables": variables if isinstance(variables, list) and all(isinstance(v, str) for v in variables) else []})
    document = data.get("document_context.json", {})
    bundle = {"report_context": {"pid": report["pid"], "company_name": document.get("company_name") or report["pid"],
                                 "report_year": document.get("report_year") or "未提供"},
              "records": records, "evidence": evidence,
              "catalogue": {"version": "B结果视图 / " + str(report.get("review_policy_version", "未提供策略版本")), "indicators": indicators},
              "b_source_files": files, "b_provenance": provenance, "b_effective_catalogue": catalogue,
              "b_execution_mode": report.get("execution_mode")}
    findings = [{**f, "record_uid": f.get("record_id"), "evidence_ids": f.get("evidence_version_ids", []),
                 "reason": f.get("reason") or f["code"]} for f in report["findings"]]
    verdicts = []
    for rid in all_ids:
        row = result_rows.get(rid, {})
        reasons = [p.get("message") or p.get("error_code") for p in report.get("problems", []) if p.get("record_id") == rid]
        verdicts.append({"record_uid": rid, "verdict": row.get("review_status", "INSUFFICIENT"),
                         "reason": "；".join(str(x) for x in reasons if x) or ("原始输入未附加，当前仅展示结果。" if rid not in source else "B逐记录审查结果；请结合问题与原文核对。")})
    result = {"status": report["status"], "findings": findings, "record_verdicts": verdicts, "issues": [],
              "manifest": {"input_sha256": digest(files)}, "b_report": report}
    return bundle, result


def tasks_for(run_id, bundle, result):
    from .review_policy import plan
    apply_numeric_applicability(bundle,result)
    tasks,_=plan(bundle,result)
    priorities = {f["finding_id"]: f["severity"] for f in result["findings"]}
    for i, task in enumerate(tasks):
        task.update(task_id=f"bt_{run_id}_{i:05d}", run_id=run_id,
                    priority="P0" if any(priorities[f] == "BLOCK" for f in task["finding_ids"]) else "P1")
    return tasks


class BWorkbench:
    def __init__(self, app):
        self.app = app

    def configuration(self):
        path = Path(os.environ.get("ESG_B_WORKBENCH_CONFIG", ROOT / "review_workbench.b.local.json"))
        if not path.is_file():
            raise ContractError("尚未配置A侧契约和客户端；可先运行B合成演示或导入B结果。")
        try:
            config = loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(config, dict) or set(config) - {"factory", "contracts", "policy", "catalogue"} or not isinstance(config.get("factory"), str) or ":" not in config["factory"]:
                raise ValueError()
            for key in ("contracts", "policy", "catalogue"):
                if config.get(key):
                    config[key] = str((path.parent / config[key]).resolve())
                    if not Path(config[key]).exists():
                        raise ValueError()
            return config
        except (ValueError, TypeError, OSError):
            raise ContractError("B本机配置无效，请按B界面接入说明检查配置和文件路径。") from None

    def capabilities(self):
        try:
            self.configuration()
            configured, message = True, "已提供本机配置；正式运行时仍须通过A契约校验。"
        except ContractError as exc:
            configured, message = False, str(exc)
        return {"configured": configured, "message": message, "max_text_mb": 7}

    def import_results(self, files):
        bundle, result = project(files, provenance="import_unverified")
        # Import does not assert that an uploaded report is synthetic or formally validated.
        identity = self.app.store.import_bundle(bundle, False)
        run_id = self.app.store.create_run(identity, "b_import")
        self.app.store.complete(run_id, result, tasks_for(run_id, bundle, result))
        return run_id

    def start(self, files=None, *, demo=False):
        data = None
        if not demo:
            self.configuration()  # Fast feedback; never queue a known unconfigured formal run.
            data = parse_files(files, INPUTS)
            if set(files) & set(OUTPUTS):
                raise ContractError("运行输入中不能混入审查输出文件。")
            if data["document_context.json"].get("pdf_path"):
                raise ContractError("网页运行不读取上传文本中的本机PDF路径。需PDF视觉审查时，请用B命令行运行后导入结果；文本审查可使用pdf_path为null的输入。")
        with self.app.lock:
            if self.app.active and not self.app.active.done():
                raise Conflict("已有审查运行中，请等待完成。")
            if demo:
                files = {name: (ROOT / "tests/fixtures/audit/normal" / name).read_text(encoding="utf-8")
                         for name in INPUTS + OPTIONAL if (ROOT / "tests/fixtures/audit/normal" / name).is_file()}
                files["catalogue.json"] = (ROOT / "tests/fixtures/audit/catalogue.json").read_text(encoding="utf-8")
                data = parse_files(files, INPUTS)
            context = data["document_context.json"]
            if not isinstance(context.get("pid"), str) or not context["pid"]:
                raise ContractError("document_context.json缺少pid。")
            initial = {"report_context": {"pid": context["pid"], "company_name": context.get("company_name") or context["pid"],
                                          "report_year": context.get("report_year") or "未提供"},
                       "records": [], "evidence": [], "catalogue": {"version": "B审查准备中", "indicators": []},
                       "b_source_files": files, "b_provenance": "demo" if demo else "formal_pending"}
            identity = self.app.store.import_bundle(initial, demo)
            run_id = self.app.store.create_run(identity, "b_demo" if demo else "b_formal")
            self.app.active = self.app.executor.submit(self._execute, run_id, files, demo)
            return run_id

    def _execute(self, run_id, files, demo):
        try:
            run_audit_file, integration_context = core()
            directory = self.app.store.directory / "b_runs" / run_id
            # B publishes through a UUID staging directory. Windows' legacy 260-char
            # limit can otherwise fail even when the workbench database opens normally.
            if os.name == "nt":
                absolute = str(directory.resolve())
                if not absolute.startswith("\\\\?\\"):
                    directory = Path("\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\") else "\\\\?\\" + absolute)
            directory.mkdir(parents=True, exist_ok=False)
            for name, text in files.items():
                (directory / name).write_text(text, encoding="utf-8")
            policy = ROOT / "skills/audit/policy.json"
            bindings = {"fixture": demo, "catalogue": loads(files.get("catalogue.json", "{}"))}
            if demo:
                from scripts.esg_audit_run import fake_client
                llm = fake_client()
            else:
                config = self.configuration()
                module, name = config["factory"].split(":", 1)
                provided = dict(getattr(importlib.import_module(module), name)())
                llm = provided.pop("llm")
                if provided.get("fixture"):
                    raise ValueError("Formal factory cannot enable fixture mode")
                bindings.update(provided)
                if config.get("contracts"):
                    from esg_semantic.audit.external_contracts import ExternalContracts
                    validator = ExternalContracts(config["contracts"])
                    bindings.update(validator=validator, contract_fingerprint=validator.fingerprint)
                if config.get("policy"):
                    policy = Path(config["policy"])
                if config.get("catalogue"):
                    bindings["catalogue"] = loads(Path(config["catalogue"]).read_text(encoding="utf-8-sig"))
            with integration_context(**bindings):
                run_audit_file(directory, policy_path=policy, llm=llm, run_id=run_id)
            published = directory / "audit_runs" / run_id
            snapshot = {**files, **{name: (published / name).read_text(encoding="utf-8") for name in OUTPUTS}}
            bundle, result = project(snapshot, provenance="demo" if demo else "formal_bound", catalogue_override=bindings.get("catalogue"))
            # Both snapshots and completion are committed together; the submitted input bundle is retained.
            self.app.store.complete(run_id, {**result, "b_view": bundle}, tasks_for(run_id, bundle, result))
        except Exception:
            # A factory/provider exception may contain credentials. Never return its raw text.
            self.app.store.fail(run_id, "B_AUDIT_FAILED_CHECK_INPUT_CONTRACT_CLIENT",
                                [{"task_id": "bt_" + run_id + "_startup", "run_id": run_id, "record_uid": None,
                                  "indicator_id": "运行状态检查", "finding_ids": [], "review_status": "INSUFFICIENT",
                                  "priority": "P0", "title": "B运行失败，请检查输入、契约、依赖及客户端"}])

    def detail(self, detail):
        if detail["result"] and detail["result"].get("b_view"):
            detail["bundle"] = detail["result"]["b_view"]
        detail["engine"] = "b"
        exempt=apply_numeric_applicability(detail["bundle"],detail["result"] or {})
        from .review_policy import plan
        planned,routing=plan(detail["bundle"],detail["result"] or {})
        def match(task):
            return next((p for p in planned if p.get("record_uid")==task.get("record_uid") and
                (task.get("record_uid") is not None or set(p["finding_ids"])&set(task.get("finding_ids",[])))),None)
        removed=[t for t in detail["tasks"] if match(t) is None]
        detail["tasks"]=[t for t in detail["tasks"] if t not in removed]
        for task in detail["tasks"]:
            task.update({k:v for k,v in match(task).items() if k in ("title","finding_ids","review_reason","review_question","review_evidence")})
        detail["archived_nonactionable_tasks"]=removed
        detail["review_routing"]=routing
        detail["numeric_review_applicability"]={"version":APPLICABILITY_VERSION,"not_applicable_records":len(exempt),
            "automatic_tasks_suppressed":len(removed),"note":"定性披露不因缺少数值或单位要求人工复核；原B结果及已保存人工意见保留。"}
        detail["b_provenance"] = detail["bundle"]["b_provenance"]
        detail["display_values"] = {r["record_uid"]: str(r["value"]) if r.get("value") is not None else None for r in detail["bundle"]["records"]}
        # Keep source decimal digits in the UI as well as in the exported original.
        for line in detail["bundle"]["b_source_files"].get("records.jsonl", "").lstrip("\ufeff").splitlines():
            if line.strip():
                row = json.loads(line, parse_float=str, parse_int=str)
                if row.get("record_id") in detail["display_values"]:
                    value = row.get("value")
                    detail["display_values"][row["record_id"]] = str(value) if value is not None else None
        # Website reviews are separate events, not B results or S5 clearance.
        detail["handoff"] = {"schema_version": "b-workbench-review-1", "run_id": detail["run_id"],
                             "source_audit_run_id": (detail["result"] or {}).get("b_report", {}).get("audit_run_id"),
                             "formal_contract_bound": detail["b_provenance"] == "formal_bound",
                             "s5_validation_required": True, "production_ready": False,
                             "note": "人工意见仅供交接；原始B结果不变。修订须生成新版本重审，入库仍需S5验证。"}
        return detail

    def validate_review(self, detail, task, decision):
        record = next((r for r in detail["bundle"]["records"] if r["record_uid"] == task["record_uid"]), None)
        if decision["action"] in {"CORRECT_VALUE", "REMAP", "EXCLUDE_FROM_SCORING"} and (not record or not record.get("source_available")):
            raise ContractError("未附加原始记录，仅可保留或暂缓；请补充records.jsonl后提交修订。")
        if decision["action"] == "REMAP":
            indicator = next((i for i in detail["bundle"]["catalogue"]["indicators"] if i["indicator_id"] == task["indicator_id"]), {})
            if decision.get("proposed_variable") not in indicator.get("variables", []):
                raise ContractError("未提供有效指标闭集，或候选变量不在闭集中。")
        if decision["action"] == "CORRECT_VALUE" and decision.get("proposed_value") is None:
            raise ContractError("请填写候选修订值；允许真实的0。")
        for action, field in (("CORRECT_VALUE", "proposed_value"), ("REMAP", "proposed_variable")):
            if decision["action"] != action and decision.get(field) is not None:
                raise ContractError("此操作不接受" + field)

    def export(self, detail):
        if detail["status"] == "RUNNING":
            raise Conflict("运行尚未完成，请稍后导出。")
        files = {"b_original/" + name: text for name, text in detail["bundle"]["b_source_files"].items()}
        files["workbench/review_events.jsonl"] = "\n".join(canonical(e) for e in detail["events"])
        files["workbench/review_tasks.json"] = canonical(detail["tasks"])
        files["workbench/archived_nonactionable_tasks.json"] = canonical(detail.get("archived_nonactionable_tasks",[]))
        files["workbench/review_routing.json"] = canonical(detail.get("review_routing",{}))
        files["workbench/handoff.json"] = canonical(detail["handoff"])
        files["workbench/effective_catalogue.json"] = canonical(detail["bundle"].get("b_effective_catalogue", {}))
        files["README.txt"] = "b_original保留导入或生成的B原始文本；workbench仅保存人工复核意见。人工意见不改写审查结果，也不等于通过S5。"
        files["manifest.json"] = canonical({"run_id": detail["run_id"], "status": detail["status"], "is_demo": detail["is_demo"],
                                            "pdf_sha256": detail["pdf_sha256"],
                                            "provenance": detail["b_provenance"], "error": detail["error"],
                                            "file_sha256": {n: hashlib.sha256(t.encode("utf-8")).hexdigest() for n, t in files.items()}})
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, text in files.items():
                archive.writestr(name, text.encode("utf-8"))
        return output.getvalue()
