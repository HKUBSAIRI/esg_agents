"""Executable JSON Schema subset and versioned public contracts (no third-party dependencies)."""
from __future__ import annotations

import hashlib
import json
import math

SCHEMA_VERSION = "s4-1.0"
CODES = ["A-IND-MISMATCH", "A-VAR-MISMATCH", "A-SCOPE-MIX", "A-UNIT-MISMATCH",
         "A-YEAR-MISMATCH", "A-BOUNDARY-MISSING", "A-VALUE-IMPLAUSIBLE",
         "A-CROSS-RECORD", "A-DUPLICATE-UNMERGED", "A-AGGREGATION-WRONG",
         "A-MISSING-KEY", "A-EVIDENCE-WEAK", "A-CLOSED-SET-GAP"]
VERDICTS = ["CONFIRMED", "SUSPECT", "REJECTED", "INSUFFICIENT"]
ACTIONS = ["KEEP", "REMAP", "CORRECT_VALUE", "MARK_MISSING", "MERGE_DUPLICATE", "ESCALATE"]
REVIEW_ACTIONS = ["KEEP", "REMAP", "CORRECT_VALUE", "EXCLUDE_FROM_SCORING", "DEFER"]


class ContractError(ValueError):
    pass


def obj(properties, required=None):
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required,
            "additionalProperties": False}


def string(maximum=512, nullable=False):
    return {"type": ["string", "null"] if nullable else "string", "minLength": 1, "maxLength": maximum}


def array(items, minimum=0, maximum=2000):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


NUMBER = {"type": "number"}
NULLNUM = {"type": ["number", "null"]}
EVIDENCE = obj({
    "evidence_id": string(240), "page_no": {"type": "integer", "minimum": 1},
    "kind": {"enum": ["text", "table_cell", "figure", "page_ocr"]}, "content": string(100000),
    "bbox": {"type": ["array", "null"], "items": NUMBER, "minItems": 4, "maxItems": 4},
    "unit_hint": string(160, True), "col_header": string(512, True), "row_header": string(512, True),
    "tier": string(40, True), "source_ref": {"type": "object"}, "extensions": {"type": "object"},
}, ["evidence_id", "page_no", "kind", "content"])
RECORD = obj({
    "record_uid": string(1000), "indicator_id": string(80), "variable": string(512),
    "value": NULLNUM, "unit": string(160, True), "year": {"type": ["integer", "null"], "minimum": 1900, "maximum": 2200},
    "breakdown": string(512, True), "source_page": {"type": ["integer", "null"], "minimum": 1},
    "evidence_id": string(240, True), "provenance_source": {"enum": ["evidence_id", "page_value", "unresolved"]},
    "classification_status": {"enum": ["ASSIGNED", "AMBIGUOUS", "UNMAPPED", "OUT_OF_CLOSED_SET", "INSUFFICIENT_CONTEXT"]},
    "classification_reason": string(3000, True), "extensions": {"type": "object"},
}, ["record_uid", "indicator_id", "variable", "value", "unit", "year", "breakdown", "source_page",
    "evidence_id", "provenance_source", "classification_status"])
INDICATOR = obj({"indicator_id": string(80), "definition": string(4000),
                 "variables": array(string(512), 1, 200), "keywords": array(string(80), 0, 100)},
                ["indicator_id", "definition", "variables"])
POLICY = obj({"policy_version": string(80), "water_rules_version": string(80),
              "check_missing_key": {"type": "boolean"}, "block_confidence": {"type": "number", "minimum": .7, "maximum": 1},
              "max_model_calls": {"type": "integer", "minimum": 0, "maximum": 100},
              "max_output_tokens": {"type": "integer", "minimum": 1000, "maximum": 16000}},
             ["policy_version", "water_rules_version", "check_missing_key", "block_confidence", "max_model_calls", "max_output_tokens"])
