"""ExtractionAgent 单测：用 fake 模型/存储，不访问网络。"""

from __future__ import annotations

import json
import time
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents.extraction import ExtractionAgent, extract_json_object, stats
from app.core.catalog import METRIC_NAMES, STAGE1_METRIC_IDS
from app.core.errors import AppError, ErrorCode
from app.core.settings import Settings
from app.models.contracts import CompanyProfile, MetricDefinition, ReportRef

VALUE_KINDS = {
    "water_withdrawal": "VOLUME",
    "water_consumption": "VOLUME",
    "water_recycling_rate": "RATIO",
    "water_recycled_volume": "VOLUME",
    "water_discharge": "VOLUME",
    "water_intensity": "INTENSITY",
    "water_policy": "POLICY",
    "water_risk": "NARRATIVE",
    "cod_emission": "MASS",
    "ammonia_nitrogen_emission": "MASS",
    "total_nitrogen_emission": "MASS",
    "total_phosphorus_emission": "MASS",
    "revenue": "MONEY",
}


class FakeStore:
    def __init__(self) -> None:
        self.public_url_calls = 0

    def get_bytes(self, uri: str) -> bytes:
        return b"%PDF-1.4 fake pdf content"

    def public_url(self, uri: str, ttl_s: int = 900):
        self.public_url_calls += 1
        return None


class FakeSkills:
    def load_registry(self) -> dict[str, MetricDefinition]:
        return {
            mid: MetricDefinition(
                metric_id=mid,
                version="1.0.0",
                name=METRIC_NAMES[mid],
                value_kind=VALUE_KINDS[mid],
                canonical_unit="m³",
            )
            for mid in STAGE1_METRIC_IDS
        }

    def resolve(self, task: str, industry_code: str, metric_ids: list[str], max_tokens: int = 12000):
        return SimpleNamespace(
            text="## [common/evidence@1.0.0] 证据规则\n证据必须逐字。",
            digest="d" * 64,
            versions={"common/evidence": "1.0.0"},
            metric_versions={mid: "1.0.0" for mid in metric_ids},
            files=["common/evidence.md"],
            token_estimate=20,
            omitted=[],
        )


class FakePrompts:
    def render(self, name: str, version: str = "v1", **context):
        return SimpleNamespace(text=f"PROMPT::{name}::{version}::{len(context)}", version=version, digest="p" * 64)


