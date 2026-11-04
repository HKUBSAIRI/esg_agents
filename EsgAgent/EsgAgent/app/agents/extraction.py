"""ExtractionAgent：整份 PDF → 模型调用 → 候选数值 + 证据 + 指标归类。

约束（docs/01）：
- 提取阶段不运行本地 PDF 文本/OCR，模型直接读 PDF；
- 优先单次提交一份完整 PDF。平台单次 PDF 解析存在实际上限（实测 <=70 页可、>=90 页
  返回 file format illegal），超过阈值时按页分块多次调用，页码回填原报告物理页码；
  这是平台限制下的降级方案，写入 coverage.notes 并在日志中记录；
- 坏 JSON 至多一次修复调用；仍失败保留原响应并转人工；
- 无法确定时使用 UNKNOWN / NEEDS_REVIEW / INSUFFICIENT_EVIDENCE，禁止硬归类。
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from app.core.catalog import STAGE1_METRIC_IDS
from app.core.errors import AppError, ErrorCode
from app.core.ids import new_batch_id, new_candidate_id, new_evidence_id, new_model_call_id
from app.core.logging import RunLogger, get_logger
from app.models.contracts import (
    CompanyProfile,
    Coverage,
    Evidence,
    ExtractionBatch,
    ExtractionCandidate,
    MetricDefinition,
    ReportRef,
)
from app.models.model_output import DraftExtractionBatch
from app.services.chunker import PdfChunk, split_pdf, MAX_INLINE_PDF_BYTES

logger = get_logger(__name__)

VALID_STATUS = {"CLASSIFIED", "UNKNOWN", "NEEDS_REVIEW", "INSUFFICIENT_EVIDENCE"}
VALID_SOURCE_TYPES = {"TEXT", "TABLE", "CHART", "IMAGE"}


@dataclass
class ExtractionOutcome:
    batch: ExtractionBatch
    warnings: list[str] = field(default_factory=list)
    from_cache: bool = False


@dataclass
class _CallResult:
    candidates: list[ExtractionCandidate]
    evidence: list[Evidence]
    coverage: Coverage
    warnings: list[str]
    raw: dict[str, Any]


def extract_json_object(text: str) -> dict[str, Any]:
    """从模型输出中稳健地取出 JSON 对象（容忍代码块、前后说明、尾随逗号）。"""
    if not text or not text.strip():
        raise ValueError("模型输出为空")
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.S)
    if fence:
        candidate = fence.group(1).strip()
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("未找到 JSON 对象边界")
    payload = candidate[start : end + 1]
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        cleaned = re.sub(r",\s*([}\]])", r"\1", payload)
        obj = json.loads(cleaned)
    if not isinstance(obj, dict):
        raise ValueError("顶层 JSON 不是对象")
    return obj


class ExtractionAgent:
    def __init__(
        self,
        settings,
        store,
        skills,
        prompts,
        qwen,
        run_logger: RunLogger | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.skills = skills
        self.prompts = prompts
        self.qwen = qwen
        self.log = run_logger or RunLogger(settings.log_dir, "extraction")

    # ------------------------------------------------------------------ 对外接口
    def extract(
        self,
        report: ReportRef,
        company: CompanyProfile,
        *,
        force: bool = False,
        file_url: str | None = None,
        concurrency: int | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> ExtractionOutcome:
        """从对象存储读取原 PDF，调用 qwen3.8-flash 完成提取 + 归类，返回并持久化候选批次。"""
        cached = self._load_cached(report)
        if cached is not None and not force:
            logger.info("命中提取缓存 report_id=%s", report.report_id)
            return ExtractionOutcome(batch=cached, warnings=["从本地缓存加载（--force 可重新提取）"], from_cache=True)

        pdf_bytes = self.store.get_bytes(report.object_uri)
        registry = self.skills.load_registry()
        bundle = self.skills.resolve(
            task="extraction",
            industry_code=company.industry_code,
            metric_ids=STAGE1_METRIC_IDS,
            max_tokens=24000,
        )
        base_context = {
            "report_id": report.report_id,
            "company_id": company.company_id,
            "company_name": company.name,
            "company_name_en": company.name_en or "",
            "report_year": report.report_year,
            "page_count": report.page_count,
            "industry_code": company.industry_code,
            "sasb_sector": company.sasb_sector,
            "sasb_industry": company.sasb_industry,
            "source_filename": report.source_filename,
        }
        rendered = self.prompts.render(
            "extraction",
            version=self.settings.prompt_version,
            skill_text=bundle.text,
            report_context=base_context,
            metric_ids=STAGE1_METRIC_IDS,
            schema_version=self.settings.schema_version,
            prompt_version=self.settings.prompt_version,
            chunk_info=None,
        )

        if file_url is None and hasattr(self.store, "public_url"):
            file_url = self.store.public_url(report.object_uri)

        threshold = int(getattr(self.settings, "extract_chunk_threshold", 80))
        chunk_pages = int(getattr(self.settings, "extract_chunk_pages", 63))
        concurrency = max(
            1,
            min(int(concurrency or getattr(self.settings, "extract_chunk_concurrency", 3)), 4),
        )
        use_chunks = report.page_count > threshold or (not file_url and len(pdf_bytes)>MAX_INLINE_PDF_BYTES)
        started_perf = time.perf_counter()
        self.log.event(
            "extraction_started",
            report_id=report.report_id,
            company_id=company.company_id,
            pdf_bytes=len(pdf_bytes),
            page_count=report.page_count,
            model=self.settings.qwen_model,
            prompt_version=rendered.version,
            prompt_digest=rendered.digest,
            skill_digest=bundle.digest,
            skill_files=len(bundle.files),
            skill_omitted=bundle.omitted,
            transport="file_url" if file_url else "file_data_base64",
            chunked=use_chunks,
            chunk_pages=chunk_pages if use_chunks else None,
            concurrency=concurrency if use_chunks else None,
        )
        logger.info(
            "调用 %s 提取 %s (%d 页, %.1f MB, prompt=%d 字符, chunked=%s)",
            self.settings.qwen_model,
            report.source_filename,
            report.page_count,
            len(pdf_bytes) / 1024 / 1024,
            len(rendered.text),
            use_chunks,
        )

        if not use_chunks:
            calls = [
                self._call_once(
                    pdf_bytes,
                    report.source_filename,
                    report,
                    company,
                    registry,
                    bundle,
                    base_context,
                    file_url=file_url,
                    chunk=None,
                )
            ]
        else:
            # 分块时 file_url 指向整份 PDF，无法复用；每块改用 base64 传输。
            chunks = split_pdf(pdf_bytes, chunk_pages)
            budget=getattr(self.settings,"extract_max_calls",None)
            if budget is not None and len(chunks)>budget:
                raise ValueError("ESGAGENT_CALL_BUDGET_EXHAUSTED")
            logger.info(
                "PDF 超过单次解析上限 %d 页，分 %d 块提取（并发 %d）：%s",
                threshold,
                len(chunks),
                min(concurrency, len(chunks)),
                ", ".join(f"{c.start_page}-{c.end_page}" for c in chunks),
            )
            calls = self._run_chunks(
                chunks,
                report,
                company,
                registry,
                bundle,
                base_context,
                concurrency=min(concurrency, len(chunks)),
                progress=progress,
            )

        evidence: list[Evidence] = []
        candidates: list[ExtractionCandidate] = []
        warnings: list[str] = []
        for call in calls:
            evidence.extend(call.evidence)
            candidates.extend(call.candidates)
            warnings.extend(call.warnings)
        evidence, candidates = _deduplicate(evidence, candidates)
        coverage = _merge_coverage(calls, report.page_count, chunked=use_chunks, chunk_pages=chunk_pages)
        if not candidates:
            raise AppError(
                ErrorCode.EVIDENCE_NOT_LOCATED,
                "模型未返回任何可用候选（可能输出被截断或全部缺少证据），转人工处理",
                retryable=False,
                details={"report_id": report.report_id, "raw_path": str(self._raw_path(report))},
            )

        batch = ExtractionBatch(
            batch_id=new_batch_id(),
            report_id=report.report_id,
            company_id=company.company_id,
            report_year=report.report_year,
            model=self.settings.qwen_model,
            prompt_version=rendered.version,
            skill_digest=bundle.digest,
            candidates=candidates,
            evidence=evidence,
            coverage=coverage,
        )
        self._persist(report, batch)
        raw_payload: dict[str, Any]
        raw_payload = calls[0].raw if len(calls) == 1 else {"chunked": True, "calls": [c.raw for c in calls]}
        self._write_raw(report, raw_payload)
        self.log.event(
            "extraction_finished",
            report_id=report.report_id,
            candidates=len(batch.candidates),
            evidence=len(batch.evidence),
            calls=len(calls),
            usage=_sum_usage(calls),
            elapsed_ms=int((time.perf_counter() - started_perf) * 1000),
            chunk_latencies_ms=[c.raw.get("latency_ms") for c in calls],
            concurrency=min(concurrency, len(calls)) if use_chunks else 1,
            warnings=warnings,
        )
        return ExtractionOutcome(batch=batch, warnings=warnings)

    # ------------------------------------------------------------------ 分块并行
    def _run_chunks(
        self,
        chunks: list[PdfChunk],
        report: ReportRef,
        company: CompanyProfile,
        registry: dict[str, MetricDefinition],
        bundle,
        base_context: dict,
        *,
        concurrency: int,
        progress: Callable[[str], None] | None,
    ) -> list[_CallResult]:
        """并行执行分块调用，按块顺序返回结果；可重试失败立即重跑一次（不阻塞其他块）。"""
        total = len(chunks)
        workers = max(1, min(concurrency, total))

        def work(index: int, chunk: PdfChunk) -> _CallResult:
            if progress:
                progress(
                    f"[chunk {index + 1}/{total}] 页 {chunk.start_page}-{chunk.end_page} 开始"
                )
            started = time.perf_counter()
            result = self._call_once(
                chunk.pdf_bytes,
                f"{report.source_filename}#p{chunk.start_page}-{chunk.end_page}",
                report,
                company,
                registry,
                bundle,
                base_context,
                file_url=None,
                chunk=chunk,
            )
            elapsed = time.perf_counter() - started
            if progress:
                progress(
                    f"[chunk {index + 1}/{total}] 页 {chunk.start_page}-{chunk.end_page} 完成 ({elapsed:.1f}s)"
                )
            return result

        self.log.event(
            "chunk_parallel_started",
            report_id=report.report_id,
            chunks=total,
            concurrency=workers,
        )
        parallel_started = time.perf_counter()
        results: list[_CallResult | None] = [None] * total
        failures: dict[int, Exception] = {}
        attempts: dict[int, int] = {index: 1 for index in range(total)}
        max_attempts = 2  # 1 次正常 + 1 次重试
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = {pool.submit(work, index, chunk): index for index, chunk in enumerate(chunks)}
            while pending:
                done, _ = wait(pending.keys(), return_when=FIRST_COMPLETED)
                for future in done:
                    index = pending.pop(future)
                    try:
                        results[index] = future.result()
                    except Exception as exc:  # noqa: BLE001 - 汇总/重试后统一抛出
                        retryable = isinstance(exc, AppError) and exc.retryable
                        if retryable and attempts[index] < max_attempts:
                            attempts[index] += 1
                            chunk = chunks[index]
                            self.log.event(
                                "chunk_retry",
                                report_id=report.report_id,
                                chunk=index + 1,
                                pages=f"{chunk.start_page}-{chunk.end_page}",
                                attempt=attempts[index],
                                error=exc.code.value if isinstance(exc, AppError) else type(exc).__name__,
                            )
                            if progress:
                                progress(
                                    f"[chunk {index + 1}/{total}] 页 {chunk.start_page}-{chunk.end_page} "
                                    f"可重试失败，立即重试（第 {attempts[index]} 次）"
                                )
                            pending[pool.submit(work, index, chunk)] = index
                        else:
                            failures[index] = exc

        if failures:
            errors = []
            for index in sorted(failures):
                chunk = chunks[index]
                exc = failures[index]
                errors.append(
                    {
                        "chunk": index + 1,
                        "pages": f"{chunk.start_page}-{chunk.end_page}",
                        "code": exc.code.value if isinstance(exc, AppError) else "UNEXPECTED",
                        "error": str(exc)[:500],
                    }
                )
            done = [result.raw for result in results if result is not None]
            self._write_raw(report, {"chunked": True, "calls": done, "errors": errors})
            first_exc = failures[sorted(failures)[0]]
            if isinstance(first_exc, AppError):
                first_exc.details.setdefault("chunk_errors", errors)
                first_exc.details.setdefault("raw_path", str(self._raw_path(report)))
                raise first_exc
            raise AppError(
                ErrorCode.INTERNAL,
                f"分块提取失败: {first_exc}",
                retryable=False,
                details={"chunk_errors": errors},
            ) from first_exc

        self.log.event(
            "chunk_parallel_finished",
            report_id=report.report_id,
            elapsed_ms=int((time.perf_counter() - parallel_started) * 1000),
            concurrency=workers,
        )
        return [result for result in results if result is not None]

    # ------------------------------------------------------------------ 单次调用
    def _call_once(
        self,
        pdf_bytes: bytes,
        filename: str,
        report: ReportRef,
        company: CompanyProfile,
        registry: dict[str, MetricDefinition],
        bundle,
        base_context: dict,
        *,
        file_url: str | None,
        chunk: PdfChunk | None,
    ) -> _CallResult:
        chunk_info = None
        if chunk is not None:
            chunk_info = {
                "start_page": chunk.start_page,
                "end_page": chunk.end_page,
                "page_count": chunk.page_count,
                "total_pages": report.page_count,
            }
        rendered = self.prompts.render(
            "extraction",
            version=self.settings.prompt_version,
            skill_text=bundle.text,
            report_context=base_context,
            metric_ids=STAGE1_METRIC_IDS,
            schema_version=self.settings.schema_version,
            prompt_version=self.settings.prompt_version,
            chunk_info=chunk_info,
        )
        model_call_id = new_model_call_id()
        result = self.qwen.extract_pdf(pdf_bytes, filename, rendered.text, file_url=file_url)
        if result.finish_reason == "length":
            self.log.event(
                "extraction_truncated",
                report_id=report.report_id,
                usage=result.usage,
                max_tokens=self.settings.qwen_max_output_tokens,
                chunk_info=chunk_info,
            )
            raise AppError(
                ErrorCode.MODEL_BAD_JSON,
                "模型输出达到 token 上限被截断，候选集不完整；请提高 QWEN_MAX_OUTPUT_TOKENS 后重试",
                retryable=True,
                details={
                    "report_id": report.report_id,
                    "finish_reason": result.finish_reason,
                    "usage": result.usage,
                    "raw_path": str(self._raw_path(report)),
                },
            )
        raw = {
            "model": result.model,
            "request_id": result.request_id,
            "latency_ms": result.latency_ms,
            "usage": result.usage,
            "finish_reason": result.finish_reason,
            "attempts": result.attempts,
            "chunk": chunk_info,
            "text": result.text,
        }
        draft = self._parse_with_repair(result.text, report)
        page_limit = chunk.page_count if chunk is not None else report.page_count
        page_offset = chunk.page_offset if chunk is not None else 0
        evidence, candidates, coverage, warnings = self._to_canonical(
            draft, report, company, registry, bundle, model_call_id, page_limit, page_offset
        )
        return _CallResult(candidates=candidates, evidence=evidence, coverage=coverage, warnings=warnings, raw=raw)

    # ------------------------------------------------------------------ 解析与修复
    def _parse_with_repair(self, text: str, report: ReportRef) -> DraftExtractionBatch:
        try:
            payload = extract_json_object(text)
            return DraftExtractionBatch.model_validate(payload)
        except (ValueError, ValidationError) as exc:
            logger.warning("模型 JSON 首次解析失败，尝试一次修复: %s", exc)
            self.log.event("json_repair_attempt", report_id=report.report_id, error=str(exc)[:500])
            repaired = self.qwen.repair_json(text, str(exc)[:1500])
            try:
                payload = extract_json_object(repaired)
                return DraftExtractionBatch.model_validate(payload)
            except (ValueError, ValidationError) as exc2:
                self.log.event("json_repair_failed", report_id=report.report_id, error=str(exc2)[:500])
                raise AppError(
                    ErrorCode.MODEL_BAD_JSON,
                    "模型输出 JSON 无法解析，原始响应已保存，转人工处理",
                    retryable=False,
                    details={"report_id": report.report_id, "raw_path": str(self._raw_path(report))},
                ) from exc2

    # ------------------------------------------------------------------ 草稿 -> 规范对象
    def _to_canonical(
        self,
        draft: DraftExtractionBatch,
        report: ReportRef,
        company: CompanyProfile,
        registry: dict[str, MetricDefinition],
        bundle,
        model_call_id: str,
        page_limit: int,
        page_offset: int,
    ) -> tuple[list[Evidence], list[ExtractionCandidate], Coverage, list[str]]:
        warnings: list[str] = []
        evidence: list[Evidence] = []
        ev_id_map: dict[str, str] = {}

        for d in draft.evidence:
            page_local = d.source_page
            locator = "QWEN_PAGE_TEXT"
            if page_local < 1 or page_local > page_limit:
                warnings.append(f"证据 {d.evidence_id} 页码 {page_local} 超出本段 1..{page_limit}，标记 UNVERIFIED")
                locator = "UNVERIFIED"
            page_global = max(1, min(page_local, page_limit)) + page_offset
            ev = Evidence(
                evidence_id=new_evidence_id(),
                report_id=report.report_id,
                source_page=max(1, min(page_global, report.page_count)),
                printed_page=d.printed_page,
                source_type=d.source_type if d.source_type in VALID_SOURCE_TYPES else "TEXT",
                source_evidence=d.source_evidence,
                context_text=d.context_text,
                table_title=d.table_title,
                row_header=d.row_header,
                column_header=d.column_header,
                unit_header=d.unit_header,
                locator_method=locator,
            )
            evidence.append(ev)
            if d.evidence_id not in ev_id_map:
                ev_id_map[d.evidence_id] = ev.evidence_id

        candidates: list[ExtractionCandidate] = []
        for d in draft.candidates:
            flags = [f for f in d.ambiguity_flags if isinstance(f, str)]
            status = (d.classification_status or "").upper()
            if status not in VALID_STATUS:
                flags.append(f"INVALID_STATUS:{d.classification_status}")
                status = "NEEDS_REVIEW"

            if d.raw_value is None and d.raw_text is None:
                warnings.append(f"候选 {d.candidate_id} 既无 raw_value 也无 raw_text，已丢弃")
                continue
            if d.raw_value is not None and not str(d.raw_value).strip():
                d.raw_value = None

            metric_id = d.metric_id
            metric_version = d.metric_version
            if metric_id is not None:
                definition = registry.get(metric_id)
                if definition is None:
                    flags.append("UNKNOWN_METRIC")
                    warnings.append(f"候选 {d.candidate_id} 指标 {metric_id} 未登记，降级为 UNKNOWN")
                    metric_id, metric_version = None, None
                    status = "UNKNOWN"
                else:
                    metric_version = definition.version
            if status == "CLASSIFIED" and not metric_id:
                flags.append("CLASSIFIED_WITHOUT_METRIC")
                status = "UNKNOWN"
            if status == "CLASSIFIED" and not metric_version:
                metric_version = registry[metric_id].version if metric_id in registry else None

            mapped_evidence = [ev_id_map[e] for e in d.evidence_ids if e in ev_id_map]
            if not mapped_evidence:
                warnings.append(f"候选 {d.candidate_id} 未引用到有效证据，已丢弃")
                continue
            dropped = [e for e in d.evidence_ids if e not in ev_id_map]
            if dropped:
                flags.append("EVIDENCE_REF_MISSING")
                warnings.append(f"候选 {d.candidate_id} 引用不存在的证据 {dropped}，已忽略这些引用")

            bad_pages = [ev for ev in evidence if ev.evidence_id in mapped_evidence and ev.locator_method == "UNVERIFIED"]
            if bad_pages and status != "UNKNOWN":
                status = "NEEDS_REVIEW"
                flags.append("PAGE_OUT_OF_RANGE")

            reason = (d.classification_reason or "").strip()
            if not reason:
                reason = "模型未给出归类理由，需人工复核"
                flags.append("MISSING_REASON")
                if status == "CLASSIFIED":
                    status = "NEEDS_REVIEW"

            confidence = max(0.0, min(1.0, float(d.confidence or 0.0)))
            boundary = d.reporting_boundary if isinstance(d.reporting_boundary, dict) else {}
            if not boundary:
                boundary = {"level": "UNKNOWN", "description": "模型未给出统计边界"}

            try:
                candidate = ExtractionCandidate(
                    candidate_id=new_candidate_id(),
                    report_id=report.report_id,
                    company_id=company.company_id,
                    report_year=report.report_year,
                    data_year=d.data_year,
                    raw_value=str(d.raw_value) if d.raw_value is not None else None,
                    raw_text=d.raw_text,
                    unit=d.unit,
                    statistical_period=d.statistical_period,
                    reporting_boundary=boundary,
                    evidence_ids=mapped_evidence,
                    ambiguity_flags=flags,
                    metric_id=metric_id,
                    metric_version=metric_version,
                    classification_status=status,
                    classification_reason=reason,
                    alternatives=[a for a in d.alternatives if isinstance(a, str)],
                    confidence=confidence,
                    skill_versions=dict(bundle.metric_versions),
                    model_call_id=model_call_id,
                )
            except ValidationError as exc:
                warnings.append(f"候选 {d.candidate_id} 结构校验失败已丢弃: {exc.errors()[:2]}")
                continue
            candidates.append(candidate)

        coverage = Coverage()
        if draft.coverage is not None and draft.coverage.pages_scanned:
            coverage = Coverage(
                pages_scanned=sorted({p + page_offset for p in draft.coverage.pages_scanned if 1 <= p <= page_limit}),
                notes=draft.coverage.notes,
                suspected_missing=draft.coverage.suspected_missing,
            )
        else:
            warnings.append("模型未返回覆盖页码，无法确认是否逐页通读")
            coverage = Coverage(pages_scanned=list(range(1 + page_offset, page_offset + page_limit + 1)))
        return evidence, candidates, coverage, warnings

    # ------------------------------------------------------------------ 持久化
    def _batch_path(self, report: ReportRef) -> Path:
        return Path(self.settings.batches_dir) / f"{report.report_id}.json"

    def _raw_path(self, report: ReportRef) -> Path:
        return Path(self.settings.batches_dir) / f"{report.report_id}.raw.json"

    def _persist(self, report: ReportRef, batch: ExtractionBatch) -> None:
        path = self._batch_path(report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(batch.model_dump_json(indent=2), encoding="utf-8")

    def _write_raw(self, report: ReportRef, payload: dict[str, Any]) -> None:
        path = self._raw_path(report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _load_cached(self, report: ReportRef) -> ExtractionBatch | None:
        path = self._batch_path(report)
        if not path.exists():
            return None
        try:
            batch = ExtractionBatch.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, ValueError):
            logger.warning("缓存批次损坏，忽略: %s", path)
            return None
        if batch.report_id != report.report_id:
            return None
        return batch


# ---------------------------------------------------------------------- 合并工具
def _deduplicate(
    evidence: list[Evidence], candidates: list[ExtractionCandidate]
) -> tuple[list[Evidence], list[ExtractionCandidate]]:
    """跨块去重：证据按 (页, 原文) 去重；候选按 (指标, 原值, 单位, 年份, 证据页) 去重。"""
    ev_seen: dict[tuple, Evidence] = {}
    for ev in evidence:
        key = (ev.source_page, ev.source_evidence.strip())
        ev_seen.setdefault(key, ev)
    kept_evidence = list(ev_seen.values())
    ev_by_id = {e.evidence_id: e for e in kept_evidence}

    ca_seen: dict[tuple, ExtractionCandidate] = {}
    for c in candidates:
        pages = tuple(sorted({ev_by_id[eid].source_page for eid in c.evidence_ids if eid in ev_by_id}))
        key = (c.metric_id, c.raw_value, c.unit, c.data_year, pages)
        if key in ca_seen:
            if c.confidence > ca_seen[key].confidence:
                ca_seen[key] = c
            continue
        ca_seen[key] = c
    return kept_evidence, list(ca_seen.values())


def _merge_coverage(
    calls: list[_CallResult], total_pages: int, *, chunked: bool, chunk_pages: int
) -> Coverage:
    pages: set[int] = set()
    missing: list[str] = []
    notes: list[str] = []
    for call in calls:
        pages.update(call.coverage.pages_scanned)
        missing.extend(call.coverage.suspected_missing)
        if call.coverage.notes:
            notes.append(call.coverage.notes)
    if chunked:
        notes.insert(0, f"分块提取（平台单次 PDF 解析页数上限，每块约 {chunk_pages} 页）")
    if not pages:
        pages = set(range(1, total_pages + 1))
        notes.append("模型未返回覆盖页码，由系统补全")
    return Coverage(pages_scanned=sorted(pages), notes="；".join(notes) or None, suspected_missing=missing)


def _sum_usage(calls: list[_CallResult]) -> dict[str, int]:
    prompt = completion = 0
    for call in calls:
        usage = call.raw.get("usage") or {}
        prompt += int(usage.get("prompt_tokens") or 0)
        completion += int(usage.get("completion_tokens") or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def stats(batch: ExtractionBatch) -> dict[str, Any]:
    """批次统计，用于 CLI 输出。"""
    by_metric: dict[str, int] = {}
    for c in batch.candidates:
        key = c.metric_id or "UNKNOWN"
        by_metric[key] = by_metric.get(key, 0) + 1
    status: dict[str, int] = {}
    for c in batch.candidates:
        status[c.classification_status] = status.get(c.classification_status, 0) + 1
    return {
        "candidates": len(batch.candidates),
        "evidence": len(batch.evidence),
        "by_metric": by_metric,
        "status": status,
        "pages_scanned": len(batch.coverage.pages_scanned) if batch.coverage else 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
