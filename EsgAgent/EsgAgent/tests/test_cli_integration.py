"""跨模块集成测试：PDF 入库 → ExtractionAgent（fake 模型）→ 15 列归类 CSV。

覆盖契约（docs/01、docs/07、app/core/catalog.py）：
- `IngestService.ingest_pdf` 只做大小/页数/哈希校验，原件不可变存储；
- `ExtractionAgent.extract` 从对象存储取 PDF → 渲染 Skill/Prompt → 一次模型调用 →
  草稿校验/ID 重映射 → 落盘 `ExtractionBatch v1`；
- `app.services.export` 产出阶段一归类结果表（表头必须逐字等于 `STAGE1_COLUMNS`）
  与候选人明细表（含 source_pages / source_evidence）；
- 同一 report 第二次 extract 命中本地批次缓存，不新增模型调用。

测试不访问网络、不依赖仓库内正式 skills/prompts：全部在 tmp_path 生成最小夹具，
模型通过 fake client 注入。
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pymupdf
import pytest

from app.adapters.object_store import LocalObjectStore
from app.adapters.qwen import QwenPDFClient
from app.agents.extraction import ExtractionAgent
from app.core.catalog import (
    COLUMN_METRIC_IDS,
    METRIC_NAMES,
    STAGE1_COLUMNS,
    STAGE1_METRIC_IDS,
)
from app.core.logging import RunLogger
from app.core.settings import Settings
from app.models.contracts import CompanyProfile, ExtractionBatch, MetricDefinition, ReportRef
from app.services.export import (
    CANDIDATE_COLUMNS,
    build_candidate_rows,
    build_stage1_row,
    write_candidate_csv,
    write_stage1_csv,
)
from app.services.ingest import IngestService
from app.services.prompt_registry import PromptRegistry
from app.services.skill_loader import SkillLoader
from tests.fakes.qwen_fake import FakeOpenAI, make_stream

API_KEY = "test-key-1234567890"
COMPANY = CompanyProfile(
    company_id="co_integration_0001",
    hkex_code="02460.HK",
    name="测试食品",
    name_en="Test Food Limited",
    name_en_short="TEST FOOD",
    industry_code="fb-nb",
    sasb_sector="Food & Beverage",
    sasb_industry="Non-Alcoholic Beverages",
    default_report_year=2025,
)

#: 指标 value_kind（与 app/core/catalog.py 的阶段一指标一致）
VALUE_KINDS: dict[str, str] = {
    "water_withdrawal": "VOLUME",
    "water_consumption": "VOLUME",
    "water_recycling_rate": "RATIO",
    "water_recycled_volume": "VOLUME",
    "water_discharge": "VOLUME",
    "water_intensity": "INTENSITY",
    "water_policy": "POLICY",
    "water_risk": "POLICY",
    "cod_emission": "MASS",
    "ammonia_nitrogen_emission": "MASS",
    "total_nitrogen_emission": "MASS",
    "total_phosphorus_emission": "MASS",
    "revenue": "MONEY",
}

CANONICAL_UNITS: dict[str, str] = {
    "VOLUME": "m³",
    "MASS": "t",
    "RATIO": "%",
    "MONEY": "百万元",
}

#: metric_id -> 阶段一 CSV 列名（由目录反查，保证与正式契约一致）
COLUMN_BY_METRIC: dict[str, str] = {metric_id: column for column, metric_id in COLUMN_METRIC_IDS.items()}

#: 模型返回的 8 个候选（覆盖换算/原样/比率/金额四类断言）。
#: 注意：value 与 unit 是模型原文，Python 只做展示换算，不改写 raw_value。
CANDIDATE_SPECS: list[dict[str, object]] = [
    {
        "metric_id": "water_withdrawal",
        "raw_value": "1,234",
        "unit": "万吨",  # → 12,340,000 m³
        "source_page": 1,
        "source_type": "TABLE",
        "source_evidence": "总取水量 1,234 万吨",
        "table_title": "水资源绩效",
        "row_header": "总取水量",
        "unit_header": "万吨",
    },
    {
        "metric_id": "water_consumption",
        "raw_value": "456,789",
        "unit": "立方米",  # 原样
        "source_page": 1,
        "source_type": "TABLE",
        "source_evidence": "总耗水量 456,789 立方米",
        "table_title": "水资源绩效",
        "row_header": "总耗水量",
        "unit_header": "立方米",
    },
    {
        "metric_id": "water_recycling_rate",
        "raw_value": "32.5",
        "unit": "%",
        "source_page": 2,
        "source_type": "TEXT",
        "source_evidence": "水循环利用率为 32.5%",
        "table_title": "水资源绩效",
        "row_header": "水循环利用率",
    },
    {
        "metric_id": "water_recycled_volume",
        "raw_value": "120,000",
        "unit": "立方米",
        "source_page": 2,
        "source_type": "TABLE",
        "source_evidence": "循环利用水量 120,000 立方米",
        "table_title": "水资源绩效",
        "row_header": "循环利用水量",
        "unit_header": "立方米",
    },
    {
        "metric_id": "water_discharge",
        "raw_value": "300,000",
        "unit": "立方米",
        "source_page": 2,
        "source_type": "TABLE",
        "source_evidence": "总排水量 300,000 立方米",
        "table_title": "水资源绩效",
        "row_header": "总排水量",
        "unit_header": "立方米",
    },
    {
        "metric_id": "cod_emission",
        "raw_value": "12.3",
        "unit": "吨",  # MASS 吨原样
        "source_page": 2,
        "source_type": "TABLE",
        "source_evidence": "COD 排放量 12.3 吨",
        "table_title": "污染物排放",
        "row_header": "COD 排放量",
        "unit_header": "吨",
    },
    {
        "metric_id": "ammonia_nitrogen_emission",
        "raw_value": "1.2",
        "unit": "吨",
        "source_page": 2,
        "source_type": "TABLE",
        "source_evidence": "氨氮排放量 1.2 吨",
        "table_title": "污染物排放",
        "row_header": "氨氮排放量",
        "unit_header": "吨",
    },
    {
        "metric_id": "revenue",
        "raw_value": "12.34",
        "unit": "亿元",  # → 1,234 百万元（CNY）
        "source_page": 1,
        "source_type": "TABLE",
        "source_evidence": "营业收入 12.34 亿元",
        "table_title": "财务摘要",
        "row_header": "营业收入",
        "unit_header": "亿元",
    },
]

COMMON_SKILL = """\
---
id: common.evidence
version: 1.0.0
kind: common
applies_to: []
depends_on: []
priority: 100
name: 证据与定位规则
---

