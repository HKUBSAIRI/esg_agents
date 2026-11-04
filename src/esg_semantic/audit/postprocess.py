"""Evidence-bound finding validation and code-derived record statuses."""
from collections import Counter
from copy import deepcopy
from jsonschema import Draft202012Validator

from .common import number, number_tokens, resources, utcnow
from .visual import valid_bbox
from .integration import current

STATS = ("records_total records_reviewed records_not_reviewed filter_samples_reviewed findings block warn info "
         "block_downgraded invalid_finding_code dangling_evidence duplicated_findings missing_key_without_signal "
         "overdrop_without_sample model_calls extraction_findings classification_findings visual_checked trace_missing "
         "invalid_finding_shape unsupported_block invalid_record_reference invalid_proposal").split()


def derive_review_status(findings, record):
    """record flags are internal copies, never added to A's actual record."""
    if record.get("_insufficient"):
        return "INSUFFICIENT"
    if any(f["severity"] == "BLOCK" for f in findings):
        return "REJECTED"
    if any(f["code"] == "A-CONTEXT-INSUFFICIENT" for f in findings):
        return "INSUFFICIENT"
    if any(f["severity"] == "WARN" for f in findings):
        return "SUSPECT"
    if not record.get("_reviewed"):
        return "NOT_REVIEWED"
    return "CONFIRMED"


def _prerequisite(code, checks, record, group_counts):
    if code == "A-UNIT-MISMATCH": return checks.get("D4") is False
    if code == "A-YEAR-MISMATCH": return checks.get("D5") is False
    if code == "A-CROSS-RECORD": return checks.get("D10") is True
    if code == "A-DUPLICATE-UNMERGED": return checks.get("D7") is True
    if code == "A-NORMALIZATION-INVALID": return checks.get("D3") is False
    if code == "A-EVIDENCE-WEAK": return checks.get("D2") == "missing" or checks.get("D6") is False
    if code == "A-EXTRACT-VALUE": return checks.get("D13") is False or checks.get("D2") == "missing"
    if code == "A-EXTRACT-PAGE": return checks.get("D14") is False
    if code == "A-TRACE-MISSING": return checks.get("D11") == "MISSING"
    if code == "A-CLOSED-SET-GAP": return record.get("classification_status") in ("UNMAPPED", "OUT_OF_CLOSED_SET")
    if code == "A-BOUNDARY-MISSING":
        if current().record_format == "profile_delivery_v1" and record.get("scope_type") in ("GROUP", "COMPANY"):
            return False
        return not record.get("breakdown") and group_counts[(record.get("indicator_id"), record.get("year"))] >= 2
    return True


