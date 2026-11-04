"""模型输出的草稿契约（Draft）。

模型不知道 `report_id` / 最终 ID，因此先用宽松结构承接模型 JSON，再由 Python：
1) 校验结构、页码范围、指标登记；
2) 重映射为全局唯一 ID（ev_/ca_ UUIDv7）；
3) 注入 report_id / company_id / report_year；
4) 产出 `ExtractionBatch` 规范对象。

草稿模型只做“是否是可用结构”的判断，不复制业务规则。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DraftEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    evidence_id: str
    source_page: int
    printed_page: int | None = None
    source_type: str = "TEXT"
    source_evidence: str
    context_text: str | None = None
    table_title: str | None = None
    row_header: str | None = None
    column_header: str | None = None
    unit_header: str | None = None
    locator_method: str = "QWEN_PAGE_TEXT"


class DraftCandidate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    candidate_id: str
    data_year: int | None = None
    raw_value: str | None = None
    raw_text: str | None = None
    unit: str | None = None
    statistical_period: str | None = None
    reporting_boundary: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    ambiguity_flags: list[str] = Field(default_factory=list)
    metric_id: str | None = None
    metric_version: str | None = None
    classification_status: str
    classification_reason: str = ""
    alternatives: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    skill_versions: dict[str, str] = Field(default_factory=dict)


class DraftCoverage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pages_scanned: list[int] = Field(default_factory=list)
    notes: str | None = None
    suspected_missing: list[str] = Field(default_factory=list)


class DraftExtractionBatch(BaseModel):
    """模型 JSON 的顶层结构（ExtractionBatch v1 的模型侧形态）。"""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = "1"
    candidates: list[DraftCandidate] = Field(default_factory=list)
    evidence: list[DraftEvidence] = Field(default_factory=list)
    coverage: DraftCoverage | None = None
    report_notes: str | None = None