class FakeQwen:
    def __init__(
        self,
        text: str,
        repair_text: str | None = None,
        *,
        delay: float = 0.0,
        fail_filename_contains: str | None = None,
        fail_times: int = 1,
        fail_retryable: bool = False,
    ) -> None:
        self.text = text
        self.repair_text = repair_text
        self.delay = delay
        self.fail_filename_contains = fail_filename_contains
        self.fail_times = fail_times
        self.fail_retryable = fail_retryable
        self.extract_calls = 0
        self.repair_calls = 0
        self._fail_counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def extract_pdf(self, pdf_bytes, filename, prompt, *, max_tokens=None, temperature=None, file_url=None):
        with self._lock:
            self.extract_calls += 1
            call_no = self.extract_calls
        if self.delay:
            time.sleep(self.delay)
        if self.fail_filename_contains and self.fail_filename_contains in filename:
            with self._lock:
                count = self._fail_counts.get(filename, 0) + 1
                self._fail_counts[filename] = count
            if count <= self.fail_times:
                raise AppError(ErrorCode.MODEL_BAD_JSON, "fake 分块失败", retryable=self.fail_retryable)
        return SimpleNamespace(
            text=self.text,
            model="qwen3.8-flash",
            request_id=f"req-test-{call_no}",
            latency_ms=1234,
            usage={"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200},
            finish_reason="stop",
            attempts=1,
            first_token_ms=800,
        )

    def repair_json(self, broken_text: str, validation_error: str) -> str:
        self.repair_calls += 1
        if self.repair_text is None:
            raise AppError(ErrorCode.MODEL_BAD_JSON, "修复失败")
        return self.repair_text


def make_settings(tmp_path: Path) -> Settings:
    return Settings(dashscope_api_key="test-key-1234567890", project_root=tmp_path)


def make_report(page_count: int = 70) -> ReportRef:
    return ReportRef(
        report_id="rp_test",
        company_id="co_test",
        report_year=2025,
        sha256="a" * 64,
        object_uri="local://pdf/test.pdf",
        page_count=page_count,
        source_filename="华润饮料 2025.pdf",
        size_bytes=1024,
    )


def make_company() -> CompanyProfile:
    return CompanyProfile(
        company_id="co_test",
        hkex_code="02460.HK",
        name="华润饮料",
        industry_code="fb-nb",
        sasb_sector="Food & Beverage",
        sasb_industry="Non-Alcoholic Beverages",
    )


def make_payload(**candidate_overrides) -> str:
    candidate = {
        "candidate_id": "ca_001",
        "data_year": 2025,
        "raw_value": "1,366.20",
        "unit": "万吨",
        "statistical_period": "2025-01-01/2025-12-31",
        "reporting_boundary": {"level": "GROUP", "description": "集团"},
        "evidence_ids": ["ev_001"],
        "ambiguity_flags": [],
        "metric_id": "water_withdrawal",
        "metric_version": "1.0.0",
        "classification_status": "CLASSIFIED",
        "classification_reason": "附录 KPI 表行名为“年度新鲜水用水量”，单位为万吨，2025 年集团口径",
        "alternatives": [],
        "confidence": 0.92,
    }
    candidate.update(candidate_overrides)
    payload = {
        "schema_version": "1",
        "candidates": [candidate],
        "evidence": [
            {
                "evidence_id": "ev_001",
                "source_page": 61,
                "source_type": "TABLE",
                "source_evidence": "年度新鲜水用水量 万吨 799.20 1,298.03 1,366.20",
                "table_title": "环境绩效指标",
                "row_header": "年度新鲜水用水量",
                "column_header": "2025 年",
                "unit_header": "万吨",
            }
        ],
        "coverage": {"pages_scanned": list(range(1, 71)), "notes": "逐页阅读", "suspected_missing": []},
        "report_notes": "",
    }
    return json.dumps(payload, ensure_ascii=False)


def build_agent(tmp_path: Path, qwen: FakeQwen):
    settings = make_settings(tmp_path)
    return ExtractionAgent(settings, FakeStore(), FakeSkills(), FakePrompts(), qwen), settings


def test_extract_success(tmp_path: Path):
    agent, settings = build_agent(tmp_path, FakeQwen(make_payload()))
    outcome = agent.extract(make_report(), make_company())
    batch = outcome.batch
    assert len(batch.candidates) == 1
    assert len(batch.evidence) == 1
    c = batch.candidates[0]
    assert c.report_id == "rp_test"
    assert c.company_id == "co_test"
    assert c.candidate_id.startswith("ca_")
    assert c.evidence_ids[0].startswith("ev_")
    assert c.metric_id == "water_withdrawal"
    assert c.metric_version == "1.0.0"
    assert c.classification_status == "CLASSIFIED"
    assert batch.evidence[0].source_page == 61
    assert batch.skill_digest == "d" * 64
    assert stats(batch)["candidates"] == 1
    # 原始响应与批次都已落盘
    assert (Path(settings.batches_dir) / "rp_test.raw.json").exists()
    assert (Path(settings.batches_dir) / "rp_test.json").exists()


def test_extract_uses_cache_unless_force(tmp_path: Path):
    qwen = FakeQwen(make_payload())
    agent, _ = build_agent(tmp_path, qwen)
    agent.extract(make_report(), make_company())
    assert qwen.extract_calls == 1
    second = agent.extract(make_report(), make_company())
    assert second.from_cache is True
    assert qwen.extract_calls == 1
    agent.extract(make_report(), make_company(), force=True)
    assert qwen.extract_calls == 2


def test_page_out_of_range_becomes_needs_review(tmp_path: Path):
    payload = json.loads(make_payload())
    payload["evidence"][0]["source_page"] = 999
    agent, _ = build_agent(tmp_path, FakeQwen(json.dumps(payload, ensure_ascii=False)))
    batch = agent.extract(make_report(), make_company()).batch
    ev = batch.evidence[0]
    assert ev.locator_method == "UNVERIFIED"
    assert ev.source_page == 70  # 夹到合法范围
    c = batch.candidates[0]
    assert c.classification_status == "NEEDS_REVIEW"
    assert "PAGE_OUT_OF_RANGE" in c.ambiguity_flags


def test_unknown_metric_downgraded_to_unknown(tmp_path: Path):
    agent, _ = build_agent(tmp_path, FakeQwen(make_payload(metric_id="not_registered", metric_version="1.0.0")))
    batch = agent.extract(make_report(), make_company()).batch
    c = batch.candidates[0]
    assert c.metric_id is None
    assert c.metric_version is None
    assert c.classification_status == "UNKNOWN"
    assert "UNKNOWN_METRIC" in c.ambiguity_flags


def test_bad_json_repaired_once(tmp_path: Path):
    qwen = FakeQwen("这不是 JSON", repair_text=make_payload())
    agent, _ = build_agent(tmp_path, qwen)
    batch = agent.extract(make_report(), make_company()).batch
    assert qwen.repair_calls == 1
    assert len(batch.candidates) == 1


def test_bad_json_repair_failure_raises(tmp_path: Path):
    agent, _ = build_agent(tmp_path, FakeQwen("这不是 JSON", repair_text=None))
    with pytest.raises(AppError) as exc:
        agent.extract(make_report(), make_company())
    assert exc.value.code == ErrorCode.MODEL_BAD_JSON
    assert exc.value.retryable is False


def test_candidate_without_valid_evidence_dropped(tmp_path: Path):
    payload = json.loads(make_payload())
    payload["candidates"][0]["evidence_ids"] = ["ev_missing"]
    agent, _ = build_agent(tmp_path, FakeQwen(json.dumps(payload, ensure_ascii=False)))
    with pytest.raises(AppError) as exc:
        agent.extract(make_report(), make_company())
    assert exc.value.code == ErrorCode.EVIDENCE_NOT_LOCATED


def test_classified_without_metric_degrades(tmp_path: Path):
    agent, _ = build_agent(tmp_path, FakeQwen(make_payload(metric_id=None, metric_version=None)))
    batch = agent.extract(make_report(), make_company()).batch
    c = batch.candidates[0]
    assert c.classification_status == "UNKNOWN"
    assert "CLASSIFIED_WITHOUT_METRIC" in c.ambiguity_flags


def test_extract_json_object_tolerates_fences_and_trailing_comma():
    text = '说明文字\n```json\n{"a": 1, "b": [1, 2,],}\n```\n结束'
    assert extract_json_object(text) == {"a": 1, "b": [1, 2]}


def test_extract_json_object_rejects_garbage():
    with pytest.raises(ValueError):
        extract_json_object("完全不是 JSON")


def test_chunked_extraction_offsets_pages_and_merges(tmp_path: Path, monkeypatch):
    """超过单次解析上限时按页分块：页码回填原报告物理页，分块信息写入 coverage。"""
    from app.services.chunker import PdfChunk

    def fake_split(pdf_bytes, chunk_pages):
        return [
            PdfChunk(index=0, start_page=1, end_page=63, page_count=63, pdf_bytes=b"%PDF-fake-1"),
            PdfChunk(index=1, start_page=64, end_page=100, page_count=37, pdf_bytes=b"%PDF-fake-2"),
        ]

    monkeypatch.setattr("app.agents.extraction.split_pdf", fake_split)
    qwen = FakeQwen(make_payload())
    agent, _ = build_agent(tmp_path, qwen)
    report = make_report(page_count=100)  # > 默认阈值 80 → 分 2 块（63 + 37）
    outcome = agent.extract(report, make_company())
    assert qwen.extract_calls == 2
    batch = outcome.batch
    assert len(batch.candidates) == 2
    # 第 1 块原页 61 保持不变；第 2 块只有 37 页（原 64-100），61 越界后夹到 100 并标 UNVERIFIED
    assert sorted(e.source_page for e in batch.evidence) == [61, 100]
    assert any(e.locator_method == "UNVERIFIED" for e in batch.evidence)
    assert "分块提取" in (batch.coverage.notes or "")
    pages = batch.coverage.pages_scanned
    assert pages == sorted(set(pages))
    assert min(pages) == 1 and max(pages) == 100


# ----------------------------------------------------------------- 分块并行
def _fake_chunks(count: int, pages: int = 63, total: int = 189):
    from app.services.chunker import PdfChunk

    chunks = []
    start = 1
    for index in range(count):
        end = min(start + pages - 1, total)
        chunks.append(
            PdfChunk(
                index=index,
                start_page=start,
                end_page=end,
                page_count=end - start + 1,
                pdf_bytes=f"%PDF-fake-{index}".encode(),
            )
        )
        start = end + 1
    return chunks


def test_chunk_parallel_faster_than_serial(tmp_path: Path, monkeypatch):
    """3 块 × 0.35s 延迟：并发墙钟应显著短于串行总和。"""
    monkeypatch.setattr("app.agents.extraction.split_pdf", lambda pdf, n: _fake_chunks(3))
    qwen = FakeQwen(make_payload(), delay=0.35)
    agent, _ = build_agent(tmp_path, qwen)
    report = make_report(page_count=189)

    t0 = time.perf_counter()
    agent.extract(report, make_company(), concurrency=3)
    parallel = time.perf_counter() - t0

    t0 = time.perf_counter()
    agent.extract(report, make_company(), concurrency=1, force=True)
    serial = time.perf_counter() - t0

    assert qwen.extract_calls == 6  # 两次运行各 3 块
    assert parallel < serial * 0.75, f"parallel={parallel:.2f}s serial={serial:.2f}s"


def test_parallel_merge_equals_serial(tmp_path: Path, monkeypatch):
    """并发与串行的候选/证据/覆盖页集合必须一致（仅 ID 不同）。"""
    monkeypatch.setattr("app.agents.extraction.split_pdf", lambda pdf, n: _fake_chunks(3))
    qwen = FakeQwen(make_payload())
    agent, _ = build_agent(tmp_path, qwen)
    report = make_report(page_count=189)

    serial_batch = agent.extract(report, make_company(), concurrency=1).batch
    parallel_batch = agent.extract(report, make_company(), concurrency=3, force=True).batch

    def candidate_sig(batch):
        ev = {e.evidence_id: e for e in batch.evidence}
        return {
            (
                c.metric_id,
                c.raw_value,
                c.unit,
                c.data_year,
                c.classification_status,
                tuple(sorted(ev[eid].source_page for eid in c.evidence_ids)),
            )
            for c in batch.candidates
        }

    assert candidate_sig(serial_batch) == candidate_sig(parallel_batch)
    assert {(e.source_page, e.source_evidence) for e in serial_batch.evidence} == {
        (e.source_page, e.source_evidence) for e in parallel_batch.evidence
    }
    assert serial_batch.coverage.pages_scanned == parallel_batch.coverage.pages_scanned


def test_chunk_failure_raises_with_chunk_details(tmp_path: Path, monkeypatch):
    """某个分块失败时不落半成品批次，错误里带块范围，已完成块的 raw 保留。"""
    monkeypatch.setattr("app.agents.extraction.split_pdf", lambda pdf, n: _fake_chunks(3))
    qwen = FakeQwen(make_payload(), fail_filename_contains="64-126")
    agent, settings = build_agent(tmp_path, qwen)
    report = make_report(page_count=189)

    with pytest.raises(AppError) as exc:
        agent.extract(report, make_company(), concurrency=3)

    assert exc.value.code == ErrorCode.MODEL_BAD_JSON
    chunk_errors = exc.value.details.get("chunk_errors") or []
    assert len(chunk_errors) == 1
    assert chunk_errors[0]["pages"] == "64-126"
    raw_path = Path(settings.batches_dir) / f"{report.report_id}.raw.json"
    assert raw_path.exists()
    assert "errors" in json.loads(raw_path.read_text(encoding="utf-8"))
    assert not (Path(settings.batches_dir) / f"{report.report_id}.json").exists()


def test_retryable_chunk_failure_is_retried_in_place(tmp_path: Path, monkeypatch):
    """可重试的分块失败（如瞬时 APIError）会立即重跑该块一次，其他块不受影响。"""
    monkeypatch.setattr("app.agents.extraction.split_pdf", lambda pdf, n: _fake_chunks(3))
    qwen = FakeQwen(
        make_payload(),
        fail_filename_contains="64-126",
        fail_times=1,
        fail_retryable=True,
    )
    agent, _ = build_agent(tmp_path, qwen)
    outcome = agent.extract(make_report(page_count=189), make_company(), concurrency=3)
    assert qwen.extract_calls == 4  # 3 块 + 1 次重试
    assert len(outcome.batch.candidates) >= 1
    assert len(outcome.batch.coverage.pages_scanned) > 0
