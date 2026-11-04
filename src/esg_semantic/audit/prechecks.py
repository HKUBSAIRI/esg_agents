"""D1-D14: evidence-bound hints, never corrections to upstream values."""
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
import re

from .common import close, logical_key, number, number_tokens, unit
from .visual import bbox_on_page, page_bounds, valid_bbox
from .integration import current

RECORD_REQUIRED = {"record_id", "pid", "evidence_version_id", "evidence_id", "source_page", "source_kind",
                   "classification_status", "value_nature", "raw_value", "raw_unit", "value", "unit",
                   "normalized_value", "normalized_unit", "normalization", "logical_group_key", "indicator_id",
                   "variable", "year", "breakdown"}
EVIDENCE_REQUIRED = {"evidence_version_id", "page_no", "kind", "content", "numbers", "bbox", "tier",
                     "source_format", "unit_hint", "source_ref"}
KINDS = {"text", "table_cell", "figure", "page_ocr"}
NON_NUMERIC = {"NOT_DISCLOSED", "NOT_APPLICABLE", "UNCERTAIN"}


def hint(check, value, *, code=None, reason=None, severity="WARN", evidence_ids=None, **details):
    return {"check": check, "value": value, "code": code, "reason": reason,
            "severity": severity, "evidence_ids": evidence_ids or [], "details": details}


def normalization_ok(record):
    raw = number(record.get("raw_value"))
    normalized = number(record.get("normalized_value"))
    norm = record.get("normalization")
    if record.get("value") is None and record.get("value_nature") in NON_NUMERIC:
        return normalized is None
    if not isinstance(norm, dict):
        return False
    if norm.get("status") in ("UNKNOWN_UNIT", "NO_UNIT", "NOT_APPLICABLE"):
        return normalized is None
    factor = number(norm.get("factor"))
    if norm.get("status") != "OK" or raw is None or normalized is None or factor is None or factor <= 0:
        return False
    source = unit(norm.get("from"))
    target = unit(norm.get("to"))
    if source is None or target is None or source != unit(record.get("raw_unit")) or target != unit(record.get("normalized_unit")):
        return False
    if source[0] != target[0]:
        if current().record_format == "profile_delivery_v1" and norm.get("rule_id") and close(raw * factor, normalized):
            # An observed A rule is not an approved cross-dimension conversion.
            return None
        if source[0].startswith("literal:") or target[0].startswith("literal:"):
            return None if close(raw * factor, normalized) else False
        return False
    if not close(factor, Decimal(source[1]) / Decimal(target[1])):
        return False
    # C03 draft decision: raw_value is the numeric value before normalization.
    return close(raw * factor, normalized)


