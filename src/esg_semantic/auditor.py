"""Developer B entry points. A models are consumed, never redefined here.

Without A's binding, only explicit fixture/local-delivery modes can run.
See docs/B_开发文档.md and docs/使用说明.md.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from math import ceil
from pathlib import Path
import tempfile
import time
import uuid
import hashlib

from .audit.common import canonical, mapping, policy_values, problem, resources, sha, utcnow
from .audit.context import build_group_prompt, parse_audit_response, group_context
from .audit.indexes import InputError, build_indexes
from .audit.events import emit
from .audit.integration import current, finish, validate_as
from .audit.postprocess import postprocess, derive_review_status
from .audit.prechecks import candidates_from_hints, hint, precheck, record_group_checks, report_checks
from .audit.sampling import batches
from .audit.visual import compare_visual, read_region_with_vlm, render_pdf_region, should_visual_check


def _failure(document, policy, run, code, message):
    return {"schema_version": "3.0", "audit_run_id": run.get("audit_run_id", run.get("run_id", "unknown")),
            "pid": document.get("pid"), "review_policy_version": policy.get("review_policy_version"),
            "record_results": [], "findings": [], "coverage": {}, "stats": {}, "status": "FAILED",
            "problems": [problem(code, message, level="error")], "created_at": run.get("created_at") or utcnow(),
            "precheck_counts": {}, "truncated": False}


class SemanticAuditor:
    def __init__(self, llm, policy, *, catalogue=None):
        self.llm = llm
        self.policy = policy_values(policy)
        self.catalogue = deepcopy(catalogue if catalogue is not None else current().catalogue)

    def audit_group(self, group_key, records, evidence_by_id, decisions_by_evidence, document, *, run):
        if any((mapping(r).get("pid"), mapping(r).get("indicator_id")) != group_key for r in records):
            raise ValueError("audit_group only accepts its stated (pid, indicator_id)")
        return self.audit_report(records, list(evidence_by_id.values()), list(decisions_by_evidence.values()),
                                 [], None, document, run=run)

    def audit_report(self, records, evidence, decisions, filter_sample, coverage, document, *, run):
        input_refs = (records, evidence, decisions, filter_sample, coverage, document)
        def input_fingerprint():
            return sha([[mapping(item) for item in collection] for collection in input_refs[:4]] +
                       [mapping(input_refs[4]) if input_refs[4] is not None else None, mapping(input_refs[5])])
        try:
            source_fingerprint = input_fingerprint()
        except Exception:
            source_fingerprint = None
        try:
            document, run = mapping(document), mapping(run)
        except (ValueError,TypeError):
            return _failure({},self.policy,{},"ERR_CONTRACT_INVALID","报告上下文或运行上下文不是有效数据对象。")
        run.setdefault("audit_run_id", run.get("run_id") or "ar_" + uuid.uuid4().hex)
        run.setdefault("created_at", utcnow())
        binding = current()
        if not binding.fixture and not callable(binding.validator):
            return _failure(document, self.policy, run, "ERR_CONTRACT_NOT_READY", "A公共契约未绑定；仅可显式运行fixture模式。")
        if source_fingerprint is None:
            return _failure(document, self.policy, run, "ERR_CONTRACT_INVALID", "输入不能无损表示为有限值JSON，未调用模型。")
        emit("audit_start", run_id=run["audit_run_id"], pid=document.get("pid"),
             fixture=binding.fixture, contract_fingerprint=binding.contract_fingerprint,
             review_policy_version=self.policy["review_policy_version"], prompt_sha256=self.policy["prompt_sha256"])
        try:
            rows = [deepcopy(mapping(r)) for r in records]
            pool = [deepcopy(mapping(e)) for e in evidence]
            filters = [deepcopy(mapping(d)) for d in decisions]
            samples = [deepcopy(mapping(s)) for s in filter_sample]
            coverage = deepcopy(mapping(coverage)) if coverage is not None else None
            if not {"pid", "report_year", "page_count", "pdf_path"}.issubset(document):
                raise InputError("document_context lacks required fields")
            if not isinstance(document["pid"], str) or not document["pid"]:
                raise InputError("document pid is invalid")
            if any(r.get("pid") != document["pid"] for r in rows):
                raise InputError("Cross-report records are not accepted")
            if any(e.get("pid") not in (None, document["pid"]) for e in pool):
                raise InputError("Cross-report evidence is not accepted")
            validate_as("DocumentContext", document)
            for name, collection in (("EvidenceRecord", pool), ("FilterDecision", filters), ("FilterAuditSample", samples)):
                for item in collection:
                    validate_as(name, item)
            if coverage is not None:
                validate_as("CoverageSignal", coverage)
            indexes, decision_index, problems = build_indexes(rows, pool, filters)
        except Exception as exc:
            return _failure(document, self.policy, run, getattr(exc, "code", "ERR_CONTRACT_INVALID"),
                            "输入结构、身份或公共契约校验失败；未调用模型。")
        if binding.fixture:
            problems.append(problem("CONTRACT_UNVERIFIED_FIXTURE_ONLY", "仅验证B逻辑；未与A公共模型或真实产物联合验收。"))
        if binding.validation_scope == "local_delivery":
            problems.append(problem("LOCAL_DELIVERY_CONTRACT_ONLY", "基于A实物的本地适配校验，未验收A正式Schema；不是合成数据。"))
            record_years = sorted({r.get("report_year") for r in rows if type(r.get("report_year")) is int})
            if any(year != document.get("report_year") for year in record_years):
                problems.append(problem("DELIVERY_REPORT_YEAR_CONFLICT", "文档报告年与记录报告期有差异，保留原值与来源，需核对公告年及披露期。", document_report_year=document.get("report_year"), record_report_years=record_years))
        if binding.execution_mode == "rules_only":
            problems.append(problem("RULES_ONLY_NO_MODEL", "仅执行确定性规则；未调用语义或视觉模型，无问题不等于确认。"))
        if coverage is None:
            problems.append(problem("COVERAGE_NOT_PROVIDED", "A-MISSING-KEY已禁用。"))
        if not samples:
            problems.append(problem("FILTER_SAMPLE_NOT_PROVIDED", "没有有效给定过滤样本；不推断过滤误删。"))
        codebook = resources()["codebook"]
        extra_checks = record_group_checks(rows, indexes["resolved"])
        hints, raw_findings = {}, []
        for record in rows:
            uid = record["record_id"]
            ev = indexes["resolved"][uid]
            decision = decision_index.get(ev["evidence_version_id"]) if ev else None
            if decision:
                record["_effective_filter_status"] = decision["status"]
            checks = precheck(record, ev, decision, document) + extra_checks[uid]
            try:
                validate_as("ClassificationRecord", {k: v for k, v in record.items() if not k.startswith("_")})
            except Exception:
                checks[0]["value"] = False
            record["_insufficient"] = (not checks[0]["value"] or ev is None or
                                        any(h["check"] in ("D6", "PDF_READ") and h["value"] in (False, "unknown") for h in checks))
            if binding.record_format == "profile_delivery_v1" and (record.get("classification_status") != "ASSIGNED" or not record.get("profile_field")):
                record["_insufficient"] = True
                problems.append(problem("DELIVERY_CLASSIFICATION_UNRESOLVED", "上游未完成闭集归类或上下文不足，保留待复核。", record_id=uid))
            if binding.record_format == "profile_delivery_v1" and record.get("profile_field") == "revenue_currency":
                record["_insufficient"] = True
                problems.append(problem("DELIVERY_CURRENCY_TEXT_ONLY", "币种是文本属性，当前数值审查不将其判为数值提取错误。", record_id=uid))
                checks[:] = [h for h in checks if h["check"] != "D2"]
            if ev and ev.get("source_ref", {}).get("independent_binding_verified") is False:
                # Generated transcription is not an independent confirmation of itself.
                record["_insufficient"] = True
                if checks[0]["value"] and record.get("classification_status") == "ASSIGNED":
                    record["_delivery_semantic_eligible"] = True
                problems.append(problem("INDEPENDENT_SOURCE_REVIEW_REQUIRED", "提取候选尚未独立核对原页；可审查归类，但不得仅凭模型转录确认原始数值。", record_id=uid))
            if any(h["check"] == "D3" and h["value"] is None for h in checks):
                record["_insufficient"] = True
                problems.append(problem("UNIT_CONVERSION_UNVERIFIED", "声明的归一算术一致，但缺少该单位转换的正式依据。", record_id=uid))
                if binding.record_format == "profile_delivery_v1" and checks[0]["value"] and ev is not None and record.get("classification_status") == "ASSIGNED" and record.get("profile_field") != "revenue_currency" and all(h["value"] is True for h in checks if h["check"] == "D6"):
                    # Can assess classification independently, but cannot clear the
                    # outstanding conversion uncertainty even after a model response.
                    record["_delivery_semantic_eligible"] = True
            if decision and record.get("filter_status", decision["status"]) != decision["status"]:
                record["_insufficient"] = True
                problems.append(problem("FILTER_STATUS_CONFLICT", "记录与过滤决策状态不一致。", record_id=uid))
            if record["_insufficient"]:
                problems.append(problem("RECORD_INPUT_INSUFFICIENT", "单记录输入或溯源不足，保留为INSUFFICIENT。", record_id=uid))
            hints[uid] = checks
            raw_findings.extend(candidates_from_hints(record, ev, checks, codebook))
        report_hints, sample_problems, sample_count = report_checks(samples, coverage, indexes["evidence"], rows, self.policy)
        problems.extend(problem(code, "过滤样本的证据版本无效。", evidence_version_id=eid) for code, eid in sample_problems)
        raw_findings.extend(candidates_from_hints(None, None, report_hints, codebook))
        allowed_missing = {h["details"]["indicator_id"]: h["evidence_ids"] for h in report_hints if h["check"] == "D9"}
        # A supplied sample permits review even if D8 alone did not prove an error.
        allowed_filter = {s["evidence_version_id"] for s in samples[:self.policy["filter_sample_max"]]
                          if s.get("evidence_version_id") in indexes["evidence"]}
        groups, selected, skipped = batches(rows, self.policy)
        # Report-level signals get their own bounded model unit, including empty-record reports.
        if allowed_filter or allowed_missing:
            report_group = ((document["pid"], None), [], False)
            if allowed_missing:
                groups.insert(0, report_group)
            else:
                groups.append(report_group)
        budget = self.policy["max_model_calls"]
        if budget is None:
            budget = ceil(len(groups) / 2) + 2
        calls, visual_checked = 0, 0
        truncated = False
        for group, group_rows, split in groups:
            if binding.execution_mode == "rules_only":
                continue
            emit("audit_group_start", run_id=run["audit_run_id"], pid=document["pid"], indicator_id=group[1])
            ids = {r["record_id"] for r in group_rows}
            record_evidence = {indexes["resolved"][r["record_id"]]["evidence_version_id"]: indexes["resolved"][r["record_id"]] for r in group_rows}
            if group[1] is None:
                report_ids = allowed_filter | {eid for values in allowed_missing.values() for eid in values}
                record_evidence = {eid: indexes["evidence"][eid] for eid in report_ids}
            group_samples = [s for s in samples[:self.policy["filter_sample_max"]] if s.get("evidence_version_id") in record_evidence] if group[1] is None else []
            context_coverage = coverage if group[1] is None else None
            if split:
                problems.append(problem("GROUP_SPLIT", "同组超过上限，已切分；跨记录检查先在完整组执行。", indicator_id=group[1]))
            # D13 is optional, shares the same outbound-call budget, and cannot consume key semantic capacity.
            for record in group_rows:
                uid, ev = record["record_id"], indexes["resolved"][record["record_id"]]
                trace = next((h["value"] for h in hints[uid] if h["check"] == "D11"), "MISSING")
                visual_result = "not_selected"
                reader = binding.visual_reader
                if trace == "FULL" and should_visual_check(record, self.policy):
                    if not callable(reader):
                        visual_result = "unknown"
                        problems.append(problem("VISUAL_CLIENT_UNAVAILABLE", "区域视觉客户端未提供，未声称核对过PDF。", record_id=uid))
                    elif budget - calls > len(groups):
                        with tempfile.TemporaryDirectory(prefix="b-audit-region-") as temp:
                            renderer = binding.renderer or render_pdf_region
                            image = renderer(document["pdf_path"], ev["page_no"], ev["bbox"], out_dir=Path(temp))
                            if image is None:
                                visual_result = "unknown"
                            else:
                                calls += 1
                                visual_started=time.monotonic()
                                try:
                                    observed = reader(Path(image), record=deepcopy(record), max_attempts=1)
                                    visual_result = compare_visual(observed, record)
                                    visual_checked += 1
                                except Exception:
                                    visual_result = "unknown"
                                emit("audit_visual_end",run_id=run["audit_run_id"],pid=document["pid"],
                                     model_call_id=f"mdl_{run['audit_run_id']}_{calls:04d}",record_id=uid,
                                     latency_ms=round((time.monotonic()-visual_started)*1000),
                                     status="SUCCEEDED" if visual_result is True or visual_result is False else "PARTIAL")
                        if visual_result in (None, "unknown"):
                            problems.append(problem("VISUAL_CHECK_FAILED", "视觉核对未完成，不视为一致。", record_id=uid))
                    else:
                        visual_result = "unknown"
                        problems.append(problem("VISUAL_BUDGET_RESERVED", "优先保留语义审查预算，区域视觉未执行。", record_id=uid))
                if visual_result is None or visual_result == "unknown":
                    record["_insufficient"]=True
                visual_hint = hint("D13", visual_result, code="A-EXTRACT-VALUE" if visual_result is False else None,
                                   severity="BLOCK", reason="PDF区域核对与记录值、单位或年度不一致。", evidence_ids=[ev["evidence_version_id"]])
                hints[uid].append(visual_hint)
                raw_findings.extend(candidates_from_hints(record, ev, [visual_hint], codebook))
            if calls >= budget or self.llm is None:
                error = "ERR_MODEL_BUDGET_EXCEEDED" if calls >= budget else "ERR_MODEL_UNAVAILABLE"
                truncated |= calls >= budget
                for record in group_rows:
                    record["_insufficient"] = True
                problems.append(problem(error, "本组模型审查未执行。", level="error", indicator_id=group[1]))
                emit("audit_group_end",run_id=run["audit_run_id"],pid=document["pid"],level="ERROR",error_code=error,status="PARTIAL",indicator_id=group[1])
                continue
            group_decisions = [decision_index[eid] for eid in record_evidence if eid in decision_index]
            batch_hints = {uid: hints[uid] for uid in ids}
            prompt = build_group_prompt(group, group_rows, list(record_evidence.values()), group_decisions, document,
                                        context_coverage, batch_hints, catalogue=self.catalogue, filter_sample=group_samples)
            if len(prompt)>180_000:
                for record in group_rows: record["_insufficient"]=True
                problems.append(problem("ERR_MODEL_CONTEXT_LIMIT","上下文超过本地上限，未发送模型。",level="error",indicator_id=group[1]))
                emit("audit_group_end",run_id=run["audit_run_id"],pid=document["pid"],level="ERROR",error_code="ERR_MODEL_CONTEXT_LIMIT",status="PARTIAL",indicator_id=group[1])
                continue
            call_id = f"mdl_{run['audit_run_id']}_{calls + 1:04d}"
            receipt = {"model_call_id": call_id, "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(), "tokens": None, "latency_ms": None,
                       "record_ids": sorted(ids), "indicator_id": group[1]}
            started = time.monotonic()
            try:
                calls += 1
                # B-private bridge: exactly one invocation. A owns any retry and must honor max_attempts.
                response = self.llm(prompt, max_attempts=budget - calls + 1)
                if isinstance(response, dict) and "text" in response:
                    attempts = response.get("attempts", 1)
                    if type(attempts) is not int or attempts < 1:
                        raise ValueError("Client violated remaining call budget")
                    calls += attempts - 1
                    if calls > budget:
                        raise InputError("Client exceeded call budget", "ERR_MODEL_BUDGET_EXCEEDED")
                    tokens = response.get("tokens")
                    receipt["tokens"] = tokens if type(tokens) is int and tokens >= 0 else None
                    response = response["text"]
                found, parse_errors = parse_audit_response(response, group, record_evidence)
                if "ERR_MODEL_OUTPUT_INVALID" in parse_errors:
                    raise ValueError("Invalid model JSON")
                for code in parse_errors:
                    problems.append(problem(code, "模型JSON经过一次有界救援解析。", indicator_id=group[1]))
                for candidate in found:
                    illegal_private=any(key.startswith("_") for key in candidate)
                    candidate = {**candidate, "_source": "MODEL", "_call_id": call_id,
                                 "_allowed_evidence": list(record_evidence), "_batch_record_ids": sorted(ids),
                                 "_illegal_model_private":illegal_private}
                    if candidate.get("record_id") is not None and candidate.get("record_id") not in {r["record_id"] for r in group_rows}:
                        candidate["_invalid_batch_record"] = True
                    raw_findings.append(candidate)
                for record in group_rows:
                    record["_reviewed"] = True
                    ev = indexes["resolved"][record["record_id"]]
                    if len(ev.get("content", "")) > 1200:
                        record["_insufficient"] = True
                        problems.append(problem("EVIDENCE_CONTEXT_TRUNCATED", "模型片段截断，不能声称完整审查。", record_id=record["record_id"]))
                receipt["status"] = "SUCCEEDED"
            except TimeoutError:
                receipt.update(status="FAILED", error_code="ERR_MODEL_TIMEOUT")
                for record in group_rows: record["_insufficient"] = True
                problems.append(problem("ERR_MODEL_TIMEOUT", "模型组执行失败，未进行Agent级重试。", level="error", indicator_id=group[1]))
            except InputError as exc:
                truncated = True
                receipt.update(status="FAILED", error_code=exc.code)
                for record in group_rows: record["_insufficient"] = True
                problems.append(problem(exc.code, "客户端重试超出给定预算，已停止后续调用。", level="error", indicator_id=group[1]))
            except Exception:
                receipt.update(status="FAILED", error_code="ERR_MODEL_OUTPUT_INVALID")
                for record in group_rows: record["_insufficient"] = True
                problems.append(problem("ERR_MODEL_OUTPUT_INVALID", "模型调用/输出失败；原始异常不写日志。", level="error", indicator_id=group[1]))
            finally:
                receipt["latency_ms"] = round((time.monotonic() - started) * 1000)
                emit("audit_group_end", run_id=run["audit_run_id"], pid=document["pid"], **receipt)
        findings, results, stats = postprocess(raw_findings, rows, self.policy, evidence_by_id=indexes["evidence"], hints_by_record=hints,
                                              run=run, document=document, allowed_missing=allowed_missing, allowed_filter=allowed_filter,
                                              catalogue=self.catalogue)
        stats.update(model_calls=calls, visual_checked=visual_checked, filter_samples_reviewed=sample_count)
        for counter in ("invalid_finding_code","dangling_evidence","invalid_finding_shape","invalid_record_reference","invalid_proposal"):
            if stats[counter]:
                emit("audit_finding_dropped", run_id=run["audit_run_id"], pid=document["pid"], counter=counter, count=stats[counter])
        for item in results:
            if item["review_status"] == "INSUFFICIENT" and not any(p.get("record_id") == item["record_id"] for p in problems):
                problems.append(problem("RECORD_REVIEW_INSUFFICIENT", "审查结果不足，未自动确认。", record_id=item["record_id"]))
            if indexes["records"][item["record_id"]].get("indicator_id") in self.policy["key_indicators"] and item["review_status"]=="INSUFFICIENT":
                problems.append(problem("ERR_AUDIT_KEY_INSUFFICIENT","关键指标审查未完成，需人工确认。",record_id=item["record_id"],level="error"))
        key_missing = sum(r.get("indicator_id") in self.policy["key_indicators"] and r["record_id"] in skipped for r in rows)
        coverage_out = {k: stats[k] for k in ("records_total", "records_reviewed", "records_not_reviewed", "filter_samples_reviewed", "visual_checked")}
        coverage_out.update(key_not_reviewed=key_missing,
                            v1_records=sum(ev is not None and ev.get("source_format") == "v1_numeric_blocks" for ev in indexes["resolved"].values()),
                            v2_records=sum(ev is not None and ev.get("source_format") == "v2" for ev in indexes["resolved"].values()))
        precheck_counts = dict(Counter(h["check"] for values in hints.values() for h in values))
        precheck_counts.update(D8=sample_count, D9=len(self.policy["key_indicators"]) if coverage is not None else 0)
        status = "PARTIAL" if binding.fixture or binding.validation_scope == "local_delivery" or binding.execution_mode == "rules_only" or truncated or any(r["review_status"] == "INSUFFICIENT" for r in results) or any(p["level"] == "error" for p in problems) else "SUCCEEDED"
        try:
            unchanged = source_fingerprint is not None and source_fingerprint == input_fingerprint()
        except Exception:
            unchanged = False
        if not unchanged:
            status = "FAILED"
            problems.append(problem("ERR_AUDIT_MUTATED_UPSTREAM", "输入发生变化。", level="error"))
        outcome = {"schema_version": "3.0", "audit_run_id": run["audit_run_id"], "pid": document["pid"],
                   "review_policy_version": self.policy["review_policy_version"], "record_results": results, "findings": findings,
                   "coverage": coverage_out, "stats": stats, "status": status, "problems": problems, "created_at": run["created_at"],
                   "precheck_counts": precheck_counts, "truncated": truncated}
        if binding.validation_scope == "local_delivery" or binding.execution_mode == "rules_only":
            outcome.update(validation_scope=binding.validation_scope, execution_mode=binding.execution_mode)
        try:
            for finding in findings: validate_as("AuditFinding", finding)
            for result in results: validate_as("AuditRecordResult", result)
            emit("audit_end", run_id=run["audit_run_id"], pid=document["pid"], status=outcome["status"], model_calls=calls)
            return finish(outcome)
        except Exception:
            return _failure(document, self.policy, run, "ERR_CONTRACT_INVALID", "输出不符合注入的A公共契约，禁止正式发布。")


def audit_classification(records, evidence, decisions, filter_sample, coverage, document, *, policy, run, llm):
    return SemanticAuditor(llm, policy).audit_report(records, evidence, decisions, filter_sample, coverage, document, run=run)


def run_audit_file(pid_dir, *, policy_path, llm, run_id=None, dry_run=False):
    from .audit.files import run_files
    return run_files(pid_dir, policy_path=policy_path, llm=llm, run_id=run_id, dry_run=dry_run)
