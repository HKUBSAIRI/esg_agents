"""单位解析与展示归一化。

注意边界：正式事实（OfficialFact.normalized_value）的归一化发生在审核通过后的发布事务中；
本模块当前服务于“候选导出视图”，只做确定性的单位换算，绝不改写原始 `raw_value` / `raw_text`。
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_NUM_RE = re.compile(r"^[+-]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$")
_CLEAN_PREFIX = ("约", "約", "approximately", "about", "~", "≈")
_SPACE = " \u00a0\u2009\u202f\t"

# 繁体中文单位 -> 简体（港股报告常用繁体，"噸"不能与"吨"混用）
_TRAD_TO_SIMP = str.maketrans(
    {
        "噸": "吨",
        "萬": "万",
        "億": "亿",
        "稱": "称",
        "產": "产",
        "噸": "吨",
    }
)

VOLUME_TO_M3: dict[str, Decimal] = {
    "亿立方米": Decimal("100000000"),
    "万立方米": Decimal("10000"),
    "千立方米": Decimal("1000"),
    "百万立方米": Decimal("1000000"),
    "立方米": Decimal("1"),
    "立方公尺": Decimal("1"),
    "m3": Decimal("1"),
    "m³": Decimal("1"),
    "㎥": Decimal("1"),
    "百万升": Decimal("1000"),
    "千升": Decimal("1"),
    "万升": Decimal("10"),
    "升": Decimal("0.001"),
    "l": Decimal("0.001"),
    "kl": Decimal("1"),
    "ml": Decimal("1000"),
    "millilitre": Decimal("0.000001"),
    "megalitre": Decimal("1000"),
    # 水按密度 1t/m³ 换算
    "亿万吨": Decimal("1000000000000"),
    "万吨": Decimal("10000"),
    "千吨": Decimal("1000"),
    "吨": Decimal("1"),
    "t": Decimal("1"),
}

MASS_TO_T: dict[str, Decimal] = {
    "亿万吨": Decimal("1000000000000"),
    "万吨": Decimal("10000"),
    "千吨": Decimal("1000"),
    "吨": Decimal("1"),
    "公吨": Decimal("1"),
    "t": Decimal("1"),
    "ton": Decimal("1"),
    "tonne": Decimal("1"),
    "千克": Decimal("0.001"),
    "公斤": Decimal("0.001"),
    "kg": Decimal("0.001"),
    "克": Decimal("0.000001"),
    "g": Decimal("0.000001"),
}

RATIO_TO_PCT: dict[str, Decimal] = {
    "%": Decimal("1"),
    "％": Decimal("1"),
    "百分比": Decimal("1"),
    "percent": Decimal("1"),
}

MONEY_SCALE_TO_MILLION: dict[str, Decimal] = {
    "十亿元": Decimal("1000"),
    "百万元": Decimal("1"),
    "千万元": Decimal("10"),
    "亿元": Decimal("100"),
    "万元": Decimal("0.01"),
    "千元": Decimal("0.001"),
    "元": Decimal("0.000001"),
    "十亿": Decimal("1000"),
    "百万": Decimal("1"),
    "千万": Decimal("10"),
    "亿": Decimal("100"),
    "万": Decimal("0.01"),
    "千": Decimal("0.001"),
    "million": Decimal("1"),
    "billion": Decimal("1000"),
}

# 金额单位里币种词先剥离，再匹配数量级（"百万港元" → "百万"）
_CURRENCY_WORDS = [
    "人民币", "港元", "港币", "港紙", "美元", "欧元", "rmb", "cny", "hkd", "usd", "eur", "hk$", "us$", "￥", "¥",
]

# 先判断显式外币标记，再判断人民币标记；"元/亿元/万元" 在中文报告里默认为人民币，
# 仅当没有显式外币标记时生效（港币/美元报告会写"港元/美元"）。
FOREIGN_CURRENCY_TOKENS: list[tuple[str, str]] = [
    ("港元", "HKD"),
    ("港币", "HKD"),
    ("港紙", "HKD"),
    ("hk$", "HKD"),
    ("hkd", "HKD"),
    ("美元", "USD"),
    ("us$", "USD"),
    ("usd", "USD"),
    ("欧元", "EUR"),
    ("eur", "EUR"),
]

CNY_CURRENCY_TOKENS: list[tuple[str, str]] = [
    ("人民币", "CNY"),
    ("rmb", "CNY"),
    ("cny", "CNY"),
    ("￥", "CNY"),
    ("¥", "CNY"),
    ("元", "CNY"),
]

# Unit labels are matched as complete labels, never by a single letter inside
# an unknown word (e.g. mg/L is a concentration, not a mass in grams).
for label in ("ton", "tons", "tonne", "tonnes"):
    VOLUME_TO_M3[label]=Decimal(1)
    MASS_TO_T[label]=Decimal(1)
for label in ("cubicmetre", "cubicmetres", "cubicmeter", "cubicmeters"):
    VOLUME_TO_M3[label]=Decimal(1)
for prefix,factor in (("thousand",Decimal(1000)),("million",Decimal(1000000))):
    for label in ("m3","cubicmetres","cubicmeters","tonnes","tons"):
        VOLUME_TO_M3[prefix+label]=factor
    for label in ("tonnes","tons"):
        MASS_TO_T[prefix+label]=factor
for label in ("litre","litres","liter","liters"):
    VOLUME_TO_M3[label]=Decimal("0.001")
    VOLUME_TO_M3["million"+label]=Decimal(1000)


@dataclass(frozen=True)
class NormalizedValue:
    """展示用归一化结果。value=None 表示无法可靠换算，需人工确认。"""

    value: Decimal | None
    unit: str | None
    currency: str | None = None
    factor: Decimal | None = None
    method: str = "unsupported"  # as_is | unit_convert | unsupported | not_numeric | no_value
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.value is not None


def _norm_text(text: str | None) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text).translate(_TRAD_TO_SIMP)
    t = re.sub(r"\s+", "", t).replace("³", "3")
    return t.strip().lower()


def parse_number(raw: str | None) -> Decimal | None:
    """把 '1,366.20' / ' 366.2 ' 解析为 Decimal；区间、文字结论返回 None。"""
    if raw is None:
        return None
    text = raw.strip().replace("\u00a0", " ").replace(" ", "")
    text = unicodedata.normalize("NFKC", text)
    for prefix in _CLEAN_PREFIX:
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = text.rstrip("。.;；")
    if not text:
        return None
    if not _NUM_RE.match(text):
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def detect_currency(unit_text: str | None, extra: str | None = None) -> str | None:
    """从单位/表头文本判断币种；无法判断返回 None。"""
    haystack = _norm_text(unit_text) + "|" + _norm_text(extra)
    for token, code in FOREIGN_CURRENCY_TOKENS:
        if token in haystack:
            return code
    for token, code in CNY_CURRENCY_TOKENS:
        if token in haystack:
            return code
    return None


def _match_unit(unit_text: str, table: dict[str, Decimal]) -> tuple[Decimal | None, str | None]:
    """Accept exact units or equivalent parenthetical spellings, not substrings."""
    if unit_text in table:return table[unit_text],unit_text
    pair=re.fullmatch(r"([^()]+)\(([^()]+)\)",unit_text)
    if pair and pair[1] in table and pair[2] in table and table[pair[1]]==table[pair[2]]:
        return table[pair[1]],pair[1]
    return None, None


def normalize(
    raw_value: str | None,
    unit: str | None,
    value_kind: str | None,
    *,
    unit_context: str | None = None,
) -> NormalizedValue:
    """按指标 value_kind 换算到展示单位：VOLUME→m³，MASS→t，RATIO→%，MONEY→百万元+币种。"""
    if raw_value is None or not raw_value.strip():
        return NormalizedValue(None, None, method="no_value")
    number = parse_number(raw_value)
    if number is None:
        return NormalizedValue(None, None, method="not_numeric", note=f"原值非数值: {raw_value!r}")

    unit_text = _norm_text(unit) or ""
    kind = (value_kind or "").upper()
    if kind in {"VOLUME","MASS","MONEY","RATIO"} and number<0:
        return NormalizedValue(None,None,method="unsupported",note="负值保留为候选，不作为非负总量或比例填表")
    if kind=="RATIO" and number>100:
        return NormalizedValue(None,None,method="unsupported",note="比例超过100%，需核对定义或单位")

    if kind == "VOLUME":
        if str(unit or "").strip() in ("mL", "ml"):
            unit_text = "millilitre"
        elif str(unit or "").strip() == "ML":
            unit_text = "megalitre"
        if "/" in unit_text:
            return NormalizedValue(None, None, method="unsupported", note="强度单位不能作为体积总量")
        factor, matched = _match_unit(unit_text, VOLUME_TO_M3)
        if factor is None:
            return NormalizedValue(None, None, method="unsupported", note=f"无法识别体积单位: {unit!r}")
        return NormalizedValue(
            number * factor,
            "m³",
            factor=factor,
            method="unit_convert" if matched not in {"m3", "m³", "㎥", "立方米", "立方公尺"} else "as_is",
            note=None if unit in (None, "", "m³", "m3", "立方米") else f"{unit} → m³",
        )

    if kind == "MASS":
        factor, matched = _match_unit(unit_text, MASS_TO_T)
        if factor is None:
            return NormalizedValue(None, None, method="unsupported", note=f"无法识别质量单位: {unit!r}")
        return NormalizedValue(
            number * factor,
            "t",
            factor=factor,
            method="unit_convert" if matched not in {"t", "吨", "公吨"} else "as_is",
            note=None if unit in (None, "", "t", "吨", "公吨") else f"{unit} → t",
        )

    if kind == "RATIO":
        factor, matched = _match_unit(unit_text, RATIO_TO_PCT)
        if factor is None:
            return NormalizedValue(
                None,
                None,
                method="unsupported",
                note=f"比例单位缺失或未识别({unit!r})，不推定为百分数",
            )
        return NormalizedValue(number * factor, "%", factor=factor, method="as_is")

    if kind == "MONEY":
        money_text = unit_text
        for word in _CURRENCY_WORDS:
            money_text = money_text.replace(word, "")
        factor, matched = _match_unit(money_text, MONEY_SCALE_TO_MILLION)
        if factor is None and money_text.strip().replace("’", "'") in ("'000", "000s", "'000s"):
            factor, matched = Decimal("0.001"), "thousands"
        currency = detect_currency(unit, unit_context)
        if factor is None:
            return NormalizedValue(None, None, currency=currency, method="unsupported", note=f"无法识别金额单位: {unit!r}")
        return NormalizedValue(
            number * factor,
            "百万元",
            currency=currency,
            factor=factor,
            method="unit_convert",
            note=None if matched == "百万元" else f"{unit} → 百万元",
        )

    # INTENSITY / POLICY / NARRATIVE：不换算，保留原值原单位
    return NormalizedValue(number, unit, method="as_is")


def format_decimal(value: Decimal | None, *, max_decimals: int = 4) -> str:
    """千分位格式化；整数不带小数，保留至多 max_decimals 位小数并去掉尾随 0。"""
    if value is None:
        return ""
    if value == value.to_integral_value():
        return f"{int(value):,}"
    quant = Decimal(1).scaleb(-max_decimals)
    rounded = value.quantize(quant)
    text = format(rounded, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    integer, _, decimal = text.partition(".")
    sign = "-" if integer.startswith("-") else ""
    integer = integer.lstrip("-")
    grouped = f"{int(integer):,}" if integer else "0"
    return f"{sign}{grouped}.{decimal}" if decimal else f"{sign}{grouped}"


def format_ratio(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format_decimal(value, max_decimals=2)


def ratio_from_share(value: Decimal | None) -> Decimal | None:
    """0.325 → 32.5 的帮助函数（仅在明确知道原文是份额时使用）。"""
    if value is None:
        return None
    return value * 100


def sqrt_ratio(part: Decimal | None, whole: Decimal | None) -> Decimal | None:
    """预留：比率计算接口（不在提取阶段使用）。"""
    if part is None or whole is None or whole == 0:
        return None
    return (part / whole) * 100


def safe_log10(value: Decimal | None) -> float | None:
    if value is None or value <= 0:
        return None
    return math.log10(float(value))