BUNDLE = obj({
    "schema_version": {"const": SCHEMA_VERSION},
    "report_context": obj({"pid": string(120), "company_name": string(512),
                           "report_year": {"type": "integer", "minimum": 2000, "maximum": 2200},
                           "period_start": string(40, True), "period_end": string(40, True),
                           "extensions": {"type": "object"}}, ["pid", "company_name", "report_year"]),
    "catalogue": obj({"version": string(80), "indicators": array(INDICATOR, 1, 100)}),
    "review_policy": POLICY, "evidence": array(EVIDENCE, 0, 2000), "records": array(RECORD, 0, 500),
})
MODEL_FINDING = obj({
    "record_uid": string(1000, True), "code": {"enum": CODES},
    "severity": {"enum": ["BLOCK", "WARN", "INFO"]}, "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "evidence_ids": array(string(240), 1, 40), "evidence_span": string(300, True),
    "observed": string(512, True), "expected": string(512, True),
    "proposed_action": {"enum": ACTIONS}, "proposed_value": NULLNUM,
    "proposed_variable": string(512, True), "reason": string(512),
}, ["record_uid", "code", "severity", "confidence", "evidence_ids", "proposed_action", "reason"])
MODEL_RESPONSE = obj({
    "record_verdicts": array(obj({"record_uid": string(1000), "verdict": {"enum": VERDICTS}, "reason": string(512)}), 0, 20),
    "findings": array(MODEL_FINDING, 0, 100),
})
FINDING = obj({**MODEL_FINDING["properties"], "finding_id": string(240), "audit_run_id": string(120),
               "pid": string(120), "indicator_id": string(80), "review_policy_version": string(80),
               "model_call_id": string(120, True)}, MODEL_FINDING["required"] +
              ["finding_id", "audit_run_id", "pid", "indicator_id", "review_policy_version", "model_call_id"])
FINDING["allOf"] = [
    {"if": {"properties": {"severity": {"const": "BLOCK"}}},
     "then": {"properties": {"confidence": {"type": "number", "minimum": .7}}}},
    {"if": {"properties": {"proposed_action": {"const": "CORRECT_VALUE"}}},
     "then": {"required": ["proposed_value", "evidence_span"],
              "properties": {"proposed_value": NUMBER, "evidence_span": string(300)}}},
]
REVIEW = obj({"action": {"enum": REVIEW_ACTIONS}, "reviewer": string(80), "reason": string(2000),
              "expected_version": {"type": "integer", "minimum": 0},
              "proposed_value": NULLNUM, "proposed_variable": string(512, True)},
             ["action", "reviewer", "reason", "expected_version"])
VERDICT = obj({"record_uid": string(1000), "indicator_id": string(80), "verdict": {"enum": VERDICTS},
               "reason": string(512), "finding_ids": array(string(240), 0, 100)})
COVERAGE = obj({"split_group": {"type": "boolean"}, "evidence_omitted": {"type": "boolean"}, "catalogue_version": string(80)})
OUTCOME = obj({"audit_run_id": string(120), "pid": string(120), "indicator_id": string(80), "batch": {"type": "integer", "minimum": 1},
               "policy_version": string(80), "record_verdicts": array(VERDICT, 0, 20),
               "findings": array(string(240), 0, 100), "stats": {"type": "object"},
               "status": {"enum": ["SUCCEEDED", "PARTIAL", "FAILED"]}, "coverage": COVERAGE})
HANDOFF = obj({"schema_version": {"const": "s4-handoff-1.0"}, "run_id": string(120),
               "records": array(obj({"record_uid": string(1000), "indicator_id": string(80),
                   "state": {"enum": ["REVIEW_REQUIRED", "REAUDIT_REQUIRED", "EXCLUDED_FROM_SCORING", "READY_FOR_S5"]},
                   "audit_run_id": string(120), "is_demo": {"type": "boolean"},
                   "task_ids": array(string(240), 0, 2000), "s5_validation_required": {"const": True}}), 0, 500),
               "unresolved_group_tasks": array(string(240), 0, 100), "note": string(512)})
SCHEMAS = {"bundle": BUNDLE, "model_response": MODEL_RESPONSE, "audit_finding": FINDING,
           "audit_outcome": OUTCOME, "s5_handoff": HANDOFF, "review": REVIEW}