def precheck(record, evidence, decision, document, coverage=None, filter_sample=None):
    """Exactly one hint per record-scoped check; D7/D8/D9/D10/D13 are filled by orchestration."""
    hints = []
    missing = sorted(RECORD_REQUIRED - set(record))
    numeric = record.get("value")
    schema_ok = (not missing and isinstance(record.get("indicator_id"), str)
                 and record.get("source_kind") in KINDS
                 and record.get("classification_status") in {"ASSIGNED", "AMBIGUOUS", "UNMAPPED", "OUT_OF_CLOSED_SET", "INSUFFICIENT_CONTEXT"}
                 and (numeric is None or type(numeric) in (int, float) and number(numeric) is not None
                      or record.get("schema_version") == "local-a-1.0" and isinstance(numeric, str) and number(numeric) is not None)
                 and (record.get("year") is None or type(record.get("year")) is int)
                 and record.get("logical_group_key") == logical_key(record))
    if evidence is not None:
        schema_ok = (schema_ok and not (EVIDENCE_REQUIRED - set(evidence))
                     and type(evidence.get("page_no")) is int and evidence["page_no"] >= 1
                     and isinstance(evidence.get("content"), str) and isinstance(evidence.get("numbers"), list)
                     and isinstance(evidence.get("source_ref"), dict)
                     and evidence.get("source_format") in ("v2", "v1_numeric_blocks"))
    if decision is None or not {"status", "reason_code", "policy_version"}.issubset(decision) or decision.get("status") not in {"ACCEPT", "REVIEW", "EXCLUDE"}:
        schema_ok = False
    hints.append(hint("D1", schema_ok, missing_fields=missing))
    if not schema_ok:
        return hints
    ids = [evidence["evidence_version_id"]] if evidence else []
    if evidence is None:
        return hints + [hint("D2", "missing", code=None, reason="Evidence version could not be resolved"),
                        hint("D6", False, reason="No associated evidence")]
    content = evidence.get("content", "")
    declared = {n for item in evidence.get("numbers", []) if (n := number(item)) is not None}
    tokens = declared | number_tokens(content)
    raw, value = number(record.get("raw_value")), number(record.get("value"))
    is_missing_value = record.get("value") is None and record.get("value_nature") in NON_NUMERIC
    match = "not_applicable" if is_missing_value else "exact" if raw is not None and raw in tokens else "approx" if value is not None and value in tokens else "missing"
    hints.append(hint("D2", match, code="A-EVIDENCE-WEAK" if match == "missing" else None,
                      reason="记录数值未出现在关联证据的完整数字中。", severity="BLOCK", evidence_ids=ids))
    if record.get("schema_version") == "local-a-1.0" and record.get("native_numeric_support") == "ABSENT":
        hints[-1] = hint("D2", "missing_native", code="A-EVIDENCE-WEAK", severity="WARN", evidence_ids=ids,
                         reason="记录数值在PDF原生文本区域未匹配，可能是转录或定位偏差，需核对原页；不能据此认定公司未披露。")
    normalization = normalization_ok(record)
    hints.append(hint("D3", normalization, code="A-NORMALIZATION-INVALID" if normalization is False else None,
                      reason="已有归一值、单位或因子不一致，须回到上游核对。", severity="BLOCK", evidence_ids=ids))
    actual_unit, reported_unit = unit(record.get("raw_unit")), unit(evidence.get("unit_hint"))
    unit_match = None if actual_unit is None or reported_unit is None else actual_unit == reported_unit
    page_hint_only = current().record_format == "profile_delivery_v1" and evidence.get("source_format") == "v1_numeric_blocks" and not evidence.get("col_header") and not evidence.get("row_header")
    if page_hint_only:
        # v1 hints may describe a whole page/table with many units, not this cell.
        unit_match = None
    normalized_unit, displayed_unit = unit(record.get("normalized_unit")), unit(record.get("unit"))
    expected_display = actual_unit if current().record_format == "profile_delivery_v1" else normalized_unit
    if expected_display is not None and displayed_unit is not None and expected_display != displayed_unit:
        unit_match = False
    hints.append(hint("D4", unit_match, code="A-UNIT-MISMATCH" if unit_match is False else None,
                      reason="记录原始单位与证据明确绑定的单位不同。", severity="BLOCK", evidence_ids=ids))
    # A multi-year table alone does not bind a record to a column. Use a single unambiguous hint only.
    year_hints = set()
    for key in ("col_header", "year_hint"):
        if page_hint_only and key == "year_hint":
            continue
        year_hints.update(int(y) for y in re.findall(r"(?<!\d)(?:19|20|21)\d{2}(?!\d)", str(evidence.get(key) or "")))
    year_match = record.get("year") in year_hints if len(year_hints) == 1 and record.get("year") is not None else None
    if record.get("year_status") in ("INFERRED", "INFERRED_FROM_REPORT") and not record.get("year_inference_basis"):
        year_match = None
    hints.append(hint("D5", year_match, code="A-YEAR-MISMATCH" if year_match is False else None,
                      reason="记录年度与明确的证据列头/年份线索不一致。", severity="BLOCK", evidence_ids=ids))
    page_ok = record.get("source_page") == evidence.get("page_no")
    provenance_ok = record.get("provenance_source") in ("evidence_id", "evidence_version_id", "page_value")
    hints.append(hint("D6", page_ok and provenance_ok, code="A-EVIDENCE-WEAK" if not provenance_ok else None,
                      reason="记录的溯源状态无法确认。", severity="BLOCK", evidence_ids=ids, page_ok=page_ok))
    is_v1 = evidence.get("source_format") == "v1_numeric_blocks"
    ref = evidence.get("source_ref") or {}
    box = evidence.get("bbox")
    pdf = document.get("pdf_path")
    has_ref = any(ref.get(k) for k in ("table_id", "unit_id", "region_id", "page_ocr"))
    full = valid_bbox(box) and has_ref and isinstance(pdf, str) and Path(pdf).is_file()
    trace = "PAGE_ONLY" if is_v1 else "FULL" if full else "MISSING"
    hints.append(hint("D11", trace, code="A-TRACE-MISSING" if trace == "MISSING" else None,
                      reason="v2区域回溯缺少有效bbox、来源标记或可读取的PDF路径。", evidence_ids=ids))
    expected_ref = {"table_cell": "table_id", "text": "unit_id", "figure": "region_id", "page_ocr": "page_ocr"}.get(evidence.get("kind"))
    context_ok = record.get("source_kind") == evidence.get("kind") and (is_v1 or bool(ref.get(expected_ref)))
    # Explicit row/column references may be compared; never infer a mismatch from absent optional hints.
    for key in ("table_id", "row_header", "col_header", "region_id", "unit_id"):
        left = (record.get("source_ref") or {}).get(key)
        right = ref.get(key, evidence.get(key))
        if left is not None and right is not None and left != right:
            context_ok = False
    hints.append(hint("D12", context_ok, code="A-EXTRACT-CONTEXT" if not context_ok else None,
                      reason="记录的来源通道或明确的行列/区域引用与证据不一致。" + ("（页级核对）" if is_v1 else ""), evidence_ids=ids))
    bounds = page_bounds(pdf, evidence["page_no"]) if trace == "FULL" else None
    box_ok = None if trace != "FULL" or bounds is None else bbox_on_page(box, bounds)
    page_source_ok = page_ok and record.get("source_kind") == evidence.get("kind") and box_ok is not False
    hints.append(hint("D14", page_source_ok, code="A-EXTRACT-PAGE" if not page_source_ok else None,
                      reason="页码、来源通道或页面内区域坐标不一致。", evidence_ids=ids, bounds_checked=bounds is not None))
    if trace == "FULL" and bounds is None:
        trace_hint = next(h for h in hints if h["check"] == "D11")
        trace_hint.update(value="MISSING", code="A-TRACE-MISSING", reason="v2 PDF页不可读取，无法完成区域回溯。")
        hints.append(hint("PDF_READ", "unknown", reason="PDF页无法读取，未核对区域"))
    return hints