# 证据与定位规则

每个候选至少关联一条可回看的证据，证据必须给出原 PDF 物理页码与逐字原文。
"""

INDUSTRY_SKILL = """\
---
id: industry.fb-nb
version: 1.0.0
kind: industry
applies_to: [fb-nb]
depends_on: []
priority: 90
name: 饮料行业披露模式
---

# 饮料行业披露模式（fb-nb）

关注饮料制造企业的取水、耗水、排水、水循环利用率与污染物排放披露。
"""

PROMPT_TEMPLATE = """\
你是 ESG 披露候选事实提取器。只使用所附 PDF 与本轮 Skill 片段。
报告年度：{{ report_context.report_year }}
报告文件：{{ report_context.source_filename }}
----- SKILLS -----
{{ skill_text }}
----- SKILLS END -----
输出符合 ExtractionBatch v1 的 JSON；每个候选必须给出逐字证据和 PDF 物理页码。
"""


# --------------------------------------------------------------------------- 夹具构造
def _metric_skill(metric_id: str) -> str:
    """生成一份最小 metric Skill（front matter 字段按 SkillLoader 要求）。"""
    kind = VALUE_KINDS[metric_id]
    lines = [
        "---",
        f"id: metric.{metric_id}",
        "version: 1.0.0",
        "kind: metric",
        "applies_to: []",
        "depends_on: []",
        "priority: 80",
        f"metric_ids: [{metric_id}]",
        f"name: {METRIC_NAMES[metric_id]}",
        f"value_kind: {kind}",
    ]
    canonical_unit = CANONICAL_UNITS.get(kind)
    if canonical_unit:
        lines.append(f"canonical_unit: {canonical_unit}")
    csv_column = COLUMN_BY_METRIC.get(metric_id)
    if csv_column:
        lines.append(f"csv_column: {csv_column}")
    lines.extend(["status: ACTIVE", "---", "", f"# {METRIC_NAMES[metric_id]}", "", f"识别与排除规则（{metric_id}）。", ""])
    return "\n".join(lines)


def _write_skills(skills_dir: Path) -> None:
    (skills_dir / "common").mkdir(parents=True, exist_ok=True)
    (skills_dir / "common" / "evidence.md").write_text(COMMON_SKILL, encoding="utf-8")

    (skills_dir / "industries" / "fb-nb").mkdir(parents=True, exist_ok=True)
    (skills_dir / "industries" / "fb-nb" / "recognition.md").write_text(INDUSTRY_SKILL, encoding="utf-8")

    for metric_id in STAGE1_METRIC_IDS:
        metric_dir = skills_dir / "metrics" / metric_id
        metric_dir.mkdir(parents=True, exist_ok=True)
        (metric_dir / "1.0.0.md").write_text(_metric_skill(metric_id), encoding="utf-8")


def _write_prompt(prompts_dir: Path) -> None:
    extraction_dir = prompts_dir / "extraction"
    extraction_dir.mkdir(parents=True, exist_ok=True)
    (extraction_dir / "v1.j2").write_text(PROMPT_TEMPLATE, encoding="utf-8")


def _make_pdf(path: Path, *, pages: int = 2) -> bytes:
    """生成一个 2 页最小 PDF（只用于入库校验，内容不会被后端解析）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page()
        page.insert_text((60, 80), f"Test Food ESG Report 2025 - page {index + 1}", fontsize=12)
        page.insert_text((60, 120), "Total water withdrawal 1,234 万吨 (Group, 2025)", fontsize=9)
    data = doc.tobytes()
    doc.close()
    path.write_bytes(data)
    return data


