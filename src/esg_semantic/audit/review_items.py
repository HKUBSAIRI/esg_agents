"""Website-ready view; no website implementation, no fake evidence for failed rows."""


def build_review_items(findings, record_results, records, evidence_by_id, *, include_info=False):
    records_by_id = {r["record_id"]: r for r in records}
    statuses = {r["record_id"]: r["review_status"] for r in record_results}
    items = []
    for finding in findings:
        status = statuses.get(finding["record_id"], "INSUFFICIENT")
        if finding["severity"] == "INFO" and not include_info:
            continue
        record = records_by_id.get(finding["record_id"], {})
        evidence = evidence_by_id[finding["evidence_version_ids"][0]]
        item = {k: finding[k] for k in ("schema_version", "audit_run_id", "pid", "finding_id", "record_id", "indicator_id",
                  "page_no", "bbox", "source_kind", "issue_domain", "code", "severity", "observed", "expected", "evidence_span",
                  "proposed_action", "proposed_value", "proposed_variable", "evidence_version_ids")}
        item.update(item_id=finding["finding_id"], evidence_version_id=evidence["evidence_version_id"],
                    source_format=evidence["source_format"], priority={"BLOCK": "P0", "WARN": "P1", "INFO": "P2"}[finding["severity"]],
                    review_status=status)
        item.update({key: record.get(key) for key in ("variable", "value", "unit", "year", "breakdown")})
        items.append(item)
    return items
