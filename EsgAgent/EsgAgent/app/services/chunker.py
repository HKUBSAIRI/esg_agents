"""大 PDF 分块服务。

背景：qwen3.8-flash 的 PDF 解析在单次请求上有实际页数/耗时上限——实测 70 页可解析，
90 页起在约 121s 后返回 `InternalError.Algo.InvalidParameter: The file format is illegal
and cannot be opened`。为处理 189 页的完整报告，按页切块、每块单独调用模型，再把
页码回填为原报告物理页码。

约束：
- 仅在单份 PDF 超过阈值时启用；小块仍是“完整文件”，不做文本解析/OCR；
- 切块只复制页面对象，不改变内容；页码偏移由 Python 统一回填；
- 分块信息写入 `coverage.notes`，批次可通过 `--force` 重跑。
"""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf

# DashScope enforces 20 MiB per data URI, including the Base64 prefix.
MAX_INLINE_PDF_BYTES = ((20 * 1024 * 1024 - 64) // 4) * 3


@dataclass(frozen=True)
class PdfChunk:
    index: int
    start_page: int  # 原报告物理页码，1 起始，闭区间
    end_page: int
    page_count: int
    pdf_bytes: bytes

    @property
    def page_offset(self) -> int:
        """块内物理页 + offset = 原报告物理页。"""
        return self.start_page - 1


def plan_page_ranges(page_count: int, chunk_pages: int) -> list[tuple[int, int]]:
    """返回 [(start_page, end_page), ...]，1 起始闭区间。"""
    if chunk_pages < 1:
        raise ValueError("chunk_pages 必须 >= 1")
    ranges: list[tuple[int, int]] = []
    start = 1
    while start <= page_count:
        end = min(start + chunk_pages - 1, page_count)
        ranges.append((start, end))
        start = end + 1
    return ranges


def split_pdf(pdf_bytes: bytes, chunk_pages: int, *, max_bytes: int = MAX_INLINE_PDF_BYTES) -> list[PdfChunk]:
    """按页切分 PDF（仅复制页面对象，不改内容），返回带原页码信息的块。"""
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    source = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        page_count = source.page_count
        chunks: list[PdfChunk] = []
        def append_range(start, end):
            part = pymupdf.open()
            try:
                # links/annots=False：个别报告的链接/注释指向越界对象会导致 insert_pdf 报
                # "source object number out of range"；链接与注释对提取无意义，可安全丢弃。
                part.insert_pdf(source, from_page=start - 1, to_page=end - 1, links=False, annots=False)
                data = part.tobytes(garbage=3, deflate=True)
            finally:
                part.close()
            if len(data) > max_bytes:
                if start == end:
                    from app.core.errors import AppError, ErrorCode
                    raise AppError(ErrorCode.PDF_LIMIT_EXCEEDED,
                        "单页PDF仍超过上传限制，请使用对象存储签名URL", retryable=False)
                middle=(start+end)//2
                append_range(start,middle)
                append_range(middle+1,end)
                return
            chunks.append(
                PdfChunk(
                    index=len(chunks),
                    start_page=start,
                    end_page=end,
                    page_count=end - start + 1,
                    pdf_bytes=data,
                )
            )
        for start, end in plan_page_ranges(page_count, chunk_pages):
            append_range(start,end)
        return chunks
    finally:
        source.close()
