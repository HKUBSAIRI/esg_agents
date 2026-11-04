"""Version-first lookup and fail-fast identity checks."""
from collections import defaultdict

from .common import logical_key, problem


class InputError(ValueError):
    def __init__(self, message, code="ERR_CONTRACT_INVALID"):
        super().__init__(message)
        self.code = code


def unique(rows, key, *, optional=False):
    result = {}
    for row in rows:
        identity = row.get(key)
        if not isinstance(identity, str) or not identity:
            if optional:
                continue
            raise InputError("Missing identity: " + key)
        if identity in result:
            raise InputError("Duplicate identity: " + key)
        result[identity] = row
    return result


def build_indexes(records, evidence, decisions):
    record_index = unique(records, "record_id")
    evidence_index = unique(evidence, "evidence_version_id")
    decision_index = unique(decisions, "evidence_version_id")
    aliases = defaultdict(list)
    for item in evidence:
        if item.get("evidence_id"):
            aliases[item["evidence_id"]].append(item)
    problems, resolved = [], {}
    for record in records:
        uid = record["record_id"]
        item = evidence_index.get(record.get("evidence_version_id"))
        if item is None:
            candidates = aliases.get(record.get("evidence_id"), [])
            if len(candidates) == 1:
                item = candidates[0]
                problems.append(problem("EVIDENCE_ALIAS_FALLBACK", "Unique evidence_id fallback used", record_id=uid))
        resolved[uid] = item
        if record.get("logical_group_key") != logical_key(record):
            problems.append(problem("LOGICAL_KEY_MISMATCH", "logical_group_key differs from record fields", record_id=uid))
    return {"records": record_index, "evidence": evidence_index, "aliases": dict(aliases), "resolved": resolved}, decision_index, problems
