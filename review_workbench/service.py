"""Application use cases; usable by CLI, local HTTP, or a future FastAPI/Planner adapter."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import io
from pathlib import Path
import threading
import zipfile

from . import __version__
from .auditor import audit, now
from .contracts import ContractError, HANDOFF, REVIEW, SCHEMAS, canonical, digest, loads, validate, validate_bundle
from .providers import OfflineProvider, ReplayProvider
from .store import Conflict, NotFound

EXAMPLES = Path(__file__).parent / "examples"


def demo_bundle():
    return loads((EXAMPLES / "demo.bundle.json").read_text(encoding="utf-8"))


def build_tasks(run_id, bundle, result):
    grouped = {}
    for verdict in result["record_verdicts"]:
        if verdict["verdict"] != "CONFIRMED":
            grouped[(verdict["indicator_id"], verdict["record_uid"])] = []
    for finding in result["findings"]:
        if finding["severity"] in ("BLOCK", "WARN"):
            grouped.setdefault((finding["indicator_id"], finding["record_uid"]), []).append(finding)
    for issue in result["issues"]:
        if not any(r["indicator_id"] == issue["indicator_id"] for r in bundle["records"]):
            grouped.setdefault((issue["indicator_id"], None), [])
    return [{"task_id": f"rt_{run_id}_{index:04d}", "run_id": run_id, "indicator_id": key[0],
             "record_uid": key[1], "finding_ids": [f["finding_id"] for f in findings],
             "priority": "P0" if any(f["severity"] == "BLOCK" for f in findings) else "P1",
             "title": "记录审查需人工确认" if key[1] else "指标组遗漏/证据覆盖需人工确认"}
            for index, (key, findings) in enumerate(sorted(grouped.items(), key=lambda x: (x[0][0], x[0][1] or "")), 1)]


def handoff(detail):
    rows = []
    verdicts = {v["record_uid"]: v for v in (detail["result"] or {}).get("record_verdicts", [])}
    for record in detail["bundle"]["records"]:
        tasks = [t for t in detail["tasks"] if t["indicator_id"] == record["indicator_id"] and
                 t["record_uid"] in (None, record["record_uid"])]
        states = {t["state"] for t in tasks}
        if "REAUDIT_REQUIRED" in states:
            state = "REAUDIT_REQUIRED"
        elif "EXCLUDED_FROM_SCORING" in states:
            state = "EXCLUDED_FROM_SCORING"
        elif "OPEN" in states or not detail["result"]:
            state = "REVIEW_REQUIRED"
        elif tasks or verdicts.get(record["record_uid"], {}).get("verdict") == "CONFIRMED":
            state = "READY_FOR_S5"
        else:
            state = "REVIEW_REQUIRED"
        rows.append({"record_uid": record["record_uid"], "indicator_id": record["indicator_id"],
                     "state": state, "audit_run_id": detail["run_id"], "is_demo": detail["is_demo"],
                     "task_ids": [t["task_id"] for t in tasks], "s5_validation_required": True})
    return {"schema_version": "s4-handoff-1.0", "run_id": detail["run_id"], "records": rows,
            "unresolved_group_tasks": [t["task_id"] for t in detail["tasks"] if t["record_uid"] is None and t["state"] != "RESOLVED"],
            "note": "READY_FOR_S5 仅表示可送验证，不表示可入库或可评分；模拟结果不能用于生产。"}


class Application:
    def __init__(self, store, http_provider=None):
        self.store, self.http_provider = store, http_provider
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="s4-audit")
        self.lock = threading.Lock()
        self.active = None
        from .b_adapter import BWorkbench
        self.b = BWorkbench(self)
        from .pipeline import PipelineWorkbench
        self.pipeline = PipelineWorkbench(self)

    def close(self):
        self.pipeline.cancel_owned()
        self.executor.shutdown(wait=True, cancel_futures=True)

    def import_data(self, bundle):
        validate_bundle(bundle)
        synthetic = (digest(bundle) == digest(demo_bundle()) or
                     bundle["report_context"].get("extensions", {}).get("is_synthetic") is True)
        return self.store.import_bundle(bundle, synthetic)

    def demo(self):
        bundle = demo_bundle()
        validate_bundle(bundle)
        identity = self.store.import_bundle(bundle, True)
        return self.start(identity, "demo_replay")

    def start(self, identity, mode="offline"):
        imported = self.store.get_bundle(identity)
        if "b_source_files" in imported["payload"]:
            raise ContractError("B数据请使用B独立审查入口；旧引擎不接收B结果视图。")
        if mode == "demo_replay":
            if not imported["is_demo"] or digest(imported["payload"]) != digest(demo_bundle()):
                raise ContractError("回放仅限内置合成演示数据")
            provider = ReplayProvider(loads((EXAMPLES / "demo.responses.json").read_text(encoding="utf-8")))
        elif mode == "http":
            if self.http_provider is None:
                raise ContractError("尚未配置真实模型，请先按 README 配置")
            provider = self.http_provider
        elif mode == "offline":
            provider = OfflineProvider()
        else:
            raise ContractError("不支持的审查模式")
        with self.lock:
            if self.active and not self.active.done():
                raise Conflict("已有审查运行中，请等待完成")
            run_id = self.store.create_run(identity, mode)
            self.active = self.executor.submit(self._execute, run_id, imported["payload"], provider)
        return run_id

    def _execute(self, run_id, bundle, provider):
        try:
            result = audit(bundle, provider, run_id, lambda done, total: self.store.progress(run_id, done, total))
            result["manifest"]["code_version"] = __version__
            self.store.complete(run_id, result, build_tasks(run_id, bundle, result))
        except Exception:
            # No raw exception text: a provider may include request content or credentials.
            self.store.fail(run_id, "AUDIT_INTERNAL_ERROR")

    def detail(self, run_id):
        detail = self.store.detail(run_id)
        if detail["provider"].startswith("b_"):
            detail["pipeline_available"] = self.pipeline.summary_for_run(run_id) is not None
            return self.b.detail(detail)
        # Preserve exact input digits in the browser (JS numbers can round large integers/decimals).
        detail["display_values"] = {r["record_uid"]: str(r["value"]) if r["value"] is not None else None
                                    for r in detail["bundle"]["records"]}
        detail["handoff"] = handoff(detail)
        validate(detail["handoff"], HANDOFF)
        return detail

    def review(self, run_id, task_id, decision):
        validate(decision, REVIEW)
        detail = self.detail(run_id)
        task = next((t for t in detail["tasks"] if t["task_id"] == task_id), None)
        if task is None:
            raise NotFound("未找到复核任务")
        if detail.get("engine") == "b":
            self.b.validate_review(detail, task, decision)
            return self.store.review(run_id, task_id, decision)
        action = decision["action"]
        if action in ("REMAP", "CORRECT_VALUE", "EXCLUDE_FROM_SCORING") and task["record_uid"] is None:
            raise ContractError("指标组任务需先补充/核对上游记录；请暂缓或注明依据保留")
        if action == "CORRECT_VALUE" and decision.get("proposed_value") is None:
            raise ContractError("修订值不能为空，真实 0 可以填写")
        if action == "REMAP":
            indicator = next(i for i in detail["bundle"]["catalogue"]["indicators"] if i["indicator_id"] == task["indicator_id"])
            if decision.get("proposed_variable") not in indicator["variables"]:
                raise ContractError("请选择该指标闭集中的候选变量；跨指标改映射需回到上游处理")
        if action != "CORRECT_VALUE" and decision.get("proposed_value") is not None:
            raise ContractError("此操作不接受修订值")
        if action != "REMAP" and decision.get("proposed_variable") is not None:
            raise ContractError("此操作不接受候选变量")
        return self.store.review(run_id, task_id, decision)

    def export(self, run_id):
        detail = self.detail(run_id)
        if detail.get("engine") == "b":
            return self.b.export(detail)
        if detail["status"] == "RUNNING":
            raise Conflict("运行尚未完成，请稍后导出")
        result = detail["result"] or {}
        files = {"input.bundle.json": canonical(detail["bundle"]),
                 "audit_report.json": canonical({k: v for k, v in result.items() if k not in ("findings", "model_calls")}),
                 "review_tasks.json": canonical(detail["tasks"]), "review_events.jsonl": "\n".join(canonical(e) for e in detail["events"]),
                 "audit_findings.jsonl": "\n".join(canonical(f) for f in result.get("findings", [])),
                 "model_calls.jsonl": "\n".join(canonical(c) for c in result.get("model_calls", [])),
                 "s5_handoff.json": canonical(detail["handoff"]),
                 "README.txt": "本包只包含 S4 与人工复核结果。候选修订须回上游生成新版本并重审；READY_FOR_S5 仍需 S5 验证。\n模拟数据不得用于评分。\n"}
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["finding_id", "record_uid", "code", "severity", "reason", "evidence_ids"])
        for finding in result.get("findings", []):
            cells = [finding.get(k, "") for k in ("finding_id", "record_uid", "code", "severity", "reason")]
            cells.append(";".join(finding["evidence_ids"]))
            writer.writerow(["'" + str(c) if str(c).startswith(("=", "+", "-", "@", "\t", "\r")) else c for c in cells])
        files["audit_findings.csv"] = "\ufeff" + output.getvalue()
        files["manifest.json"] = canonical({"run_id": run_id, "status": detail["status"], "error": detail["error"],
                                             "is_demo": detail["is_demo"], "provider": detail["provider"],
                                             "pdf_sha256": detail["pdf_sha256"],
                                             "input_sha256": digest(detail["bundle"]), "exported_at": now(),
                                             "file_sha256": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()}})
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, text in files.items():
                archive.writestr(name, text.encode("utf-8"))
            for name, schema in SCHEMAS.items():
                archive.writestr(f"schemas/{name}.schema.json", canonical({"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}).encode())
        return buffer.getvalue()
