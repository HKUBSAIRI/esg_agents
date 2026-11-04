from decimal import Decimal

import pytest

from app.services.normalizer import (
    detect_currency,
    format_decimal,
    normalize,
    parse_number,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1,366.20", Decimal("1366.20")),
        (" 1 366.20 ", Decimal("1366.20")),
        ("约 23.60", Decimal("23.60")),
        ("324.73", Decimal("324.73")),
        ("<0.01", None),
        ("100-500", None),
        ("不适用", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_number(raw, expected):
    assert parse_number(raw) == expected


def test_volume_wan_dun_to_m3():
    out = normalize("1,366.20", "万吨", "VOLUME")
    assert out.ok
    assert out.value == Decimal("13662000")
    assert out.unit == "m³"
    assert out.factor == Decimal("10000")


def test_volume_m3_as_is():
    out = normalize("234.67", "万立方米", "VOLUME")
    assert out.value == Decimal("2346700")
    assert out.method == "unit_convert"


def test_mass_wan_dun_to_t():
    out = normalize("2.5", "万吨", "MASS")
    assert out.value == Decimal("25000")
    assert out.unit == "t"


def test_ratio_percent():
    out = normalize("32.5", "%", "RATIO")
    assert out.value == Decimal("32.5")
    assert out.unit == "%"


def test_ratio_missing_unit_not_assumed_percent():
    out = normalize("32.5", None, "RATIO")
    assert out.value is None
    assert "不推定" in (out.note or "")


def test_money_yi_yuan_to_million_cny():
    out = normalize("324.73", "亿元", "MONEY")
    assert out.value == Decimal("32473.00")
    assert out.unit == "百万元"
    assert out.currency == "CNY"


def test_money_hkd_from_unit():
    out = normalize("1,234", "百万港元", "MONEY")
    assert out.value == Decimal("1234")
    assert out.currency == "HKD"


def test_money_yuan_defaults_cny_but_unitless_is_none():
    assert normalize("100", "百万元", "MONEY").currency == "CNY"
    assert normalize("100", "million", "MONEY").currency is None
    assert normalize("100", None, "MONEY").currency is None


def test_unsupported_unit_returns_blank():
    out = normalize("12", "未知单位", "VOLUME")
    assert not out.ok
    assert out.method == "unsupported"


def test_non_numeric_value():
    out = normalize("较上年下降 5%", None, "VOLUME")
    assert not out.ok
    assert out.method == "not_numeric"


@pytest.mark.parametrize(
    "value,expected",
    [
        (Decimal("13662000"), "13,662,000"),
        (Decimal("32473.00"), "32,473"),
        (Decimal("32.5"), "32.5"),
        (Decimal("0.00001"), "0"),
        (Decimal("-1234.5"), "-1,234.5"),
        (None, ""),
    ],
)
def test_format_decimal(value, expected):
    assert format_decimal(value) == expected


def test_detect_currency():
    assert detect_currency("人民币百万元") == "CNY"
    assert detect_currency("HK$ million") == "HKD"
    assert detect_currency("百万美元") == "USD"
    assert detect_currency("百万元") == "CNY"
    assert detect_currency("million") is None


def test_hong_kong_financial_thousands_notation():
    value=normalize("1234567", "HK$'000", "MONEY")
    assert value.value==Decimal("1234.567")
    assert value.currency=="HKD"


def test_traditional_chinese_units():
    """港股繁体报告常见「噸/萬噸」，必须与简体同义。"""
    assert normalize("20,642,247", "噸", "VOLUME").value == Decimal("20642247")
    assert normalize("20,642,247", "噸", "VOLUME").unit == "m³"
    assert normalize("828", "噸", "MASS").unit == "t"
    assert normalize("40,515", "萬噸", "VOLUME").value == Decimal("405150000")
    assert normalize("1.2", "萬噸", "MASS").value == Decimal("12000")


@pytest.mark.parametrize('unit,kind',[('mg/L','MASS'),('g/m3','MASS'),('kg per year','MASS'),('unrecognised','VOLUME'),('m3/employee','VOLUME')])
def test_unknown_or_concentration_units_never_match_substrings(unit,kind):
    assert normalize('12',unit,kind).value is None


@pytest.mark.parametrize('raw,unit,kind,expected',[
    ('2','million m3','VOLUME','2000000'),('2','million litres','VOLUME','2000'),
    ('2','tonnes (t)','MASS','2'),('2','ML','VOLUME','2000'),('2','mL','VOLUME','0.000002'),
    ('2','HK$ million','MONEY','2')])
def test_exact_units_preserve_scale(raw,unit,kind,expected):
    assert normalize(raw,unit,kind).value==Decimal(expected)


@pytest.mark.parametrize('raw,unit,kind',[('-1','m3','VOLUME'),('-1','t','MASS'),('101','%','RATIO')])
def test_invalid_totals_and_rates_remain_unavailable(raw,unit,kind):
    assert normalize(raw,unit,kind).value is None