def _build_model_payload() -> dict:
    """构造模型侧 ExtractionBatch v1 JSON（Draft 契约：无 report_id/company_id）。"""
    evidence: list[dict] = []
    candidates: list[dict] = []
    for index, spec in enumerate(CANDIDATE_SPECS, start=1):
        evidence_id = f"ev_{index:03d}"
        evidence.append(
            {
                "evidence_id": evidence_id,
                "source_page": spec["source_page"],
                "source_type": spec["source_type"],
                "source_evidence": spec["source_evidence"],
                "table_title": spec.get("table_title"),
                "row_header": spec.get("row_header"),
                "column_header": "2025 年",
                "unit_header": spec.get("unit_header"),
                "context_text": "2025 年集团口径",
            }
        )
        candidates.append(
            {
                "candidate_id": f"ca_{index:03d}",
                "data_year": 2025,
                "raw_value": spec["raw_value"],
                "raw_text": None,
                "unit": spec["unit"],
                "statistical_period": "2025-01-01/2025-12-31",
                "reporting_boundary": {"level": "GROUP", "description": "集团"},
                "evidence_ids": [evidence_id],
                "ambiguity_flags": [],
                "metric_id": spec["metric_id"],
                "metric_version": "1.0.0",
                "classification_status": "CLASSIFIED",
                "classification_reason": (
                    f"表格「{spec.get('table_title')}」行「{spec.get('row_header')}」逐字证据，"
                    f"单位为 {spec['unit']}，2025 年集团口径"
                ),
                "alternatives": [],
                "confidence": 0.9,
            }
        )
    return {
        "schema_version": "1",
        "candidates": candidates,
        "evidence": evidence,
        "coverage": {"pages_scanned": [1, 2], "notes": "逐页阅读", "suspected_missing": []},
        "report_notes": "",
    }


@dataclass
class Pipeline:
    settings: Settings
    store: LocalObjectStore
    ingest: IngestService
    agent: ExtractionAgent
    skills: SkillLoader
    registry: dict[str, MetricDefinition]
    fake: FakeOpenAI
    report: ReportRef
    pdf_bytes: bytes