def postprocess(findings, records, policy, *, evidence_by_id=None, hints_by_record=None,
                run=None, document=None, allowed_missing=None, allowed_filter=None, catalogue=None):
    settings = resources()
    codebook = settings["codebook"]
    allowed_fields = set(settings["schema"]["properties"]["findings"]["items"]["properties"])
    required = set(settings["schema"]["properties"]["findings"]["items"]["required"])
    model_validator = Draft202012Validator(settings["schema"]["properties"]["findings"]["items"])
    evidence_by_id, hints_by_record = evidence_by_id or {}, hints_by_record or {}
    records_by_id = {r["record_id"]: r for r in records}
    group_counts = Counter()
    for row in records:
        key = (row.get("indicator_id"), row.get("year"))
        try:
            hash(key)
        except TypeError:
            continue
        group_counts[key] += 1
    stats, discarded, accepted = Counter({k: 0 for k in STATS}), set(), []
    allowed_missing, allowed_filter = allowed_missing or {}, allowed_filter or set()
    run, document, catalogue = run or {}, document or {}, catalogue or {}

    def reject(raw, counter):
        stats[counter] += 1
        identity = raw.get("record_id")
        if isinstance(identity, str) and identity in records_by_id:
            discarded.add(identity)
        else:
            discarded.update(raw.get("_batch_record_ids", list(records_by_id)))

    for original in findings:
        raw = deepcopy(original)
        code, uid = raw.get("code"), raw.get("record_id")
        if not isinstance(code, str) or code not in codebook:
            reject(raw, "invalid_finding_code"); continue
        model = raw.get("_source") != "DETERMINISTIC"
        if model and raw.get("_illegal_model_private"):
            reject(raw,"invalid_finding_shape"); continue
        public_keys = {k for k in raw if not k.startswith("_")}
        if model and (public_keys - allowed_fields or required - public_keys):
            reject(raw, "invalid_finding_shape"); continue
        if model and list(model_validator.iter_errors({k: raw[k] for k in public_keys})):
            reject(raw, "invalid_finding_shape"); continue
        if raw.get("_invalid_batch_record"):
            stats["invalid_record_reference"] += 1
            discarded.update(raw.get("_batch_record_ids", []))
            continue
        if raw.get("issue_domain") != codebook[code]["domain"] or raw.get("severity") not in ("BLOCK", "WARN", "INFO"):
            reject(raw, "invalid_finding_shape"); continue
        confidence = number(raw.get("confidence"))
        if confidence is None or not 0 <= confidence <= 1 or type(raw.get("confidence")) not in (int, float):
            reject(raw, "invalid_finding_shape"); continue
        if not isinstance(raw.get("reason"), str) or not 1 <= len(raw["reason"].strip()) <= 512 or raw.get("proposed_action") not in settings["severity"]["actions"]:
            reject(raw, "invalid_finding_shape"); continue
        if uid is None and code not in ("A-MISSING-KEY", "A-FILTER-OVERDROP") or uid is not None and uid not in records_by_id:
            reject(raw, "invalid_record_reference"); continue
        ids = raw.get("evidence_version_ids")
        if not isinstance(ids, list) or not ids or any(not isinstance(eid, str) or eid not in evidence_by_id for eid in ids):
            reject(raw, "dangling_evidence"); continue
        if model and any(eid not in raw.get("_allowed_evidence", evidence_by_id) for eid in ids):
            reject(raw, "dangling_evidence"); continue
        record = records_by_id.get(uid, {})
        indicator = record.get("indicator_id") if uid is not None else raw.get("indicator_id")
        if code == "A-MISSING-KEY" and not set(ids).issubset(set(allowed_missing.get(indicator, []))):
            reject(raw, "missing_key_without_signal"); continue
        if code == "A-FILTER-OVERDROP" and not set(ids).issubset(allowed_filter):
            reject(raw, "overdrop_without_sample"); continue
        if raw.get("evidence_span") is not None and (not isinstance(raw["evidence_span"], str) or len(raw["evidence_span"]) > 300 or
                not any(raw["evidence_span"] in evidence_by_id[eid].get("content", "") for eid in ids)):
            reject(raw, "invalid_proposal"); continue
        proposed = raw.get("proposed_value")
        if proposed is not None or raw.get("proposed_action") == "CORRECT_VALUE":
            quote = raw.get("evidence_span")
            if (raw.get("proposed_action") != "CORRECT_VALUE" or proposed is None or type(proposed) not in (int, float)
                    or not quote or number(proposed) not in number_tokens(quote)):
                reject(raw, "invalid_proposal"); continue
        variable = raw.get("proposed_variable")
        variables = (catalogue.get(indicator) or {}).get("variables", [])
        if variable is not None and (variable not in variables or raw.get("proposed_action") != "REMAP"):
            reject(raw, "invalid_proposal"); continue
        if raw["proposed_action"] == "REMAP" and not variable:
            reject(raw, "invalid_proposal"); continue
        ev = evidence_by_id[ids[0]]
        is_v1 = ev.get("source_format") == "v1_numeric_blocks"
        checks = {h["check"]: h["value"] for h in hints_by_record.get(uid, [])}
        if code == "A-TRACE-MISSING" and is_v1:
            reject(raw, "invalid_finding_shape"); continue
        supported = _prerequisite(code, checks, record, group_counts)
        if code in ("A-TRACE-MISSING", "A-CLOSED-SET-GAP", "A-BOUNDARY-MISSING", "A-DUPLICATE-UNMERGED") and not supported:
            reject(raw, "invalid_finding_shape"); continue
        if code == "A-CLOSED-SET-GAP" and raw["severity"] != "INFO":
            reject(raw, "invalid_finding_shape"); continue
        if raw["severity"] == "BLOCK" and not supported:
            raw["severity"] = "WARN"; stats["unsupported_block"] += 1
        if raw["severity"] == "BLOCK" and raw["confidence"] < policy["block_min_confidence"]:
            raw["severity"] = "WARN"; stats["block_downgraded"] += 1
        if code in ("A-CONTEXT-INSUFFICIENT", "A-TRACE-MISSING", "A-BOUNDARY-MISSING", "A-VALUE-IMPLAUSIBLE"):
            raw["severity"] = "WARN"
        if is_v1 and code.startswith("A-EXTRACT") and "页级" not in raw["reason"]:
            raw["reason"] = raw["reason"][:500] + "（页级核对）"
        # Localization always comes from an actual input evidence, never model-invented coordinates.
        accepted.append({**raw, "indicator_id": indicator, "page_no": ev.get("page_no"),
                         "bbox": None if is_v1 else ev.get("bbox"), "source_kind": ev.get("kind")})

    chosen = {}
    def order(item):
        return (item.get("_source") != "DETERMINISTIC", {"BLOCK": 0, "WARN": 1, "INFO": 2}[item["severity"]], -item["confidence"])
    for item in sorted(accepted, key=order):
        key = (item["record_id"], item["code"])
        if item["record_id"] is None:
            key += (item.get("indicator_id"), tuple(sorted(item["evidence_version_ids"])))
        if key in chosen:
            stats["duplicated_findings"] += 1
        else:
            chosen[key] = item
    output, by_record = [], {}
    ordered = sorted(chosen.values(), key=lambda f: (str(f.get("record_id") or ""), str(f.get("indicator_id") or ""), f["code"], tuple(f["evidence_version_ids"])))
    created_at = run.get("created_at") or utcnow()
    for seq, item in enumerate(ordered, 1):
        record = records_by_id.get(item["record_id"], {})
        finding = {"schema_version": "3.0", "finding_id": f"af_{document.get('pid', 'unknown')}_{seq:05d}",
                   "audit_run_id": run.get("audit_run_id", run.get("run_id", "unknown")), "pid": document.get("pid"),
                   "record_id": item["record_id"], "logical_group_key": record.get("logical_group_key"),
                   "indicator_id": item.get("indicator_id"), "issue_domain": item["issue_domain"],
                   "page_no": item["page_no"], "bbox": item["bbox"], "source_kind": item["source_kind"],
                   "code": item["code"], "severity": item["severity"], "confidence": item["confidence"],
                   "evidence_version_ids": sorted(set(item["evidence_version_ids"])),
                   **{k: item.get(k) for k in ("evidence_span", "observed", "expected", "proposed_value", "proposed_variable")},
                   "proposed_action": item["proposed_action"], "reason": item["reason"],
                   "model_call_id": None if item.get("_source") == "DETERMINISTIC" else item.get("_call_id"),
                   "review_policy_version": policy["review_policy_version"], "created_at": created_at}
        output.append(finding)
        by_record.setdefault(item["record_id"], []).append(finding)
    results = []
    for original in records:
        record = deepcopy(original)
        if record["record_id"] in discarded and not any(f["severity"] in ("BLOCK", "WARN") for f in by_record.get(record["record_id"], [])):
            record["_insufficient"] = True
        own = by_record.get(record["record_id"], [])
        results.append({"schema_version": "3.0", "audit_run_id": run.get("audit_run_id", run.get("run_id", "unknown")),
                        "pid": record["pid"], "record_id": record["record_id"], "logical_group_key": record.get("logical_group_key"),
                        "review_status": derive_review_status(own, record), "finding_ids": [f["finding_id"] for f in own],
                        "reviewed_at": created_at})
    stats.update({"records_total": len(records), "findings": len(output)})
    for finding in output:
        stats[finding["severity"].lower()] += 1
        stats["extraction_findings"] += finding["issue_domain"] == "EXTRACTION"
        stats["classification_findings"] += finding["issue_domain"] == "CLASSIFICATION"
        stats["trace_missing"] += finding["code"] == "A-TRACE-MISSING"
    stats["records_not_reviewed"] = sum(r["review_status"] == "NOT_REVIEWED" for r in results)
    stats["records_reviewed"] = sum(bool(r.get("_reviewed")) for r in records)
    return output, results, dict(stats)