def canonical(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(data):
    return hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()


def loads(text):
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ContractError("JSON 数值超出有限数范围")
        return number
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError(f"重复 JSON 字段：{key}")
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_float=finite_float,
                          parse_constant=lambda x: (_ for _ in ()).throw(ContractError(f"非法数字 {x}")))
    except (ValueError, TypeError, RecursionError) as exc:
        raise ContractError(f"JSON 格式无效：{str(exc)[:160]}") from exc


def validate(data, schema, path="$"):
    """Validate only keywords emitted by SCHEMAS; never silently accept unknown schema keywords."""
    supported = {"type", "enum", "const", "required", "properties", "additionalProperties", "items", "minItems", "maxItems",
                 "minLength", "maxLength", "minimum", "maximum", "allOf", "if", "then", "$schema", "$id", "title", "description"}
    if set(schema) - supported:
        raise ValueError("运行时不支持的 Schema 关键字：" + ",".join(sorted(set(schema) - supported)))
    for rule in schema.get("allOf", []):
        validate(data, rule, path)
    if "if" in schema:
        try:
            validate(data, schema["if"], path)
        except ContractError:
            pass
        else:
            validate(data, schema.get("then", {}), path)
    def finite_number(value):
        try:
            return type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            return False
    types = {"object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list),
             "string": lambda v: isinstance(v, str), "null": lambda v: v is None,
             "boolean": lambda v: isinstance(v, bool),
             "number": finite_number,
             "integer": lambda v: type(v) is int}
    expected = schema.get("type")
    if expected:
        names = expected if isinstance(expected, list) else [expected]
        if not any(types[name](data) for name in names):
            raise ContractError(f"{path} 类型应为 {expected}")
    if "enum" in schema and data not in schema["enum"]:
        raise ContractError(f"{path} 不在允许的枚举中")
    if "const" in schema and data != schema["const"]:
        raise ContractError(f"{path} 版本或固定值不符：需要 {schema['const']}")
    if isinstance(data, dict):
        for key in schema.get("required", []):
            if key not in data:
                raise ContractError(f"{path}.{key} 为必填字段")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(data) - set(props):
            raise ContractError(f"{path} 未知字段：{', '.join(sorted(set(data) - set(props)))}")
        for key, value in data.items():
            if key in props:
                validate(value, props[key], f"{path}.{key}")
    if isinstance(data, list):
        if not schema.get("minItems", 0) <= len(data) <= schema.get("maxItems", float("inf")):
            raise ContractError(f"{path} 数组长度超出限制")
        for i, value in enumerate(data):
            validate(value, schema.get("items", {}), f"{path}[{i}]")
    if isinstance(data, str) and not schema.get("minLength", 0) <= len(data.strip()) <= schema.get("maxLength", float("inf")):
        raise ContractError(f"{path} 文本为空或过长")
    if type(data) in (int, float) and not schema.get("minimum", -float("inf")) <= data <= schema.get("maximum", float("inf")):
        raise ContractError(f"{path} 数值超出限制")


def validate_bundle(bundle):
    validate(bundle, BUNDLE)
    for collection, key in [(bundle["evidence"], "evidence_id"), (bundle["records"], "record_uid"),
                            (bundle["catalogue"]["indicators"], "indicator_id")]:
        ids = [item[key] for item in collection]
        if len(ids) != len(set(ids)):
            raise ContractError(f"{key} 不允许重复")
    indicators = {i["indicator_id"] for i in bundle["catalogue"]["indicators"]}
    if any(r["indicator_id"] not in indicators for r in bundle["records"]):
        raise ContractError("记录指标不在传入闭集中；请先补齐对应指标定义")
    for evidence in bundle["evidence"]:
        box = evidence.get("bbox")
        if box and (min(box) < 0 or box[0] >= box[2] or box[1] >= box[3]):
            raise ContractError("bbox 必须是非负、左上原点且 x0<x1、y0<y1")
    return bundle