def record_group_checks(records, resolved):
    by_group, hints = defaultdict(list), defaultdict(list)
    for record in records:
        if current().record_format == "profile_delivery_v1" and record.get("classification_status") != "ASSIGNED":
            continue
        key = tuple(record.get(k) for k in ("pid", "indicator_id", "variable", "year", "breakdown"))
        if current().record_format == "profile_delivery_v1":
            key += tuple(record.get(k) for k in ("scope_type", "period_start", "period_end", "value_qualifier"))
        try:
            hash(key)
        except TypeError:
            continue
        by_group[key].append(record)
    for rows in by_group.values():
        for record in rows:
            others = [r for r in rows if r["record_id"] != record["record_id"]]
            equivalent = [r for r in others if number(r.get("value")) is not None and number(r.get("value")) == number(record.get("value"))
                          and unit(r.get("unit")) == unit(record.get("unit"))
                          and (r.get("source_page") == record.get("source_page") or
                               r.get("duplicate_group_id") and r.get("duplicate_group_id") == record.get("duplicate_group_id"))]
            conflicts = []
            current_value = number(record.get("normalized_value"))
            for other in others:
                candidate = number(other.get("normalized_value"))
                known_scope = bool(record.get("breakdown")) or current().record_format == "profile_delivery_v1" and record.get("scope_type") in ("GROUP", "COMPANY")
                comparable = (record.get("year") is not None and known_scope
                              and unit(record.get("normalized_unit")) is not None
                              and normalization_ok(record) is True and normalization_ok(other) is True)
                if comparable and current_value is not None and candidate is not None and unit(record.get("normalized_unit")) == unit(other.get("normalized_unit")):
                    scale = max(abs(current_value), abs(candidate))
                    if scale and abs(current_value - candidate) / scale > Decimal(".1"):
                        conflicts.append(other)
            ev = resolved.get(record["record_id"])
            ids = [ev["evidence_version_id"]] if ev else []
            hints[record["record_id"]].extend([
                hint("D7", bool(equivalent), code="A-DUPLICATE-UNMERGED" if equivalent else None,
                     reason="同一可比范围存在同值、同单位及同页/重复组的记录。", evidence_ids=ids),
                hint("D10", bool(conflicts), code="A-CROSS-RECORD" if conflicts else None,
                     reason="同指标、变量、年度、范围及可比归一单位存在差异超过10%的记录。", evidence_ids=ids)])
    return hints


