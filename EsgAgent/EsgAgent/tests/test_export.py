"""导出层测试：候选选择优先级、冲突检测、CSV 列名与编码。"""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

from app.core.catalog import STAGE1_COLUMNS
from app.models.contracts import (
    CompanyProfile,
    Coverage,
    Evidence,
    ExtractionBatch,
    ExtractionCandidate,
    ReportRef,
)
from app.services.export import (
    build_candidate_rows,
    build_stage1_row,
    select_for_metric,
    write_candidate_csv,
    write_stage1_csv,
)


def make_report() -> ReportRef:
    return ReportRef(
        report_id="rp_1",
        company_id="co_1",
        report_year=2025,
        sha256="a" * 64,
        object_uri="local://pdf/x.pdf",
        page_count=70,
        source_filename="x.pdf",
        size_bytes=1024,
    )


def test_conflict_compares_currency_and_full_precision():
    for metric,unit1,unit2,value1,value2 in [
        ('revenue','HK$ million','US$ million','100','100'),
        ('water_withdrawal','m3','m3','1.000001','1.000002')]:
        a=make_candidate('c1',metric,value1,unit1,page=1)
        b=make_candidate('c2',metric,value2,unit2,page=2)
        selected=select_for_metric([a,b],metric,2025)
        assert selected.conflicts and selected.normalized is None


def make_company() -> CompanyProfile:
    return CompanyProfile(
        company_id="co_1",
        hkex_code="02460.HK",
        name="华润饮料",
        industry_code="fb-nb",
        sasb_sector="Food & Beverage",
        sasb_industry="Non-Alcoholic Beverages",
    )


def make_evidence(eid: str, page: int) -> Evidence:
    return Evidence(
        evidence_id=eid,
        report_id="rp_1",
        source_page=page,
        source_type="TABLE",
        source_evidence=f"总取水量 1,366.20 万吨（第{page}页）",
        table_title="环境绩效指标",
    )


def make_candidate(
    cid: str,
    metric_id: str,
    raw_value: str,
    unit: str,
    *,
    page: int,
    status: str = "CLASSIFIED",
    level: str = "GROUP",
    data_year: int | None = 2025,
    confidence: float = 0.9,
    evidence_id: str | None = None,
) -> ExtractionCandidate:
    return ExtractionCandidate(
        candidate_id=cid,
        report_id="rp_1",
        company_id="co_1",
        report_year=2025,
        data_year=data_year,
        raw_value=raw_value,
        unit=unit,
        reporting_boundary={"level": level, "description": "集团"},
        evidence_ids=[evidence_id or f"ev_{cid}"],
        metric_id=metric_id,
        metric_version="1.0.0",
        classification_status=status,
        classification_reason="表格行明确为总取水量，单位为万吨，集团口径",
        confidence=confidence,
        model_call_id="mc_1",
    )


def test_select_prefers_classified_group_report_year():
    bad = make_candidate("ca_bad", "water_withdrawal", "999", "万吨", page=3, status="NEEDS_REVIEW", level="SITE", data_year=2024)
    good = make_candidate("ca_good", "water_withdrawal", "1,366.20", "万吨", page=61)
    sel = select_for_metric([bad, good], "water_withdrawal", 2025)
    assert sel.candidate.candidate_id == "ca_good"
    assert sel.cell == "13,662,000"


def test_select_detects_conflict():
    a = make_candidate("ca_a", "water_withdrawal", "1,366.20", "万吨", page=61, confidence=0.9)
    b = make_candidate("ca_b", "water_withdrawal", "1,300.00", "万吨", page=30, confidence=0.8)
    sel = select_for_metric([a, b], "water_withdrawal", 2025, page_index={"ev_ca_a": 61, "ev_ca_b": 30})
    assert sel.candidate.candidate_id == "ca_a"
    assert sel.conflicts and "多个同口径候选" in sel.conflicts[0]


def test_select_no_classified_leaves_cell_blank():
    c = ExtractionCandidate(
        candidate_id="ca_u",
        report_id="rp_1",
        company_id="co_1",
        report_year=2025,
        data_year=2025,
        raw_value="10",
        unit="万吨",
        reporting_boundary={"level": "GROUP", "description": "集团"},
        evidence_ids=["ev_u"],
        metric_id=None,
        classification_status="UNKNOWN",
        classification_reason="报告只写“总用水量”，无口径，无法确定是取水还是耗水",
        confidence=0.4,
        model_call_id="mc_1",
    )
    sel = select_for_metric([c], "water_consumption", 2025)
    assert sel.cell == "N/A"
    assert sel.notes and "未归类" in sel.notes[0]


