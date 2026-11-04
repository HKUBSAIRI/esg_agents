"""统一数据契约（Pydantic v2）。

以 `docs/07_工程目录与开发接口.md` 为准。原则：
- 原文/原值与模型结论分离，原文字符串绝不覆盖；
- 定量候选 `raw_value` 必填，定性候选 `raw_text` 必填；
- `classification_status=UNKNOWN` 时 `metric_id` 必须为 null，禁止硬归类；
- 每个候选至少一条 Evidence，页码为原 PDF 物理页码（1 起始）；
- `normalized_value` 不由模型写入，仅在正式事实发布时由 Python 计算。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ClassificationStatus = Literal["CLASSIFIED", "UNKNOWN", "NEEDS_REVIEW", "INSUFFICIENT_EVIDENCE"]
ValueKind = Literal["VOLUME", "MASS", "RATIO", "INTENSITY", "MONEY", "POLICY", "NARRATIVE"]
SourceType = Literal["TEXT", "TABLE", "CHART", "IMAGE"]
LocatorMethod = Literal["QWEN_PAGE_TEXT", "UNVERIFIED"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _non_empty(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    return v or None


class Evidence(BaseModel):
    """证据：模型重新读到的原文，用于人工回看。"""

    model_config = ConfigDict(extra="ignore")

    evidence_id: str
    report_id: str = ""
    source_page: int = Field(ge=1, description="原 PDF 物理页码，1 起始")
    printed_page: int | None = Field(default=None, description="页面上印刷的页码，可空")
    source_type: SourceType = "TEXT"
    source_evidence: str = Field(min_length=1, description="逐字原文，不得改写")
    context_text: str | None = None
    table_title: str | None = None
    row_header: str | None = None
    column_header: str | None = None
    unit_header: str | None = None
    bbox: tuple[float, float, float, float] | None = Field(
        default=None, description="0..1 归一化坐标；PDF API 不承诺，可空"
    )
    crop_uri: str | None = None
    locator_method: LocatorMethod = "QWEN_PAGE_TEXT"

    @field_validator("source_evidence")
    @classmethod
    def _evidence_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("source_evidence 不得为空")
        return v

    @field_validator("bbox")
    @classmethod
    def _bbox_range(cls, v: tuple[float, float, float, float] | None):
        if v is None:
            return None
        if any(x < 0 or x > 1 for x in v):
            raise ValueError("bbox 必须为 0..1 归一化坐标")
        if v[0] > v[2] or v[1] > v[3]:
            raise ValueError("bbox 需满足 x0<=x1 且 y0<=y1")
        return v


class ExtractionCandidate(BaseModel):
    """提取 + 归类候选。允许 UNKNOWN，但不得自动入库。"""

    model_config = ConfigDict(extra="ignore")

    candidate_id: str
    report_id: str
    company_id: str
    report_year: int
    data_year: int | None = None
    raw_value: str | None = Field(default=None, description="原文数值，保留千分位与原始写法")
    raw_text: str | None = Field(default=None, description="定性结论原文")
    unit: str | None = Field(default=None, description="原文单位，如 万吨 / 立方米 / %")
    statistical_period: str | None = None
    reporting_boundary: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(min_length=1)
    ambiguity_flags: list[str] = Field(default_factory=list)
    metric_id: str | None = None
    metric_version: str | None = None
    classification_status: ClassificationStatus
    classification_reason: str = Field(min_length=1)
    alternatives: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    skill_versions: dict[str, str] = Field(default_factory=dict)
    model_call_id: str
    supersedes_id: str | None = None

    @model_validator(mode="after")
    def _validate_classification(self) -> "ExtractionCandidate":
        if self.raw_value is None and self.raw_text is None:
            raise ValueError("raw_value 与 raw_text 不能同时为空")
        if self.classification_status == "UNKNOWN" and self.metric_id is not None:
            raise ValueError("UNKNOWN 状态下 metric_id 必须为 null（禁止硬归类）")
        if self.classification_status == "CLASSIFIED":
            if not self.metric_id:
                raise ValueError("CLASSIFIED 必须给出 metric_id")
            if not self.metric_version:
                raise ValueError("CLASSIFIED 必须给出 metric_version")
        if self.evidence_ids and len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence_ids 不得重复")
        if not self.classification_reason.strip():
            raise ValueError("classification_reason 不得为空")
        return self


class Coverage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pages_scanned: list[int] = Field(default_factory=list)
    notes: str | None = None
    suspected_missing: list[str] = Field(default_factory=list)


class ExtractionBatch(BaseModel):
    """一次提取尝试的完整产物（同时是幂等单元）。"""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = "1"
    batch_id: str
    report_id: str
    company_id: str
    report_year: int
    model: str
    prompt_version: str = "v1"
    skill_digest: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    candidates: list[ExtractionCandidate] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    coverage: Coverage | None = None

    @model_validator(mode="after")
    def _validate_batch(self) -> "ExtractionBatch":
        ev_ids = [e.evidence_id for e in self.evidence]
        if len(set(ev_ids)) != len(ev_ids):
            raise ValueError("evidence_id 全局重复")
        ca_ids = [c.candidate_id for c in self.candidates]
        if len(set(ca_ids)) != len(ca_ids):
            raise ValueError("candidate_id 全局重复")
        known = set(ev_ids)
        for c in self.candidates:
            missing = [eid for eid in c.evidence_ids if eid not in known]
            if missing:
                raise ValueError(f"候选 {c.candidate_id} 引用了不存在的证据: {missing}")
            if c.report_id != self.report_id:
                raise ValueError(f"候选 {c.candidate_id} 的 report_id 与批次不一致")
        for e in self.evidence:
            if e.report_id and e.report_id != self.report_id:
                raise ValueError(f"证据 {e.evidence_id} 的 report_id 与批次不一致")
        return self


class ReportRef(BaseModel):
    """不可变报告版本引用。"""

    model_config = ConfigDict(extra="ignore")

    report_id: str
    company_id: str
    report_year: int
    revision: int = 1
    sha256: str = Field(min_length=64, max_length=64)
    object_uri: str
    page_count: int = Field(ge=1)
    published_at: datetime | None = None
    source_filename: str
    size_bytes: int = Field(ge=1)
    created_at: datetime = Field(default_factory=utcnow)


class MetricDefinition(BaseModel):
    """指标登记（由 skills/metrics 的 front matter 加载）。"""

    model_config = ConfigDict(extra="ignore")

    metric_id: str
    version: str
    name: str
    name_en: str | None = None
    value_kind: ValueKind
    canonical_unit: str | None = None
    status: Literal["ACTIVE", "DRAFT", "RETIRED"] = "ACTIVE"
    csv_column: str | None = Field(default=None, description="阶段一归类结果表列名")
    description: str | None = None


class CompanyProfile(BaseModel):
    """公司主数据（SASB 行业等公司属性，非 PDF 提取事实）。"""

    model_config = ConfigDict(extra="ignore")

    company_id: str
    hkex_code: str
    name: str
    name_en: str | None = None
    name_en_short: str | None = Field(default=None, description="标准答案口径英文简称，如 CR BEVERAGE")
    industry_code: str
    sasb_sector: str
    sasb_industry: str
    aliases: list[str] = Field(default_factory=list)
    default_report_year: int | None = None


class CompanyRegistry(BaseModel):
    companies: list[CompanyProfile]

    def by_id(self, company_id: str) -> CompanyProfile:
        for c in self.companies:
            if c.company_id == company_id:
                return c
        raise KeyError(company_id)

    def by_hkex_code(self, code: str) -> CompanyProfile:
        norm = code.upper().replace(" ", "")
        for c in self.companies:
            if c.hkex_code.upper() == norm or c.hkex_code.split(".")[0] == norm:
                return c
        raise KeyError(code)

    def match(self, text: str) -> CompanyProfile | None:
        """按股票代码 / 公司名 / 别名匹配。"""
        text = text.strip()
        try:
            return self.by_hkex_code(text)
        except KeyError:
            pass
        for c in self.companies:
            for key in [c.name, c.name_en or "", *c.aliases]:
                if key and key in text:
                    return c
        return None


class AuditDecision(BaseModel):
    """审核契约（阶段三实现，此处冻结结构）。"""

    model_config = ConfigDict(extra="ignore")

    audit_id: str
    candidate_id: str
    status: Literal["PASS", "REJECTED", "NEEDS_MANUAL_REVIEW"]
    checks: dict[str, Literal["PASS", "FAIL", "UNCERTAIN"]]
    pdf_evidence_ids: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    suggested_fix: str | None = None
    reason: str
    reviewer_type: Literal["MODEL", "HUMAN"]
    reviewer_id: str | None = None
    model_call_id: str | None = None
