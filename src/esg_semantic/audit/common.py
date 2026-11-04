"""B-owned internal helpers. No replacement for A's public record models."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re

RESOURCE_DIR = Path(__file__).resolve().parents[3] / "skills" / "audit"


def mapping(value):
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise ValueError("Expected a mapping or an A dataclass/Pydantic model")


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def read_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key: " + key)
            result[key] = value
        return result

    def finite(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("Non-finite JSON number")
        return parsed

    return json.loads(text, object_pairs_hook=pairs, parse_float=finite,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON number")))


def number(value):
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "").replace("，", "").replace("−", "-")
    if re.fullmatch(r"\([\d.]+\)", text):
        text = "-" + text[1:-1]
    try:
        parsed = Decimal(text)
        return parsed if parsed.is_finite() else None
    except InvalidOperation:
        return None


def number_tokens(content):
    # Chinese labels may touch a number; ASCII identifiers such as m3 must not turn into a value 3.
    numeric = r"(?:\d[\d,，]*(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?"
    pattern = rf"(?<![A-Za-z0-9_.])(?:\({numeric}\)|[-+−]?{numeric})(?![\d.])"
    return {n for token in re.findall(pattern, content)
            if (n := number(token)) is not None}


def close(left, right, *, relative="0.000001", absolute="0.000001"):
    return abs(left - right) <= max(Decimal(absolute), Decimal(relative) * max(abs(left), abs(right)))


_UNITS = {
    "m3": ("volume", "1"), "立方米": ("volume", "1"), "cubicmetres": ("volume", "1"),
    "cubicmeters": ("volume", "1"), "cubicmetre": ("volume", "1"), "cubicmeter": ("volume", "1"),
    "千立方米": ("volume", "1000"), "thousandm3": ("volume", "1000"),
    "万立方米": ("volume", "10000"), "millionm3": ("volume", "1000000"),
    "l": ("volume", ".001"), "litres": ("volume", ".001"), "升": ("volume", ".001"),
    "t": ("mass", "1"), "tonnes": ("mass", "1"), "tonne": ("mass", "1"), "吨": ("mass", "1"),
    "kg": ("mass", ".001"), "千克": ("mass", ".001"), "g": ("mass", ".000001"),
    "mg/l": ("concentration", "1"), "%": ("percentage", "1"),
}


def unit(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = re.sub(r"\s+", "", value.lower().replace("㎥", "m3").replace("³", "3").replace("^3", "3"))
    return _UNITS.get(text, ("literal:" + text, "1"))


def logical_key(record):
    from .integration import current
    if current().record_format == "profile_delivery_v1":
        return "|".join(("UNKNOWN" if k == "year" else "") if record.get(k) is None else str(record[k]) for k in
                        ("pid", "profile_id", "profile_field", "year", "breakdown", "scope_type"))
    return "|".join(str(record.get(k)) for k in ("pid", "indicator_id", "variable", "year", "breakdown"))


def problem(code, message, *, record_id=None, level="warn", **context):
    # Internal draft representation; formal publication requires A's Problem schema.
    return {"level": level, "error_code": code, "message": message,
            "record_id": record_id, "context": context}


def resources():
    # JSON is a valid YAML subset; no permissive YAML execution/deserialization.
    return {name: read_json((RESOURCE_DIR / filename).read_text(encoding="utf-8")) for name, filename in
            [("codebook", "codebook.yaml"), ("severity", "severity.yaml"), ("policy", "policy.json"), ("schema", "schema.json")]}


def policy_values(policy):
    supplied = mapping(policy)
    defaults = resources()["policy"]
    result = {**defaults, **supplied}
    if not result.get("review_policy_version"):
        raise ValueError("review_policy_version is required")
    for field in ("sample_rate", "visual_sample_rate"):
        if type(result[field]) not in (int, float) or not 0 <= result[field] <= 1:
            raise ValueError("Invalid " + field)
    if type(result["block_min_confidence"]) not in (int, float) or not .7 <= result["block_min_confidence"] <= 1:
        raise ValueError("Invalid block_min_confidence")
    for field, lo, hi in (("max_records_per_group", 1, 20), ("filter_sample_max", 0, 1000)):
        if type(result[field]) is not int or not lo <= result[field] <= hi:
            raise ValueError("Invalid " + field)
    if result["max_model_calls"] is not None and (type(result["max_model_calls"]) is not int or result["max_model_calls"] < 0):
        raise ValueError("Invalid max_model_calls")
    if result["allow_auto_adopt"] is not False:
        raise ValueError("B does not allow auto adoption")
    if not isinstance(result["key_indicators"], list) or not all(isinstance(i, str) and i for i in result["key_indicators"]):
        raise ValueError("Invalid key_indicators")
    actual = hashlib.sha256((RESOURCE_DIR / "prompt.md").read_bytes()).hexdigest()
    if result.get("prompt_sha256") not in (None, actual):
        raise ValueError("Prompt fingerprint does not match policy")
    result["prompt_sha256"] = actual
    return result