def test_build_stage1_row_and_csv(tmp_path: Path):
    report = make_report()
    company = make_company()
    evidence = [
        make_evidence("ev_ca_1", 61),
        make_evidence("ev_ca_2", 61),
        make_evidence("ev_ca_3", 5),
    ]
    candidates = [
        make_candidate("ca_1", "water_withdrawal", "1,366.20", "万吨", page=61, evidence_id="ev_ca_1"),
        make_candidate("ca_2", "cod_emission", "23.60", "吨", page=61, evidence_id="ev_ca_2"),
        make_candidate("ca_3", "revenue", "324.73", "亿元", page=5, evidence_id="ev_ca_3"),
        make_candidate(
            "ca_4",
            "revenue",
            "324.73",
            "亿元",
            page=5,
            status="NEEDS_REVIEW",
            evidence_id="ev_ca_3",
        ),
    ]
    batch = ExtractionBatch(
        batch_id="bt_1",
        report_id="rp_1",
        company_id="co_1",
        report_year=2025,
        model="qwen3.8-flash",
        candidates=candidates,
        evidence=evidence,
        coverage=Coverage(pages_scanned=list(range(1, 71))),
    )
    row = build_stage1_row(report, company, batch)
    assert row.values["股票代码"] == "02460"
    assert row.values["SASB Sector"] == "Food & Beverage"
    assert row.values["总取水量（m³）"] == "13,662,000"
    assert row.values["COD 排放量（t）"] == "23.6"
    assert row.values["营业收入（百万）"] == "32473.000"
    assert row.values["收入币种"] == "CNY"
    assert row.values["总排水量（m³）"] == "N/A"

    path = write_stage1_csv([row], tmp_path / "out.csv")
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM，Excel 中文不乱码
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        data = next(reader)
    assert header == STAGE1_COLUMNS
    assert len(data) == len(STAGE1_COLUMNS)


def test_candidate_rows_include_pages_and_evidence(tmp_path: Path):
    report = make_report()
    company = make_company()
    evidence = [make_evidence("ev_ca_x", 61)]
    candidates = [make_candidate("ca_x", "water_withdrawal", "1,366.20", "万吨", page=61, evidence_id="ev_ca_x")]
    batch = ExtractionBatch(
        batch_id="bt_1",
        report_id="rp_1",
        company_id="co_1",
        report_year=2025,
        model="qwen3.8-flash",
        candidates=candidates,
        evidence=evidence,
    )
    rows = build_candidate_rows(report, company, batch)
    assert rows[0]["source_pages"] == "61"
    assert rows[0]["normalized_value"] == "13,662,000"
    assert "万吨" in rows[0]["source_evidence"]
    path = write_candidate_csv(rows, tmp_path / "cand.csv")
    assert path.exists()


def test_derive_recycling_rate_when_not_disclosed(tmp_path: Path):
    """报告未披露比率时，按 循环/(取水+循环) 推算；主表带 % 与 4 位小数，并写出推算值 CSV。"""
    report = make_report()
    company = make_company()
    evidence = [make_evidence("ev_w", 61), make_evidence("ev_r", 61)]
    withdrawal = make_candidate("ca_w", "water_withdrawal", "1,366.20", "万吨", page=61, evidence_id="ev_w")
    recycled = make_candidate("ca_r", "water_recycled_volume", "181.99", "万吨", page=61, evidence_id="ev_r")
    batch = ExtractionBatch(
        batch_id="bt_d",
        report_id="rp_1",
        company_id="co_1",
        report_year=2025,
        model="qwen3.8-flash",
        candidates=[withdrawal, recycled],
        evidence=evidence,
    )
    row = build_stage1_row(report, company, batch)
    assert row.values["总取水量（m³）"] == "13,662,000"
    assert row.values["循环利用水量（m³）"] == "1,819,900"
    assert row.values["水循环利用率（%）"] == "11.7550%"
    assert "water_recycling_rate" in row.derived
    from app.services.export import build_derived_rows, write_derived_csv

    derived_rows = build_derived_rows(row)
    assert derived_rows and derived_rows[0]["公式ID"] == "water_recycling_rate.from_withdrawal_and_recycled"
    assert write_derived_csv(derived_rows, tmp_path / "derived.csv").exists()
