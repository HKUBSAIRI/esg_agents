"""阶段一导出：归类结果表（15 列）+ 候选人明细表（含证据与页码）。

主表严格对齐用户提供的表头；不在主表里塞额外的备注列。
所有“为什么没填/来源在哪/是否冲突”的信息都进入候选人明细表，便于逐条核对。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Sequence

from app.core.catalog import COLUMN_METRIC_IDS, METRIC_NAMES, STAGE1_COLUMNS
from app.models.contracts import CompanyProfile, ExtractionBatch, ExtractionCandidate, ReportRef
from app.services.normalizer import NormalizedValue, format_decimal, format_ratio, normalize

GROUP_BOUNDARY_TOKENS = ("GROUP", "集团", "公司整体", "合并", "本集团")

#: 阶段一主表缺失值写法（对齐标准答案）
NA = "N/A"


def _format_money_million(value: Decimal) -> str:
    """营业收入：固定 3 位小数、不加千分位（对齐标准答案 32473.494）。"""
    return f"{value.quantize(Decimal('0.001')):.3f}"


def _format_ratio_cell(value: Decimal, *, derived: bool) -> str:
    """水循环利用率：带 % 号；推算值固定 4 位小数（如 11.7550%），披露值最多 4 位去尾零。"""
    if derived:
        return f"{value.quantize(Decimal('0.0001')):.4f}%"
    text = f"{value.quantize(Decimal('0.0001')):.4f}".rstrip("0").rstrip(".")
    return f"{text}%"


@dataclass
class MetricSelection:
    metric_id: str
    candidate: ExtractionCandidate | None = None
    normalized: NormalizedValue | None = None
    notes: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    derived: bool = False
    formula_id: str | None = None
    formula_version: str | None = None
    derivation_inputs: dict[str, str] = field(default_factory=dict)

    @property
    def cell(self) -> str:
        if self.normalized is None or self.normalized.value is None:
            return NA
        if self.normalized.unit == "%":
            return _format_ratio_cell(self.normalized.value, derived=self.derived)
        if self.normalized.unit == "百万元":
            return _format_money_million(self.normalized.value)
        return format_decimal(self.normalized.value)

    @property
    def currency(self) -> str | None:
        return self.normalized.currency if self.normalized else None


@dataclass
class Stage1Row:
    company: CompanyProfile
    report: ReportRef
    batch: ExtractionBatch
    values: dict[str, str]
    selections: dict[str, MetricSelection]
    issues: list[str] = field(default_factory=list)
    derived: dict[str, MetricSelection] = field(default_factory=dict)


def _boundary_level(candidate: ExtractionCandidate) -> str:
    boundary = candidate.reporting_boundary or {}
    return str(boundary.get("level") or boundary.get("boundary") or "UNKNOWN").upper()


def _is_group_boundary(candidate: ExtractionCandidate) -> bool:
    if _boundary_level(candidate) == "GROUP":
        return True
    if _boundary_level(candidate) in {"SITE", "SUBSIDIARY", "SEGMENT", "REGION"}:
        return False
    text = str((candidate.reporting_boundary or {}).get("description") or "")
    return any(tok in text for tok in GROUP_BOUNDARY_TOKENS)


def _score(candidate: ExtractionCandidate, report_year: int) -> float:
    """候选优先级：CLASSIFIED > GROUP 边界 > 报告年度 > 置信度；越低越容易被淘汰。"""
    score = 0.0
    if candidate.classification_status == "CLASSIFIED":
        score += 100
    elif candidate.classification_status == "NEEDS_REVIEW":
        score += 10
    elif candidate.classification_status == "INSUFFICIENT_EVIDENCE":
        score += 5
    if _is_group_boundary(candidate):
        score += 40
    if candidate.data_year == report_year:
        score += 30
    elif candidate.data_year is None:
        score += 15
    else:
        score -= 20
    score += max(0.0, min(1.0, candidate.confidence)) * 10
    return score


def _metric_value_kind(metric_id: str, registry: dict | None) -> str:
    if registry and metric_id in registry:
        return str(registry[metric_id].value_kind)
    if metric_id == "revenue":
        return "MONEY"
    if metric_id == "water_recycling_rate":
        return "RATIO"
    if metric_id in {"water_policy", "water_risk"}:
        return "POLICY"
    if metric_id == "water_intensity":
        return "INTENSITY"
    if metric_id in {"cod_emission", "ammonia_nitrogen_emission", "total_nitrogen_emission", "total_phosphorus_emission"}:
        return "MASS"
    return "VOLUME"


def select_for_metric(
    candidates: Sequence[ExtractionCandidate],
    metric_id: str,
    report_year: int,
    registry: dict | None = None,
    page_index: dict[str, int] | None = None,
) -> MetricSelection:
    sel = MetricSelection(metric_id=metric_id)
    pool = [c for c in candidates if c.metric_id == metric_id]
    if not pool:
        unknown = [c for c in candidates if c.metric_id is None]
        if unknown:
            sel.notes.append(f"存在 {len(unknown)} 条未归类候选，已排除在 {metric_id} 之外")
        return sel

    classified = [c for c in pool if c.classification_status == "CLASSIFIED"]
    if not classified:
        statuses = sorted({c.classification_status for c in pool})
        sel.notes.append(f"仅有非 CLASSIFIED 候选（{','.join(statuses)}），不填入主表")
        # 仍保留一条最高优先级候选用于明细展示
        sel.candidate = max(pool, key=lambda c: _score(c, report_year))
        return sel

    eligible = [c for c in classified if c.data_year == report_year and _is_group_boundary(c)]
    if not eligible:
        sel.candidate = max(classified, key=lambda c: _score(c, report_year))
        sel.notes.append("没有年度明确且为集团口径的候选，保留明细，不填主表")
        return sel
    ranked = sorted(eligible, key=lambda c: _score(c, report_year), reverse=True)
    best = ranked[0]
    sel.candidate = best

    kind = _metric_value_kind(metric_id, registry)
    unit_context = None
    if best.evidence_ids:
        unit_context = None  # 证据在明细表展示
    sel.normalized = normalize(best.raw_value, best.unit, kind, unit_context=unit_context)

    # 同口径候选值冲突检测：集团、报告年度、已归类
    peers = [c for c in classified if _is_group_boundary(c) and c.data_year == report_year]
    distinct: dict[str, list[ExtractionCandidate]] = {}
    for c in peers:
        norm = normalize(c.raw_value, c.unit, kind)
        key = (str(norm.value.normalize()),norm.currency,norm.unit) if norm.value is not None else (f"RAW:{c.raw_value}",c.unit,None)
        distinct.setdefault(key, []).append(c)
    if len(distinct) > 1:
        parts = []
        for key, group in distinct.items():
            pages = sorted({p for c in group for p in _candidate_pages(c, page_index)})
            parts.append(f"{key[0]} {key[1] or ''}(第{','.join(map(str, pages))}页)")
        sel.conflicts.append(f"{metric_id} 存在多个同口径候选: " + " vs ".join(parts))
        sel.normalized = None
        return sel

    if sel.normalized is not None and not sel.normalized.ok:
        sel.notes.append(f"无法换算: {sel.normalized.note}")
    return sel


def _candidate_pages(candidate: ExtractionCandidate, page_index: dict[str, int] | None = None) -> list[int]:
    if not page_index:
        return []
    return sorted({page_index[eid] for eid in candidate.evidence_ids if eid in page_index})


def _derive_recycling_rate(
    selections: dict[str, MetricSelection], report_year: int
) -> MetricSelection | None:
    """报告未披露水循环利用率时，按 循环利用水量 /（总取水量 + 循环利用水量） 推算。

    仅当取水量与循环利用水量均为 CLASSIFIED、集团口径、报告年度时生效；
    公式与输入写入「推算值」CSV，主表值需人工确认（标准答案采用同一口径）。
    """
    rate = selections.get("water_recycling_rate")
    if rate is not None and rate.normalized is not None and rate.normalized.value is not None:
        return None  # 报告已披露比率，优先披露值
    withdrawal = selections.get("water_withdrawal")
    recycled = selections.get("water_recycled_volume")
    if not (withdrawal and recycled and withdrawal.candidate and recycled.candidate):
        return None
    for sel in (withdrawal, recycled):
        c = sel.candidate
        if c is None or c.classification_status != "CLASSIFIED":
            return None
        if c.data_year != report_year:
            return None
        if not _is_group_boundary(c):
            return None
        if sel.normalized is None or not sel.normalized.ok:
            return None
    wv = abs(withdrawal.normalized.value)
    rv = abs(recycled.normalized.value)
    if wv <= 0 or wv + rv <= 0:
        return None
    rate_value = (rv / (wv + rv)) * Decimal("100")
    return MetricSelection(
        metric_id="water_recycling_rate",
        normalized=NormalizedValue(rate_value, "%", method="unit_convert", note="由取水量与循环利用水量推算"),
        notes=["报告未披露水循环利用率，按 循环利用水量/(总取水量+循环利用水量) 推算，需人工确认"],
        derived=True,
        formula_id="water_recycling_rate.from_withdrawal_and_recycled",
        formula_version="1.0.0",
        derivation_inputs={
            "water_withdrawal_m3": format_decimal(wv),
            "water_recycled_volume_m3": format_decimal(rv),
            "formula": "循环利用水量 / (总取水量 + 循环利用水量) × 100",
        },
    )


def build_stage1_row(
    report: ReportRef,
    company: CompanyProfile,
    batch: ExtractionBatch,
    registry: dict | None = None,
) -> Stage1Row:
    page_index = {e.evidence_id: e.source_page for e in batch.evidence}
    values: dict[str, str] = {col: "" for col in STAGE1_COLUMNS}
    values["股票代码"] = company.hkex_code.split(".")[0] if company.hkex_code else ""
    values["公司名称"] = company.name_en_short or company.name_en or company.name
    values["SASB Sector"] = company.sasb_sector
    values["SASB Industry"] = company.sasb_industry

    selections: dict[str, MetricSelection] = {}
    derived: dict[str, MetricSelection] = {}
    issues: list[str] = []
    for column, metric_id in COLUMN_METRIC_IDS.items():
        sel = select_for_metric(batch.candidates, metric_id, report.report_year, registry, page_index)
        selections[metric_id] = sel
        values[column] = sel.cell
        if sel.currency:
            values["收入币种"] = sel.currency
        issues.extend(f"[{column}] {note}" for note in sel.notes)
        issues.extend(f"[{column}] {note}" for note in sel.conflicts)

    # 报告未披露水循环利用率时，用取水量与循环利用水量推算（公式与输入写入推算值 CSV）
    derived_rate = _derive_recycling_rate(selections, report.report_year)
    if derived_rate is not None:
        derived["water_recycling_rate"] = derived_rate
        selections["water_recycling_rate"] = derived_rate
        values["水循环利用率（%）"] = derived_rate.cell
        issues.append(f"[水循环利用率（%）] {derived_rate.notes[0]}")

    # 收入币种兜底：从单位文本判断
    if not values["收入币种"]:
        revenue = selections.get("revenue")
        if revenue and revenue.candidate and revenue.candidate.unit:
            from app.services.normalizer import detect_currency

            code = detect_currency(revenue.candidate.unit)
            if code:
                values["收入币种"] = code

    missing = [col for col in STAGE1_COLUMNS[4:] if not values[col]]
    for col in missing:
        issues.append(f"[{col}] 未找到可填的已归类候选（留空待人工确认）")
        values[col] = NA
    if not values["收入币种"]:
        values["收入币种"] = NA
    return Stage1Row(
        company=company,
        report=report,
        batch=batch,
        values=values,
        selections=selections,
        issues=issues,
        derived=derived,
    )


def write_stage1_csv(rows: Iterable[Stage1Row], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=STAGE1_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.values.get(col, "") for col in STAGE1_COLUMNS})
    return path


CANDIDATE_COLUMNS = [
    "序号",
    "股票代码",
    "公司名称",
    "candidate_id",
    "metric_id",
    "指标中文名",
    "classification_status",
    "raw_value",
    "raw_unit",
    "data_year",
    "reporting_boundary",
    "source_pages",
    "source_type",
    "source_evidence",
    "table_title",
    "row_header",
    "column_header",
    "unit_header",
    "classification_reason",
    "alternatives",
    "confidence",
    "evidence_ids",
    "normalized_value",
    "normalized_unit",
    "normalization_note",
]


def build_candidate_rows(
    report: ReportRef,
    company: CompanyProfile,
    batch: ExtractionBatch,
    registry: dict | None = None,
) -> list[dict[str, str]]:
    page_index = {e.evidence_id: e.source_page for e in batch.evidence}
    ev_by_id = {e.evidence_id: e for e in batch.evidence}

    def sort_key(c: ExtractionCandidate) -> tuple:
        order = list(COLUMN_METRIC_IDS.values())
        idx = order.index(c.metric_id) if c.metric_id in order else len(order)
        pages = _candidate_pages(c, page_index)
        return (idx, pages[0] if pages else 9999, c.candidate_id)

    rows: list[dict[str, str]] = []
    for i, c in enumerate(sorted(batch.candidates, key=sort_key), start=1):
        kind = _metric_value_kind(c.metric_id, registry) if c.metric_id else None
        norm = normalize(c.raw_value, c.unit, kind) if kind else NormalizedValue(None, None, method="no_value")
        evs = [ev_by_id[eid] for eid in c.evidence_ids if eid in ev_by_id]
        first = evs[0] if evs else None
        rows.append(
            {
                "序号": str(i),
                "股票代码": company.hkex_code,
                "公司名称": company.name,
                "candidate_id": c.candidate_id,
                "metric_id": c.metric_id or "UNKNOWN",
                "指标中文名": METRIC_NAMES.get(c.metric_id or "", "") if c.metric_id else "",
                "classification_status": c.classification_status,
                "raw_value": c.raw_value or "",
                "raw_unit": c.unit or "",
                "data_year": str(c.data_year) if c.data_year else "",
                "reporting_boundary": str((c.reporting_boundary or {}).get("description") or (c.reporting_boundary or {}).get("level") or ""),
                "source_pages": ",".join(str(p) for p in sorted({e.source_page for e in evs})),
                "source_type": first.source_type if first else "",
                "source_evidence": (first.source_evidence if first else ""),
                "table_title": (first.table_title or "") if first else "",
                "row_header": (first.row_header or "") if first else "",
                "column_header": (first.column_header or "") if first else "",
                "unit_header": (first.unit_header or "") if first else "",
                "classification_reason": c.classification_reason,
                "alternatives": "; ".join(c.alternatives),
                "confidence": f"{c.confidence:.2f}",
                "evidence_ids": ",".join(c.evidence_ids),
                "normalized_value": format_decimal(norm.value) if norm.value is not None else "",
                "normalized_unit": norm.unit or "",
                "normalization_note": norm.note or "",
            }
        )
    return rows


DERIVED_COLUMNS = [
    "股票代码",
    "公司名称",
    "数据年",
    "指标",
    "推算值",
    "公式ID",
    "公式版本",
    "输入",
    "说明",
]


def build_derived_rows(row: Stage1Row) -> list[dict[str, str]]:
    """把主表中的推算值单独列出，便于人工确认（不写入正式事实）。"""
    out: list[dict[str, str]] = []
    for metric_id, sel in row.derived.items():
        out.append(
            {
                "股票代码": row.values.get("股票代码", ""),
                "公司名称": row.values.get("公司名称", ""),
                "数据年": str(row.report.report_year),
                "指标": METRIC_NAMES.get(metric_id, metric_id),
                "推算值": sel.cell,
                "公式ID": sel.formula_id or "",
                "公式版本": sel.formula_version or "",
                "输入": "; ".join(f"{k}={v}" for k, v in sel.derivation_inputs.items() if k != "formula"),
                "说明": (sel.notes[0] if sel.notes else "") + f"（公式：{sel.derivation_inputs.get('formula', '')}）",
            }
        )
    return out


def write_derived_csv(rows: list[dict[str, str]], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=DERIVED_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for item in rows:
            writer.writerow(item)
    return path


def write_candidate_csv(rows: list[dict[str, str]], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CANDIDATE_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def _has_value(value: str | None) -> bool:
    return bool(value) and value != NA


def summarize_row(row: Stage1Row) -> str:
    filled = sum(1 for col in STAGE1_COLUMNS[4:] if _has_value(row.values.get(col)))
    total = len(STAGE1_COLUMNS) - 4
    lines = [
        f"{row.company.name}({row.company.hkex_code}) {row.report.report_year}: 已填 {filled}/{total} 列",
    ]
    for col in STAGE1_COLUMNS[4:]:
        if _has_value(row.values.get(col)):
            suffix = "（推算）" if col == "水循环利用率（%）" and "water_recycling_rate" in row.derived else ""
            lines.append(f"  ✓ {col} = {row.values[col]}{suffix}")
    for issue in row.issues:
        if "未找到可填" in issue or "冲突" in issue or "非 CLASSIFIED" in issue or "同口径" in issue:
            lines.append(f"  ! {issue}")
    return "\n".join(lines)