@pytest.fixture
def pipeline(tmp_path: Path) -> Pipeline:
    settings = Settings(dashscope_api_key=API_KEY, project_root=tmp_path)
    _write_skills(settings.skills_dir)
    _write_prompt(settings.prompts_dir)

    pdf_path = tmp_path / "source" / "测试食品_2025_ESG报告.pdf"
    pdf_bytes = _make_pdf(pdf_path)
    store = LocalObjectStore(settings.objects_dir)
    ingest = IngestService(settings, store)
    report = ingest.ingest_pdf(pdf_path, COMPANY, 2025)

    skills = SkillLoader(settings.skills_dir)
    prompts = PromptRegistry(settings.prompts_dir)
    run_logger = RunLogger(settings.log_dir, "itest")
    model_text = json.dumps(_build_model_payload(), ensure_ascii=False)
    fake = FakeOpenAI([make_stream(model_text, request_id="req_itest")])
    qwen = QwenPDFClient(settings, run_logger=run_logger, client=fake)
    agent = ExtractionAgent(settings, store, skills, prompts, qwen, run_logger)

    return Pipeline(
        settings=settings,
        store=store,
        ingest=ingest,
        agent=agent,
        skills=skills,
        registry=skills.load_registry(),
        fake=fake,
        report=report,
        pdf_bytes=pdf_bytes,
    )


def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows, f"CSV 为空: {path}"
    return rows[0], rows[1:]


