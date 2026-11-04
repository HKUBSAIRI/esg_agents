"""Stable sampling, priority and bounded batches; never Python's randomized hash()."""
from collections import defaultdict
import hashlib
from .integration import current


def sampled(identity, rate):
    return int(hashlib.sha1(identity.encode("utf-8")).hexdigest()[:8], 16) % 100 < rate * 100


def mandatory(record, policy):
    return (record.get("indicator_id") in policy["key_indicators"] or record.get("_effective_filter_status", record.get("filter_status")) == "REVIEW"
            or record.get("classification_status") in ("AMBIGUOUS", "INSUFFICIENT_CONTEXT"))


def record_sort(record):
    return (record.get("year") is None, record.get("year") or 0, str(record.get("breakdown") or ""), record["record_id"])


def batches(records, policy):
    selected, skipped, groups = set(), set(), defaultdict(list)
    for record in records:
        uid = record["record_id"]
        if mandatory(record, policy) or sampled(uid, policy["sample_rate"]):
            selected.add(uid)
            if not record.get("_insufficient") or (current().record_format == "profile_delivery_v1" and record.get("_delivery_semantic_eligible")):
                groups[(record["pid"], record.get("indicator_id"))].append(record)
        else:
            skipped.add(uid)
    result = []
    ordered = sorted(groups.items(), key=lambda pair: (not any(mandatory(r, policy) for r in pair[1]), str(pair[0])))
    limit = policy["max_records_per_group"]
    for key, rows in ordered:
        rows = sorted(rows, key=record_sort)
        if len(rows) <= limit:
            result.append((key, rows, False))
            continue
        subgroups = defaultdict(list)
        for row in rows:
            subgroups[(row.get("year"), row.get("breakdown"))].append(row)
        for subgroup in subgroups.values():
            for start in range(0, len(subgroup), limit):
                result.append((key, subgroup[start:start + limit], True))
    # A mixed group may include non-key rows; key-containing batches always precede ordinary ones.
    result.sort(key=lambda batch: (not any(r.get("indicator_id") in policy["key_indicators"] for r in batch[1]),
                                   not any(mandatory(r, policy) for r in batch[1])))
    return result, selected, skipped
