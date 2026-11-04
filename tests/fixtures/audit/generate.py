"""Regenerate B-only synthetic fixture files; do not use as A acceptance data."""
from copy import deepcopy
import json
from pathlib import Path

from builders import case, clone_record, WITHDRAWAL


def write_case(root, data):
    root.mkdir(parents=True, exist_ok=True)
    for name, key in [("records.jsonl", "records"), ("evidence.jsonl", "evidence"),
                      ("semantic_filter.jsonl", "decisions"), ("filter_audit_sample.jsonl", "filter_sample")]:
        text = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in data[key])
        (root / name).write_text(text + ("\n" if text else ""), encoding="utf-8")
    for name, key in [("coverage.json", "coverage"), ("document_context.json", "document")]:
        (root / name).write_text(json.dumps(data[key], ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    root = Path(__file__).parent
    normal = case()
    wrong, evidence, _ = clone_record(normal, "problem", value=200)
    evidence.update(numbers=[201], content="2025年Plant A全年总取水量实际为201 m3")
    unresolved, _, _ = clone_record(normal, "unknown", value=300, breakdown="Plant B")
    unresolved.update(evidence_version_id="ev_missing", evidence_id="e_missing", provenance_source="unresolved")
    for scenario in ("normal", "bad_unit", "bad_normalization", "bad_year", "duplicate", "filter_overdrop", "missing_key", "llm_failure"):
        data = deepcopy(normal)
        record = data["records"][0]
        if scenario == "bad_unit":
            record.update(raw_unit="kg", unit="kg", normalized_unit="kg", normalization={"status":"OK","from":"kg","to":"kg","factor":1})
        elif scenario == "bad_normalization":
            record["normalized_value"] = 1234000
        elif scenario == "bad_year":
            record["year"] = 2024
            record["logical_group_key"] = record["logical_group_key"].replace("|2025|", "|2024|")
        elif scenario == "duplicate":
            clone_record(data, "dup", value=1234, breakdown="Group")
        elif scenario == "filter_overdrop":
            data["filter_sample"] = [{"sample_id":"sample_1","evidence_version_id":"ev_b_1","status":"EXCLUDE","noise_class":"NONE",
                                       "reason_code":"TEST","page_no":7,"kind":"table_cell","numbers":[1234],"content_excerpt":"总取水量1234 m3",
                                       "keyword_hits":["water"],"sampling_method":"fixture"}]
        elif scenario == "missing_key":
            data["records"][0]["indicator_id"] = "OTHER"
            data["records"][0]["logical_group_key"] = data["records"][0]["logical_group_key"].replace("E2.1.1", "OTHER")
            for r in data["records"][1:]:
                r["indicator_id"] = "OTHER"; r["logical_group_key"] = r["logical_group_key"].replace("E2.1.1", "OTHER")
            data["coverage"]["by_indicator"]["E2.1.1"] = {"n_records":0,"unclassified_hints":[{"evidence_version_id":"ev_b_1"}]}
        write_case(root / scenario, data)
    (root / "catalogue.json").write_text(json.dumps({"E2.1.1":{"definition":"集团总取水量；按证据披露定义区分耗水量。", "variables":[WITHDRAWAL]}}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Synthetic B fixtures generated; no A modules or production records were changed.")