def report_checks(filter_sample, coverage, evidence_by_id, records, policy):
    result, problems, reviewed = [], [], 0
    layout = {"PAGE_NUMBER", "TOC", "URL", "STOCK_CODE", "HEADER", "FOOTER", "HEADER_FOOTER",
              "PAGE_HEADER", "PAGE_FOOTER", "COVER", "DIVIDER", "BLANK"}
    for sample in filter_sample[:policy["filter_sample_max"]]:
        eid = sample.get("evidence_version_id")
        if eid not in evidence_by_id:
            problems.append(("FILTER_SAMPLE_DANGLING", eid)); continue
        reviewed += 1
        if sample.get("status") == "EXCLUDE" and (sample.get("numbers") or sample.get("keyword_hits")) and str(sample.get("noise_class") or "").upper() not in layout:
            result.append(hint("D8", True, code="A-FILTER-OVERDROP", reason="给定过滤样本含数值或指标线索，疑似误删，需核对。",
                               evidence_ids=[eid], indicator_id=None))
    if coverage:
        by_indicator = coverage.get("by_indicator", {})
        for indicator_id in policy["key_indicators"]:
            signal = by_indicator.get(indicator_id, {})
            candidates = signal.get("unclassified_hints") or []
            if not candidates:
                candidates = [h for h in coverage.get("unclassified_hints", []) if isinstance(h, dict) and h.get("indicator_id") == indicator_id]
            ids = []
            for candidate in candidates:
                eid = candidate if isinstance(candidate, str) else candidate.get("evidence_version_id")
                if eid in evidence_by_id:
                    ids.append(eid)
            actual_count = sum(r.get("indicator_id") == indicator_id for r in records)
            if signal.get("n_records") == 0 and actual_count == 0 and ids:
                result.append(hint("D9", True, code="A-MISSING-KEY", reason="coverage明确指出关键指标无记录，但存在有效未归类证据。",
                                   severity="BLOCK", evidence_ids=sorted(set(ids)), indicator_id=indicator_id))
    return result, problems, reviewed


def candidates_from_hints(record, evidence, hints, codebook):
    result = []
    for item in hints:
        if not item["code"] or not item["evidence_ids"]:
            continue
        code = item["code"]
        result.append({"record_id": record.get("record_id") if record else None,
                       "indicator_id": record.get("indicator_id") if record else item["details"].get("indicator_id"),
                       "issue_domain": codebook[code]["domain"], "code": code, "severity": item["severity"],
                       "confidence": 1.0, "evidence_version_ids": item["evidence_ids"], "evidence_span": None,
                       "observed": None, "expected": None, "proposed_action": "ESCALATE", "proposed_value": None,
                       "proposed_variable": None, "reason": item["reason"], "_source": "DETERMINISTIC", "_check": item["check"]})
    return result
