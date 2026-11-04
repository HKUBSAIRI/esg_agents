"""B-document-derived synthetic mappings. NOT A public models or authoritative schemas."""
from copy import deepcopy


WITHDRAWAL = "Total Water Withdrawal (from all sources in m³)"


def case():
    document = {"pid": "SYNTHETIC-B-001", "company_name": "B合成测试公司", "report_year": 2025,
                "period_start": "2025-01-01", "period_end": "2025-12-31", "page_count": 20,
                "pdf_path": None, "source_format": "v1_numeric_blocks", "doc_type": "fixture"}
    evidence = [{"evidence_version_id": "ev_b_1", "evidence_id": "e_b_1", "run_id": "extract_fixture",
                 "content_sha256": "fixture-not-source-file-sha", "source_format": "v1_numeric_blocks",
                 "page_no": 7, "kind": "table_cell", "content": "<p>2025年集团全年总取水量 Total water withdrawal: 1,234 m³。</p>",
                 "numbers": [1234], "bbox": None, "tier": "high", "unit_hint": "m³", "year_hint": 2025,
                 "row_header": "Total water withdrawal", "col_header": "2025", "source_ref": {}}]
    records = [{"record_id": "rec_b_1", "logical_group_key": "SYNTHETIC-B-001|E2.1.1|" + WITHDRAWAL + "|2025|Group",
                "pid": document["pid"], "evidence_version_id": "ev_b_1", "evidence_id": "e_b_1", "source_kind": "table_cell",
                "source_page": 7, "bbox": None, "filter_status": "ACCEPT", "classification_status": "ASSIGNED",
                "indicator_id": "E2.1.1", "variable": WITHDRAWAL, "category": "Water", "raw_value": "1,234",
                "raw_unit": "m³", "value": 1234, "unit": "m3", "normalized_value": 1234, "normalized_unit": "m3",
                "normalization": {"status": "OK", "from": "m³", "to": "m3", "factor": 1}, "year": 2025,
                "year_status": "DISCLOSED", "year_inference_basis": None, "report_year": 2025, "breakdown": "Group",
                "value_nature": "REPORTED", "record_usable": True, "confidence": .9, "confidence_tier": "high",
                "provenance_source": "evidence_id", "validation_status": "PENDING", "validation_notes": [],
                "classification_reason": "明确的年度、集团范围及总取水量。", "catalogue_version": "SYNTHETIC-SUBSET-B"}]
    decisions = [{"evidence_version_id": "ev_b_1", "evidence_id": "e_b_1", "status": "ACCEPT", "noise_class": "NONE",
                  "topic": "WATER", "reason_code": "DATA", "reason_text": "合成指标", "decision_source": "FIXTURE", "policy_version": "FP-FIXTURE"}]
    coverage = {"key_indicators": ["E2.1.1"], "by_indicator": {"E2.1.1": {"n_records": 1, "unclassified_hints": []}}, "unclassified_hints": []}
    return {"records": records, "evidence": evidence, "decisions": decisions, "filter_sample": [], "coverage": coverage, "document": document}


def clone_record(data, suffix, *, value=200, breakdown="Plant A"):
    record, evidence, decision = deepcopy(data["records"][0]), deepcopy(data["evidence"][0]), deepcopy(data["decisions"][0])
    record.update(record_id="rec_b_" + suffix, evidence_version_id="ev_b_" + suffix, evidence_id="e_b_" + suffix,
                  raw_value=str(value), value=value, normalized_value=value, breakdown=breakdown)
    record["logical_group_key"] = "|".join(str(record.get(k)) for k in ("pid", "indicator_id", "variable", "year", "breakdown"))
    evidence.update(evidence_version_id=record["evidence_version_id"], evidence_id=record["evidence_id"], numbers=[value],
                    content=f"2025年{breakdown}全年 Total water withdrawal: {value} m3")
    decision.update(evidence_version_id=record["evidence_version_id"], evidence_id=record["evidence_id"])
    data["records"].append(record); data["evidence"].append(evidence); data["decisions"].append(decision)
    return record, evidence, decision


def model_finding(code="A-IND-MISMATCH", **updates):
    domains = {"A-UNIT-MISMATCH": "EXTRACTION", "A-YEAR-MISMATCH": "EXTRACTION", "A-EVIDENCE-WEAK": "EXTRACTION",
               "A-TRACE-MISSING": "EXTRACTION", "A-EXTRACT-VALUE": "EXTRACTION", "A-EXTRACT-CONTEXT": "EXTRACTION", "A-EXTRACT-PAGE": "EXTRACTION",
               "A-NORMALIZATION-INVALID": "NORMALIZATION", "A-MISSING-KEY": "COVERAGE", "A-FILTER-OVERDROP": "FILTER"}
    result = {"record_id": "rec_b_1", "issue_domain": domains.get(code, "CLASSIFICATION") if isinstance(code, str) else "CLASSIFICATION", "code": code,
              "severity": "BLOCK", "confidence": .9, "evidence_version_ids": ["ev_b_1"],
              "proposed_action": "ESCALATE", "reason": "合成场景：原文与记录定义不一致。"}
    result.update(updates)
    return result
