"""分块服务测试：页面范围规划、页码偏移、切片内容完整。"""

from __future__ import annotations

import pymupdf
import pytest

from app.services.chunker import plan_page_ranges, split_pdf


def _make_pdf(pages: int) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((60, 80), f"page {i + 1}")
    data = doc.tobytes()
    doc.close()
    return data


def test_plan_page_ranges_exact_and_remainder():
    assert plan_page_ranges(189, 63) == [(1, 63), (64, 126), (127, 189)]
    assert plan_page_ranges(48, 63) == [(1, 48)]
    assert plan_page_ranges(5, 2) == [(1, 2), (3, 4), (5, 5)]
    assert plan_page_ranges(63, 63) == [(1, 63)]


def test_split_pdf_offsets_and_page_counts():
    data = _make_pdf(5)
    chunks = split_pdf(data, 2)
    assert [(c.start_page, c.end_page, c.page_count) for c in chunks] == [
        (1, 2, 2),
        (3, 4, 2),
        (5, 5, 1),
    ]
    assert [c.page_offset for c in chunks] == [0, 2, 4]
    for chunk in chunks:
        doc = pymupdf.open(stream=chunk.pdf_bytes, filetype="pdf")
        try:
            assert doc.page_count == chunk.page_count
        finally:
            doc.close()


def test_split_pdf_preserves_text():
    data = _make_pdf(3)
    chunks = split_pdf(data, 2)
    second = pymupdf.open(stream=chunks[1].pdf_bytes, filetype="pdf")
    try:
        assert "page 3" in second[0].get_text()
    finally:
        second.close()


def test_byte_limit_splits_small_page_count_and_preserves_every_page():
    data = _make_pdf(8)
    limit = max(len(c.pdf_bytes) for c in split_pdf(data,1)) + 100
    chunks = split_pdf(data,63,max_bytes=limit)
    assert len(chunks)>1
    assert all(len(c.pdf_bytes)<=limit for c in chunks)
    assert [p for c in chunks for p in range(c.start_page,c.end_page+1)]==list(range(1,9))
    assert [c.index for c in chunks]==list(range(len(chunks)))
    for c in chunks:
        with pymupdf.open(stream=c.pdf_bytes,filetype='pdf') as doc:
            for index,page in enumerate(doc):
                assert f'page {c.start_page+index}' in page.get_text()


def test_single_oversized_page_fails_before_network():
    from app.core.errors import AppError,ErrorCode
    with pytest.raises(AppError) as exc:
        split_pdf(_make_pdf(1),63,max_bytes=100)
    assert exc.value.code==ErrorCode.PDF_LIMIT_EXCEEDED
