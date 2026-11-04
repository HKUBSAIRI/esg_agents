"""Deterministic Decimal normalization. No water-mass to volume assumptions."""
from decimal import Decimal, InvalidOperation
import re

UNITS = {
    "m3": ("volume", Decimal(1)), "m³": ("volume", Decimal(1)), "立方米": ("volume", Decimal(1)),
    "cubic metres": ("volume", Decimal(1)), "cubic meters": ("volume", Decimal(1)),
    "l": ("volume", Decimal("0.001")), "litres": ("volume", Decimal("0.001")),
    "millilitres": ("volume", Decimal("0.000001")), "megalitres": ("volume", Decimal(1000)),
    "t": ("mass", Decimal(1)), "tonnes": ("mass", Decimal(1)), "tonne": ("mass", Decimal(1)),
    "吨": ("mass", Decimal(1)), "噸": ("mass", Decimal(1)), "kg": ("mass", Decimal("0.001")),
    "%": ("ratio", Decimal(1)), "percent": ("ratio", Decimal(1))
}


def unit_key(unit):
    raw = str(unit or "").strip()
    if raw == "ML":
        return "megalitres"
    if raw in ("mL", "ml"):
        return "millilitres"
    return raw.lower()


def normalize(value, unit, target):
    norm = {"status": "NO_UNIT" if not unit else "UNKNOWN_UNIT", "from_unit": unit,
            "to_unit": target, "factor": None, "rule_id": None, "rule_version": "LOCAL-UNITS-1.0"}
    if value is None:
        norm["status"] = "NOT_APPLICABLE"
        return None, None, norm
    source = unit_key(unit)
    destination = unit_key(target)
    if source not in UNITS or destination not in UNITS:
        return None, None, norm
    a, b = UNITS[source], UNITS[destination]
    if a[0] != b[0]:
        return None, None, norm
    factor = a[1] / b[1]
    normalized = Decimal(str(value)) * factor
    norm.update(status="OK", factor=str(factor), rule_id="dimensional-conversion-v1")
    return format(normalized, "f"), target, norm


def decimal_value(candidate):
    raw = candidate.get("value_decimal")
    if raw is None:
        return None
    try:
        number = Decimal(str(raw))
        return format(number, "f") if number.is_finite() else None
    except InvalidOperation:
        return None


def dimension_ok(unit, target):
    if not unit:
        return False
    a = UNITS.get(unit_key(unit))
    b = UNITS.get(target)
    # Mass-water remains a candidate, but cannot be normalized to volume.
    return bool(a and b and (a[0] == b[0] or a[0] == "mass" and b[0] == "volume"))
