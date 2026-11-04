"""Lossless numeric candidates. No metric classification or business conversion."""
from decimal import Decimal, InvalidOperation
import re

# Currency prefixes may touch numbers; identifiers such as CO2 and m3 must not.
PATTERN = re.compile(r"(?:(?<=RMB)|(?<=HKD)|(?<=USD)|(?<=CNY)|(?<=EUR)|(?<![A-Za-z0-9_.]))(?:\([-−+]?\d[\d,，\u00a0\u202f ]*(?:\.\d+)?\)|[-−+]?(?:\d(?:[\d,，\u00a0\u202f ]*\d)?(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?)(?![\d.])",re.IGNORECASE)
SCALES = {"million":"1000000", "billion":"1000000000", "thousand":"1000", "万":"10000", "亿":"100000000"}


def tokens(text):
    result = []
    for match in PATTERN.finditer(str(text)):
        raw = match.group().strip()
        normalized = raw.replace("−", "-").replace("，", ",").replace("\u00a0", " ").replace("\u202f", " ")
        negative = normalized.startswith("(") and normalized.endswith(")")
        normalized = normalized.strip("() ")
        warning = None
        # Space-separated digit groups and locale-dependent commas are not guessed.
        normalized = re.sub(r",\s+(?=\d)", ",", normalized)
        if " " in normalized:
            warning = "SPACE_SEPARATED_NUMBER_REQUIRES_REVIEW"
        if "," in normalized and not re.fullmatch(r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", normalized):
            warning = "AMBIGUOUS_COMMA_FORMAT"
        value = None
        if warning is None:
            try:
                number = Decimal(normalized.replace(",", ""))
                if negative: number = -number
                if number.is_finite(): value = str(number)
            except InvalidOperation:
                warning = "NUMBER_PARSE_FAILED"
        if re.search(r"\d\s*[–—~～]\s*\d",str(text)):
            warning = warning or "RANGE_EXPRESSION_REQUIRES_REVIEW"
        following = str(text)[match.end():].lstrip()
        magnitude = next((name for name in SCALES if following.lower().startswith(name)), None)
        result.append({"value_text":raw, "value_decimal":value, "char_start":match.start(), "char_end":match.end(),
                       "magnitude_text":magnitude, "magnitude_factor_hint":SCALES.get(magnitude), "warning":warning})
    return result


def textual_state(text):
    normalized = str(text).strip().lower()
    if normalized in ("nil", "none", "zero", "零", "无"):
        return "TEXTUAL_ZERO_CANDIDATE"
    if normalized in ("", "-", "—", "–", "n/a", "na", "not available", "未披露", "不适用"):
        return "MISSING_OR_NOT_APPLICABLE"
    return "TEXT"
