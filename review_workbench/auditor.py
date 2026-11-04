"""Read-only S4 engine, independent of transport and persistence."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import time
import re
from decimal import Decimal, InvalidOperation

from .contracts import ContractError, MODEL_RESPONSE, FINDING, OUTCOME, digest, validate, validate_bundle
from .providers import ModelFailure, PROMPT_VERSION, SYSTEM_PROMPT


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def make_batches(bundle):
    for indicator in sorted(bundle["catalogue"]["indicators"], key=lambda i: i["indicator_id"]):
        records = sorted((r for r in bundle["records"] if r["indicator_id"] == indicator["indicator_id"]),
                         key=lambda r: (r["year"] or 0, r["breakdown"] or "", r["record_uid"]))
        if records:
            for start in range(0, len(records), 20):
                yield indicator, records[start:start + 20], len(records) > 20
        elif bundle["review_policy"]["check_missing_key"]:
            yield indicator, [], False


def make_context(bundle, indicator, records, split):
    refs = {r["evidence_id"] for r in records if r["evidence_id"]}
    keywords = [k.lower() for k in indicator.get("keywords", [])]
    related = [e for e in bundle["evidence"] if e["evidence_id"] in refs]
    extras = [e for e in bundle["evidence"] if e["evidence_id"] not in refs and
              (not records or any(k in e["content"].lower() for k in keywords))]
    selected = related + sorted(extras, key=lambda e: e["evidence_id"])
    omitted = len(selected) > 40
    evidence = []
    for item in selected[:40]:
        normalized = {k: item[k] for k in ("evidence_id", "page_no", "kind", "unit_hint", "col_header", "row_header", "tier") if k in item}
        source = item.get("source_ref", {})
        normalized["source_ref"] = {"quality_hint": str({k: source[k] for k in ("verdict", "review_flags") if k in source})[:100]}
        evidence.append({**normalized, "content": item["content"][:1200], "truncated": len(item["content"]) > 1200})
    clean_records = []
    for record in records:
        normalized = {k: v for k, v in record.items() if k != "extensions"}
        if normalized.get("classification_reason"):
            normalized["classification_reason"] = normalized["classification_reason"][:300]
        clean_records.append(normalized)
    return {"report_context": {k: v for k, v in bundle["report_context"].items() if k != "extensions"}, "indicator_group": {
        **indicator, "records": clean_records, "evidence": evidence}, "review_policy": bundle["review_policy"],
        "coverage": {"split_group": split, "evidence_omitted": omitted,
                     "catalogue_version": bundle["catalogue"]["version"]}}


def check_reply(reply, context):
    validate(reply, MODEL_RESPONSE)
    group = context["indicator_group"]
    records = {r["record_uid"]: r for r in group["records"]}
    evidence = {e["evidence_id"]: e for e in group["evidence"]}
    ids = [v["record_uid"] for v in reply["record_verdicts"]]
    if len(ids) != len(set(ids)) or set(ids) != set(records):
        raise ContractError("模型必须逐条且完整返回本批记录的审查结论")
    for finding in reply["findings"]:
        uid = finding["record_uid"]
        if uid is not None and uid not in records:
            raise ContractError("finding 引用了本批不存在的记录")
        if uid is None and finding["code"] not in ("A-MISSING-KEY", "A-CLOSED-SET-GAP"):
            raise ContractError("此 finding 码必须关联记录")
        if any(eid not in evidence for eid in finding["evidence_ids"]):
            raise ContractError("finding 引用了不存在的证据")
        if finding.get("proposed_variable") and finding["proposed_variable"] not in group["variables"]:
            raise ContractError("候选变量超出闭集")
        if finding["code"] == "A-CLOSED-SET-GAP" and finding["severity"] != "INFO":
            raise ContractError("闭集缺口只能为 INFO")
        span = finding.get("evidence_span")
        if span and not any(span in evidence[eid]["content"] for eid in finding["evidence_ids"]):
            raise ContractError("证据片段并非引用证据原文")
        if finding.get("proposed_value") is not None or finding["proposed_action"] == "CORRECT_VALUE":
            if finding.get("proposed_value") is None or not span:
                raise ContractError("候选修正值必须附原文片段")
            tokens = re.findall(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?", span)
            try:
                if Decimal(str(finding["proposed_value"])) not in [Decimal(t.replace(",", "")) for t in tokens]:
                    raise ContractError("候选值未直接出现在原文片段；S4 不得计算换算后的新值")
            except InvalidOperation:
                raise ContractError("候选值无法在原文核验") from None
    return reply


def audit(bundle, provider, run_id, progress=None):
    validate_bundle(bundle)
    before = digest(bundle)
    original = deepcopy(bundle)
    policy = bundle["review_policy"]
    findings, verdicts, groups, calls, issues = [], [], [], [], []
    batches = list(make_batches(bundle))
    for index, (indicator, records, split) in enumerate(batches):
        context = make_context(bundle, indicator, records, split)
        response, error, call_id = None, None, None
        for attempt in range(3):
            if len(calls) >= policy["max_model_calls"]:
                error = "MODEL_BUDGET_EXHAUSTED"
                break
            call_id = f"{run_id}_call_{len(calls) + 1:04d}"
            receipt = {"model_call_id": call_id, "request_sha256": digest(context),
                       "request": context, "started_at": now(), "attempt": attempt + 1}
            calls.append(receipt)
            try:
                result = provider.review(deepcopy(context), policy["max_output_tokens"])
                receipt["response"] = deepcopy(result.data)
                receipt["usage"] = result.usage
                response = deepcopy(check_reply(result.data, context))
                receipt["status"] = "SUCCEEDED"
                break
            except ModelFailure as exc:
                error = exc.code
                receipt.update(status="FAILED", error=error)
                if not exc.retryable or attempt == 2:
                    break
                time.sleep(.2 * (attempt + 1))
            except (ContractError, TypeError, KeyError, ValueError):
                error = "MODEL_CONTRACT_INVALID"
                receipt.update(status="FAILED", error=error)
                break
            finally:
                receipt["finished_at"] = now()
        evidence = {e["evidence_id"]: e for e in context["indicator_group"]["evidence"]}
        batch_findings = []
        if response is not None:
            seen = set()
            # Stronger duplicate findings take precedence; never let INFO erase a BLOCK.
            ordered = sorted(response["findings"], key=lambda f: ({"BLOCK": 0, "WARN": 1, "INFO": 2}[f["severity"]], -f["confidence"]))
            for item in ordered:
                key = (item["record_uid"], item["code"])
                if key in seen:
                    continue
                seen.add(key)
                if item["severity"] == "BLOCK" and item["confidence"] < policy["block_confidence"]:
                    item["severity"] = "WARN"
                finding = {**item, "finding_id": f"af_{run_id}_{len(findings) + 1:04d}",
                           "audit_run_id": run_id, "pid": bundle["report_context"]["pid"],
                           "indicator_id": indicator["indicator_id"], "review_policy_version": policy["policy_version"],
                           "model_call_id": call_id}
                validate(finding, FINDING)
                findings.append(finding)
                batch_findings.append(finding)
        by_uid = {v["record_uid"]: v for v in response["record_verdicts"]} if response else {}
        batch_partial = response is None or context["coverage"]["evidence_omitted"] or split
        for record in records:
            uid = record["record_uid"]
            own = [f for f in batch_findings if f["record_uid"] == uid]
            verdict = by_uid.get(uid, {}).get("verdict", "INSUFFICIENT")
            reason = by_uid.get(uid, {}).get("reason", error or "未取得有效模型结果")
            source = evidence.get(record["evidence_id"])
            uncertain = (not source or source["truncated"] or record["provenance_source"] != "evidence_id"
                         or record["classification_status"] != "ASSIGNED" or record["variable"] not in indicator["variables"]
                         or record["value"] is None or record["unit"] is None or record["year"] is None
                         or not record["breakdown"] or record["source_page"] != (source or {}).get("page_no")
                         or context["coverage"]["evidence_omitted"] or split)
            if uncertain:
                verdict, reason = "INSUFFICIENT", "证据、归类或范围未完整确认；需人工核对（含截断/分批覆盖限制）"
                batch_partial = True
            elif own and verdict == "CONFIRMED":
                verdict, reason = "SUSPECT", "存在审查发现，不能自动确认为无问题"
            elif verdict in ("SUSPECT", "REJECTED") and not own:
                verdict, reason = "INSUFFICIENT", "模型未提供支持该结论的审查发现"
                batch_partial = True
            elif verdict == "REJECTED" and not any(f["severity"] == "BLOCK" for f in own):
                verdict, reason = "SUSPECT", "发现尚不足以构成高置信阻断，保留疑点供人工复核"
            verdicts.append({"record_uid": uid, "indicator_id": indicator["indicator_id"], "verdict": verdict,
                             "reason": reason, "finding_ids": [f["finding_id"] for f in own]})
        if error and response is None:
            issues.append({"indicator_id": indicator["indicator_id"], "code": error, "batch": index + 1})
        if not records and (response is None or context["coverage"]["evidence_omitted"]):
            issues.append({"indicator_id": indicator["indicator_id"], "code": "MISSING_CHECK_INCOMPLETE", "batch": index + 1})
        if not records and (not evidence or any(e["truncated"] for e in evidence.values())):
            batch_partial = True
            issues.append({"indicator_id": indicator["indicator_id"], "code": "MISSING_CHECK_EVIDENCE_INCOMPLETE", "batch": index + 1})
        batch_verdicts = [v for v in verdicts if v["record_uid"] in {r["record_uid"] for r in records}]
        groups.append({"audit_run_id": run_id, "pid": bundle["report_context"]["pid"],
                       "indicator_id": indicator["indicator_id"], "batch": index + 1,
                       "record_verdicts": batch_verdicts, "policy_version": policy["policy_version"],
                       "findings": [f["finding_id"] for f in batch_findings],
                       "stats": {"records": len(records), **dict(Counter(v["verdict"] for v in batch_verdicts))},
                       "status": "PARTIAL" if batch_partial else "SUCCEEDED", "coverage": context["coverage"]})
        if progress:
            progress(index + 1, len(batches))
    assert before == digest(bundle) == digest(original), "S4 must never mutate input"
    for group in groups:
        validate(group, OUTCOME)
    return {"audit_run_id": run_id, "pid": bundle["report_context"]["pid"], "findings": findings,
            "record_verdicts": verdicts, "groups": groups, "issues": issues, "model_calls": calls,
            "status": "PARTIAL" if any(g["status"] != "SUCCEEDED" for g in groups) else "SUCCEEDED",
            "stats": {**dict(Counter(v["verdict"] for v in verdicts)),
                      **dict(Counter(f["severity"] for f in findings)), "records": len(verdicts), "findings": len(findings)},
            "manifest": {"schema_version": bundle["schema_version"], "input_sha256": before,
                         "catalogue_version": bundle["catalogue"]["version"], "policy_version": policy["policy_version"],
                         "water_rules_version": policy["water_rules_version"], "provider": provider.name,
                         "model": provider.model, "prompt_version": PROMPT_VERSION, "prompt_sha256": digest(SYSTEM_PROMPT),
                         "sampling": "ALL_IMPORTED_INDICATORS", "model_calls": len(calls), "created_at": now()}}