# --------------------------------------------------------------------------- 主链路
def test_ingest_extract_export_csv_contract(pipeline: Pipeline, tmp_path: Path) -> None:
    # 1) 入库：不可变对象 + 2 页元数据 + sha256
    report = pipeline.report
    assert report.report_id.startswith("rp_")
    assert report.page_count == 2
    assert report.sha256 == hashlib.sha256(pipeline.pdf_bytes).hexdigest()
    assert report.object_uri == f"local://pdf/{report.sha256}.pdf"
    assert pipeline.store.exists(report.object_uri) is True
    assert pipeline.ingest.read_pdf(report) == pipeline.pdf_bytes

    # 2) 提取：一次 fake 模型调用，Prompt 由 tmp 夹具的 Skill/Prompt 渲染
    outcome = pipeline.agent.extract(report, COMPANY)
    batch = outcome.batch
    assert outcome.from_cache is False
    assert len(batch.candidates) == len(CANDIDATE_SPECS) == 8
    assert len(batch.evidence) == len(CANDIDATE_SPECS)
    assert batch.report_id == report.report_id
    assert {c.metric_id for c in batch.candidates} == {spec["metric_id"] for spec in CANDIDATE_SPECS}
    assert all(c.classification_status == "CLASSIFIED" for c in batch.candidates)
    assert all(c.reporting_boundary.get("level") == "GROUP" for c in batch.candidates)

    assert len(pipeline.fake.calls) == 1
    call = pipeline.fake.calls[0]
    assert call["model"] == "qwen3.8-flash"
    assert call["stream"] is True
    content = call["messages"][0]["content"]
    assert content[0]["type"] == "file"
    # 本地对象存储 public_url() 为 None → 必须回退到 base64 file_data
    assert content[0]["file"]["filename"] == report.source_filename
    assert content[0]["file"]["file_data"].startswith("data:application/pdf;base64,")
    prompt_text = content[1]["text"]
    assert "2025" in prompt_text  # report_context.report_year 已注入
    assert "common.evidence" in prompt_text  # skill_text 已注入
    assert "metric.water_withdrawal" in prompt_text

    # 3) 批次 JSON 落盘且可反序列化
    batch_path = Path(pipeline.settings.batches_dir) / f"{report.report_id}.json"
    assert batch_path.is_file()
    restored = ExtractionBatch.model_validate_json(batch_path.read_text(encoding="utf-8"))
    assert restored.batch_id == batch.batch_id
    assert restored.report_id == report.report_id
    assert len(restored.candidates) == 8
    raw_path = Path(pipeline.settings.batches_dir) / f"{report.report_id}.raw.json"
    assert raw_path.is_file()
    raw_payload = json.loads(raw_path.read_text(encoding="utf-8"))
    assert raw_payload["model"] == "qwen3.8-flash"
    assert raw_payload["request_id"] == "req_itest"
    assert json.loads(raw_payload["text"])["candidates"]

    # 4) 导出阶段一归类结果表（15 列，表头逐字契约）
    out_dir = tmp_path / "outputs"
    prefix = f"{COMPANY.hkex_code.split('.')[0]}_{COMPANY.name}_{report.report_year}"
    row = build_stage1_row(report, COMPANY, batch, pipeline.registry)
    stage1_path = write_stage1_csv([row], out_dir / f"{prefix}_归类结果.csv")

    header, data_rows = _read_csv(stage1_path)
    assert header == STAGE1_COLUMNS
    assert len(header) == 15
    assert len(data_rows) == 1 and len(data_rows[0]) == 15
    values = dict(zip(header, data_rows[0]))
    assert values["股票代码"] == COMPANY.hkex_code.split(".")[0]
    assert values["公司名称"] == (COMPANY.name_en_short or COMPANY.name)
    assert values["SASB Sector"] == COMPANY.sasb_sector
    # 万吨 → m³
    assert values["总取水量（m³）"] == "12,340,000"
    assert values["总耗水量（m³）"] == "456,789"
    assert values["水循环利用率（%）"] == "32.5%"
    assert values["循环利用水量（m³）"] == "120,000"
    assert values["总排水量（m³）"] == "300,000"
    # COD 吨原样；氨氮吨原样
    assert values["COD 排放量（t）"] == "12.3"
    assert values["氨氮排放量（t）"] == "1.2"
    # 亿元 → 百万元 + 币种
    assert values["营业收入（百万）"] == "1234.000"
    assert values["收入币种"] == "CNY"
    # 模型未给的指标留空待人工确认
    assert values["总氮排放量（t）"] == "N/A"
    assert values["总磷排放量（t）"] == "N/A"
    # 内存行与 CSV 行一致
    assert row.values == values

    # 5) 候选人明细 CSV：含 source_pages / source_evidence 与归一化值
    candidate_rows = build_candidate_rows(report, COMPANY, batch, pipeline.registry)
    assert len(candidate_rows) == 8
    candidate_path = write_candidate_csv(candidate_rows, out_dir / f"{prefix}_候选人明细.csv")
    cand_header, cand_data = _read_csv(candidate_path)
    assert cand_header == CANDIDATE_COLUMNS
    assert "source_pages" in cand_header
    assert "source_evidence" in cand_header
    by_metric = {row_dict["metric_id"]: row_dict for row_dict in (dict(zip(cand_header, r)) for r in cand_data)}
    assert len(by_metric) == 8

    withdrawal = by_metric["water_withdrawal"]
    assert withdrawal["source_pages"] == "1"
    assert withdrawal["source_evidence"] == "总取水量 1,234 万吨"
    assert withdrawal["raw_value"] == "1,234"
    assert withdrawal["raw_unit"] == "万吨"
    assert withdrawal["normalized_value"] == "12,340,000"
    assert withdrawal["normalized_unit"] == "m³"

    cod = by_metric["cod_emission"]
    assert cod["source_pages"] == "2"
    assert cod["source_evidence"] == "COD 排放量 12.3 吨"
    assert cod["normalized_value"] == "12.3"
    assert cod["normalized_unit"] == "t"

    revenue = by_metric["revenue"]
    assert revenue["raw_unit"] == "亿元"
    assert revenue["normalized_value"] == "1,234"
    assert revenue["normalized_unit"] == "百万元"

    # 6) 导出的 JSON 同样可反序列化
    exported_json = out_dir / f"{prefix}_extraction.json"
    exported_json.write_text(batch.model_dump_json(indent=2), encoding="utf-8")
    assert ExtractionBatch.model_validate_json(exported_json.read_text(encoding="utf-8")).report_id == report.report_id

    # 7) 模型调用已写入 RunLogger（只含元数据，可追踪）
    log_path = Path(pipeline.settings.log_dir) / "itest.jsonl"
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    model_calls = [record for record in records if record.get("event") == "model_call"]
    assert len(model_calls) == 1
    assert model_calls[0]["status"] == "SUCCESS"
    assert model_calls[0]["pdf_sha256"] == report.sha256
    assert any(record.get("event") == "extraction_finished" for record in records)


def test_second_extract_uses_cache_without_model_call(pipeline: Pipeline) -> None:
    first = pipeline.agent.extract(pipeline.report, COMPANY)
    calls_after_first = len(pipeline.fake.calls)
    assert calls_after_first == 1

    second = pipeline.agent.extract(pipeline.report, COMPANY)

    assert second.from_cache is True
    assert len(pipeline.fake.calls) == calls_after_first  # 命中缓存不新增模型调用
    assert second.batch.batch_id == first.batch.batch_id
    assert second.batch.report_id == pipeline.report.report_id

    batch_files = [
        path
        for path in Path(pipeline.settings.batches_dir).glob("*.json")
        if not path.name.endswith(".raw.json")
    ]
    assert len(batch_files) == 1
