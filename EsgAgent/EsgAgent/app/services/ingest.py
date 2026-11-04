"""PDF 入库服务：校验、哈希、不可变存储与 `ReportRef` 持久化。

契约见 `docs/01_Qwen文档理解与提取归类Agent.md` 与 `docs/07_工程目录与开发接口.md`：

- 后端只做文件类型、大小、页数元数据与 SHA-256 检查，**不提取文本、不渲染、不做 OCR**；
- 页数通过 PyMuPDF 轻量读取 `page_count`，仅用于限制校验；
- 原件存对象存储（key = `pdf/<sha256>.pdf`），不可变；
- `ReportRef` 写 `data/reports/<report_id>.json`，是提取/审核的稳定输入；
- 幂等：同 `company_id + report_year + revision + sha256` 直接返回已有版本；
  同 `company_id + report_year + revision` 但内容不同抛 `CONFLICT`（修正须递增 revision）。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import datetime
from pathlib import Path

import pymupdf
from pydantic import ValidationError

from app.adapters.object_store import LocalObjectStore
from app.core.errors import AppError, ErrorCode
from app.core.ids import new_report_id
from app.models.contracts import CompanyProfile, ReportRef

_PDF_MAGIC = b"%PDF-"


class IngestService:
    """PDF 原件入库（Phase 2 入口）。"""

    def __init__(
        self,
        settings,
        store: LocalObjectStore,
        reports_dir: Path | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.reports_dir = Path(reports_dir) if reports_dir is not None else Path(settings.reports_dir)
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 入库
    def ingest_pdf(
        self,
        path: Path,
        company: CompanyProfile,
        report_year: int,
        *,
        revision: int = 1,
        published_at: datetime | None = None,
    ) -> ReportRef:
        """校验并保存 PDF 原件，返回不可变的 `ReportRef`。

        Raises:
            AppError: `NOT_FOUND`（文件不存在）、`PDF_INVALID`（非 PDF/空/无法解析）、
                `PDF_LIMIT_EXCEEDED`（超过 150MB 或 256 页）、`CONFLICT`（同版本不同内容）。
        """
        path = Path(path)
        if not path.is_file():
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"PDF 文件不存在: {path}",
                retryable=False,
                details={"path": str(path)},
            )

        size_bytes = path.stat().st_size
        if size_bytes <= 0:
            raise AppError(
                ErrorCode.PDF_INVALID,
                "PDF 文件为空",
                retryable=False,
                details={"path": str(path), "size_bytes": size_bytes},
            )
        max_bytes = int(self.settings.max_pdf_bytes)
        if size_bytes > max_bytes:
            raise AppError(
                ErrorCode.PDF_LIMIT_EXCEEDED,
                f"PDF 大小 {size_bytes} 字节超过上限 {max_bytes} 字节（约 {max_bytes // (1024 * 1024)}MB）",
                retryable=False,
                details={"size_bytes": size_bytes, "max_pdf_bytes": max_bytes},
            )

        try:
            data = path.read_bytes()
        except OSError as exc:
            raise AppError(
                ErrorCode.PDF_INVALID,
                f"PDF 读取失败: {path}",
                retryable=False,
                details={"path": str(path), "error": str(exc)},
            ) from exc

        if not data.startswith(_PDF_MAGIC):
            raise AppError(
                ErrorCode.PDF_INVALID,
                "文件不是有效 PDF（缺少 %PDF- 文件头）",
                retryable=False,
                details={"path": str(path), "head": data[:5].hex()},
            )

        sha256 = hashlib.sha256(data).hexdigest()
        existing = self._find_existing(company.company_id, report_year, revision)
        if existing is not None:
            if existing.sha256 == sha256:
                # 幂等：相同公司/年份/修订/内容，直接复用已有 ReportRef。
                return existing
            raise AppError(
                ErrorCode.CONFLICT,
                (
                    f"公司 {company.company_id} 的 {report_year} 年报告 revision={revision} "
                    f"已存在且内容不同（existing_sha256={existing.sha256[:12]}…，"
                    f"new_sha256={sha256[:12]}…）；修正请递增 revision"
                ),
                retryable=False,
                details={
                    "company_id": company.company_id,
                    "report_year": report_year,
                    "revision": revision,
                    "existing_report_id": existing.report_id,
                    "existing_sha256": existing.sha256,
                    "sha256": sha256,
                },
            )

        page_count = self._read_page_count(data, path=path)
        max_pages = int(self.settings.max_pdf_pages)
        if page_count > max_pages:
            raise AppError(
                ErrorCode.PDF_LIMIT_EXCEEDED,
                f"PDF 页数 {page_count} 超过上限 {max_pages} 页",
                retryable=False,
                details={"page_count": page_count, "max_pdf_pages": max_pages},
            )

        object_uri = self.store.put_bytes(
            f"pdf/{sha256}.pdf", data, content_type="application/pdf"
        )
        report = ReportRef(
            report_id=new_report_id(),
            company_id=company.company_id,
            report_year=report_year,
            revision=revision,
            sha256=sha256,
            object_uri=object_uri,
            page_count=page_count,
            published_at=published_at,
            source_filename=path.name,
            size_bytes=len(data),
        )
        self._write_report(report)
        return report

    # ------------------------------------------------------------------ 读取
    def load_report(self, report_id: str) -> ReportRef:
        """按 `report_id` 读取 `ReportRef` JSON。"""
        if not isinstance(report_id, str) or not report_id.strip():
            raise AppError(ErrorCode.NOT_FOUND, "report_id 不得为空", retryable=False)
        if "/" in report_id or "\\" in report_id or ".." in report_id:
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"非法 report_id: {report_id!r}",
                retryable=False,
            )
        path = self.reports_dir / f"{report_id}.json"
        if not path.is_file():
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"报告不存在: {report_id}",
                retryable=False,
                details={"report_id": report_id},
            )
        try:
            return ReportRef.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, OSError, ValueError) as exc:
            raise AppError(
                ErrorCode.INTERNAL,
                f"ReportRef 文件损坏: {report_id}",
                retryable=False,
                details={"report_id": report_id, "error": str(exc)},
            ) from exc

    def read_pdf(self, report: ReportRef) -> bytes:
        """从对象存储读取报告原件字节。"""
        return self.store.get_bytes(report.object_uri)

    # ------------------------------------------------------------------ 内部工具
    def _read_page_count(self, data: bytes, *, path: Path) -> int:
        """仅读取 page_count 元数据；不提取文本、不渲染页面。"""
        try:
            doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception as exc:  # noqa: BLE001 - PyMuPDF 抛出多种解析异常
            raise AppError(
                ErrorCode.PDF_INVALID,
                "PDF 无法解析（文件损坏或加密）",
                retryable=False,
                details={"path": str(path), "error_type": type(exc).__name__},
            ) from exc
        try:
            page_count = int(doc.page_count)
        finally:
            doc.close()
        if page_count < 1:
            raise AppError(
                ErrorCode.PDF_INVALID,
                "PDF 页数为 0，不是有效报告",
                retryable=False,
                details={"path": str(path), "page_count": page_count},
            )
        return page_count

    def _find_existing(self, company_id: str, report_year: int, revision: int) -> ReportRef | None:
        """扫描 reports_dir，找同 company_id + report_year + revision 的报告版本。"""
        for path in sorted(self.reports_dir.glob("*.json")):
            try:
                report = ReportRef.model_validate_json(path.read_text(encoding="utf-8"))
            except (ValidationError, OSError, ValueError):
                # 损坏或不完整的 ReportRef 不参与幂等判定，避免阻塞新入库。
                continue
            if (
                report.company_id == company_id
                and report.report_year == report_year
                and report.revision == revision
            ):
                return report
        return None

    def _write_report(self, report: ReportRef) -> None:
        """原子写 `data/reports/<report_id>.json`。"""
        path = self.reports_dir / f"{report.report_id}.json"
        payload = report.model_dump_json(indent=2)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.reports_dir), prefix=".tmp-", suffix=".json.part")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        finally:
            tmp_path = Path(tmp_name)
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
