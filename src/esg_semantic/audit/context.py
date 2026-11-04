"""Bounded model context and one strict + one fenced/wrapped JSON extraction attempt."""
from copy import deepcopy
from pathlib import Path

from .common import RESOURCE_DIR, canonical, read_json
from .integration import current

RECORD_FIELDS = ("record_id", "evidence_version_id", "evidence_id", "indicator_id", "variable", "raw_value", "raw_unit",
                 "value", "unit", "normalized_value", "normalized_unit", "normalization", "year", "year_status", "breakdown",
                 "classification_status", "value_nature", "provenance_source", "classification_reason", "source_page", "source_kind")
DELIVERY_FIELDS = ("profile_id", "profile_field", "profile_version", "scope_type", "scope_note", "period_start", "period_end", "value_qualifier", "report_year", "year_inference_basis")


def group_context(group, records, evidence, decisions, document, coverage, hints, *, catalogue=None, filter_sample=None):
    selected = []
    for item in sorted(evidence, key=lambda e: e["evidence_version_id"]):
        content = item.get("content", "")
        selected.append({k: deepcopy(item.get(k)) for k in ("evidence_version_id", "page_no", "kind", "unit_hint", "row_header", "col_header", "source_format")} |
                        {"numbers": deepcopy(item.get("numbers", [])[:200]), "numbers_truncated": len(item.get("numbers", [])) > 200,
                         "content": content[:1200], "truncated": len(content) > 1200})
    definition = (catalogue or {}).get(group[1], {})
    clean_records=[]
    for row in records:
        record={k:deepcopy(row.get(k)) for k in RECORD_FIELDS}
        if current().record_format == "profile_delivery_v1":
            record.update({k: deepcopy(row.get(k)) for k in DELIVERY_FIELDS})
        if isinstance(record.get("classification_reason"),str): record["classification_reason"]=record["classification_reason"][:300]
        clean_records.append(record)
    clean_decisions=[{k:deepcopy(row.get(k)) for k in ("evidence_version_id","status","noise_class","reason_code")} for row in decisions]
    clean_samples=[]
    for row in filter_sample or []:
        clean_samples.append({k:deepcopy(row.get(k)) for k in ("sample_id","evidence_version_id","status","noise_class","reason_code")} |
                             {"content_excerpt":str(row.get("content_excerpt") or "")[:1200],"numbers":row.get("numbers",[])[:200],
                              "keyword_hits":row.get("keyword_hits",[])[:50]})
    document_fields = ("pid", "company_name", "report_year", "period_start", "period_end", "doc_type")
    if current().record_format == "profile_delivery_v1":
        document_fields += ("report_year_source",)
    return {"DOCUMENT": {k: document.get(k) for k in document_fields},
            "INDICATOR": {"indicator_id": group[1], "definition": definition},
            "RECORDS": clean_records,
            "EVIDENCE": selected, "FILTER": clean_decisions, "FILTER_SAMPLE": clean_samples,
            "COVERAGE": coverage or {}, "PRECHECK": hints}


def build_group_prompt(group, records, evidence, decisions, document, coverage, hints, *, catalogue=None, filter_sample=None):
    context = group_context(group, records, evidence, decisions, document, coverage, hints,
                            catalogue=catalogue, filter_sample=filter_sample)
    envelope = {"INPUT": context,
                "CODEBOOK": read_json((RESOURCE_DIR / "codebook.yaml").read_text(encoding="utf-8")),
                "RESPONSE_SCHEMA": read_json((RESOURCE_DIR / "schema.json").read_text(encoding="utf-8"))}
    return (RESOURCE_DIR / "prompt.md").read_text(encoding="utf-8") + "\n\n" + canonical(envelope)


def parse_audit_response(raw, group, evidence_by_id):
    """Only parsing/shape here. Per-finding rules belong to postprocess; no network retry."""
    errors = []
    if isinstance(raw, dict):
        parsed = deepcopy(raw)
    else:
        if not isinstance(raw, str) or len(raw) > 1_000_000:
            return [], ["ERR_MODEL_OUTPUT_INVALID"]
        try:
            parsed = read_json(raw)
        except (ValueError, TypeError, RecursionError):
            # One bounded recovery, not arbitrary eval or repeated reparsing.
            start, end = raw.find("{"), raw.rfind("}")
            try:
                if start < 0 or end <= start:
                    raise ValueError("No JSON object")
                parsed = read_json(raw[start:end + 1])
                errors.append("MODEL_JSON_RECOVERED")
            except (ValueError, TypeError, RecursionError):
                return [], ["ERR_MODEL_OUTPUT_INVALID"]
    if not isinstance(parsed, dict) or set(parsed) != {"findings"} or not isinstance(parsed["findings"], list) or len(parsed["findings"]) > 200:
        return [], ["ERR_MODEL_OUTPUT_INVALID"]
    if any(not isinstance(f, dict) for f in parsed["findings"]):
        return [], ["ERR_MODEL_OUTPUT_INVALID"]
    return parsed["findings"], errors
