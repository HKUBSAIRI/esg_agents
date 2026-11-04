"""PDF 入库与本地对象存储单测（临时生成 PDF，不访问网络）。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
import pytest

from app.adapters.object_store import LocalObjectStore
from app.core.errors import AppError, ErrorCode
from app.core.settings import Settings
from app.models.contracts import CompanyProfile, ReportRef
from app.services.ingest import IngestService

API_KEY = "sk-unit-test-fake-key-0123456789"


def make_settings(**overrides) -> Settings:
    values = {
        "dashscope_api_key": API_KEY,
        "app_env": "test",
        "max_pdf_bytes": 150 * 1024 * 1024,
        "max_pdf_pages": 256,
    }
    values.update(overrides)
    return Settings(**values)


def make_pdf(path: Path, *, pages: int = 2, marker: str = "ESG Report") -> bytes:
    """用 PyMuPDF 生成一个最小可解析 PDF 并写入 path。"""
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"{marker} page {index + 1}")
    data = doc.tobytes()
    doc.close()
    path.write_bytes(data)
    return data


@pytest.fixture
def company() -> CompanyProfile:
    return CompanyProfile(
        company_id="co_test_0001",
        hkex_code="0001",
        name="测试食品有限公司",
        name_en="Test Food Limited",
        industry_code="fb-nb",
        sasb_sector="Food & Beverage",
        sasb_industry="Food",
    )


@pytest.fixture
def store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "objects")


@pytest.fixture
def service(tmp_path: Path, store: LocalObjectStore) -> IngestService:
    return IngestService(make_settings(), store, reports_dir=tmp_path / "reports")


def make_service(
    tmp_path: Path,
    store: LocalObjectStore,
    **settings_overrides,
) -> IngestService:
    return IngestService(
        make_settings(**settings_overrides),
        store,
        reports_dir=tmp_path / "reports",
    )


# --------------------------------------------------------------------------- 成功入库
def test_ingest_pdf_success_persists_report_and_object(
    tmp_path: Path, service: IngestService, store: LocalObjectStore, company: CompanyProfile
) -> None:
    pdf_path = tmp_path / "report.pdf"
    data = make_pdf(pdf_path)
    sha256 = hashlib.sha256(data).hexdigest()

    report = service.ingest_pdf(pdf_path, company, 2025)

    assert isinstance(report, ReportRef)
    assert report.report_id.startswith("rp_")
    assert report.company_id == company.company_id
    assert report.report_year == 2025
    assert report.revision == 1
    assert report.sha256 == sha256
    assert report.object_uri == f"local://pdf/{sha256}.pdf"
    assert report.page_count == 2
    assert report.source_filename == "report.pdf"
    assert report.size_bytes == len(data)
    assert report.published_at is None
    assert report.created_at is not None

    # 对象已不可变保存
    assert store.exists(report.object_uri) is True
    assert store.get_bytes(report.object_uri) == data
    assert store.public_url(report.object_uri) is None

    # ReportRef JSON 已落盘，且能回读
    ref_path = tmp_path / "reports" / f"{report.report_id}.json"
    assert ref_path.is_file()
    payload = json.loads(ref_path.read_text(encoding="utf-8"))
    assert payload["sha256"] == sha256
    assert payload["page_count"] == 2
    loaded = service.load_report(report.report_id)
    assert loaded == report

    # read_pdf 返回原件字节
    assert service.read_pdf(report) == data


def test_ingest_pdf_persists_published_at(
    tmp_path: Path, service: IngestService, company: CompanyProfile
) -> None:
    pdf_path = tmp_path / "report.pdf"
    make_pdf(pdf_path, pages=1)
    published = datetime(2025, 3, 28, tzinfo=timezone.utc)

    report = service.ingest_pdf(pdf_path, company, 2025, published_at=published)

    assert report.published_at == published
    assert service.load_report(report.report_id).published_at == published


# --------------------------------------------------------------------------- 幂等 / 冲突
def test_ingest_pdf_is_idempotent_for_same_content(
    tmp_path: Path, service: IngestService, store: LocalObjectStore, company: CompanyProfile
) -> None:
    pdf_path = tmp_path / "report.pdf"
    make_pdf(pdf_path)

    first = service.ingest_pdf(pdf_path, company, 2025)
    second = service.ingest_pdf(pdf_path, company, 2025)

    assert second.report_id == first.report_id
    assert second == first
    assert len(list((tmp_path / "reports").glob("*.json"))) == 1
    assert len(list((tmp_path / "objects").rglob("*.pdf"))) == 1


def test_ingest_pdf_conflicts_when_same_version_has_different_content(
    tmp_path: Path, service: IngestService, company: CompanyProfile
) -> None:
    pdf_path = tmp_path / "report.pdf"
    make_pdf(pdf_path, marker="original report")
    first = service.ingest_pdf(pdf_path, company, 2025)

    make_pdf(pdf_path, marker="changed report")
    with pytest.raises(AppError) as excinfo:
        service.ingest_pdf(pdf_path, company, 2025)

    error = excinfo.value
    assert error.code == ErrorCode.CONFLICT
    assert error.retryable is False
    assert error.details["existing_report_id"] == first.report_id
    assert error.details["revision"] == 1


def test_ingest_pdf_allows_new_revision_with_different_content(
    tmp_path: Path, service: IngestService, company: CompanyProfile
) -> None:
    pdf_path = tmp_path / "report.pdf"
    make_pdf(pdf_path, marker="revision one")
    first = service.ingest_pdf(pdf_path, company, 2025, revision=1)

    make_pdf(pdf_path, marker="revision two")
    second = service.ingest_pdf(pdf_path, company, 2025, revision=2)

    assert second.report_id != first.report_id
    assert second.revision == 2
    assert second.sha256 != first.sha256
    assert len(list((tmp_path / "reports").glob("*.json"))) == 2


def test_ingest_pdf_allows_same_content_under_other_year(
    tmp_path: Path, service: IngestService, company: CompanyProfile
) -> None:
    pdf_path = tmp_path / "report.pdf"
    make_pdf(pdf_path)
    first = service.ingest_pdf(pdf_path, company, 2024)
    second = service.ingest_pdf(pdf_path, company, 2025)
    assert second.report_id != first.report_id
    assert second.object_uri == first.object_uri  # 相同 sha256 复用同一个对象


# --------------------------------------------------------------------------- 校验失败
def test_ingest_pdf_missing_file_raises_not_found(
    tmp_path: Path, service: IngestService, company: CompanyProfile
) -> None:
    with pytest.raises(AppError) as excinfo:
        service.ingest_pdf(tmp_path / "missing.pdf", company, 2025)
    assert excinfo.value.code == ErrorCode.NOT_FOUND


def test_ingest_pdf_empty_file_raises_pdf_invalid(
    tmp_path: Path, service: IngestService, company: CompanyProfile
) -> None:
    pdf_path = tmp_path / "empty.pdf"
    pdf_path.write_bytes(b"")
    with pytest.raises(AppError) as excinfo:
        service.ingest_pdf(pdf_path, company, 2025)
    assert excinfo.value.code == ErrorCode.PDF_INVALID


def test_ingest_pdf_non_pdf_raises_pdf_invalid(
    tmp_path: Path, service: IngestService, company: CompanyProfile
) -> None:
    fake_path = tmp_path / "not_a_pdf.pdf"
    fake_path.write_bytes(b"this is plain text, not a pdf")
    with pytest.raises(AppError) as excinfo:
        service.ingest_pdf(fake_path, company, 2025)
    assert excinfo.value.code == ErrorCode.PDF_INVALID


def test_ingest_pdf_corrupt_pdf_raises_pdf_invalid(
    tmp_path: Path, service: IngestService, company: CompanyProfile
) -> None:
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"%PDF-1.7\nthis body is not a real pdf structure")
    with pytest.raises(AppError) as excinfo:
        service.ingest_pdf(corrupt, company, 2025)
    assert excinfo.value.code == ErrorCode.PDF_INVALID


def test_ingest_pdf_exceeding_size_limit_raises_pdf_limit(
    tmp_path: Path, store: LocalObjectStore, company: CompanyProfile
) -> None:
    pdf_path = tmp_path / "report.pdf"
    data = make_pdf(pdf_path)
    service = make_service(tmp_path, store, max_pdf_bytes=len(data) - 1)

    with pytest.raises(AppError) as excinfo:
        service.ingest_pdf(pdf_path, company, 2025)

    assert excinfo.value.code == ErrorCode.PDF_LIMIT_EXCEEDED
    assert excinfo.value.details["size_bytes"] == len(data)
    assert list((tmp_path / "reports").glob("*.json")) == []


def test_ingest_pdf_exceeding_page_limit_raises_pdf_limit(
    tmp_path: Path, store: LocalObjectStore, company: CompanyProfile
) -> None:
    pdf_path = tmp_path / "report.pdf"
    make_pdf(pdf_path, pages=3)
    service = make_service(tmp_path, store, max_pdf_pages=2)

    with pytest.raises(AppError) as excinfo:
        service.ingest_pdf(pdf_path, company, 2025)

    assert excinfo.value.code == ErrorCode.PDF_LIMIT_EXCEEDED
    assert excinfo.value.details["page_count"] == 3
    assert list((tmp_path / "reports").glob("*.json")) == []


def test_ingest_pdf_does_not_store_object_when_rejected(
    tmp_path: Path, service: IngestService, store: LocalObjectStore, company: CompanyProfile
) -> None:
    fake_path = tmp_path / "not_a_pdf.pdf"
    fake_path.write_bytes(b"not a pdf")
    with pytest.raises(AppError):
        service.ingest_pdf(fake_path, company, 2025)
    assert list((tmp_path / "objects").rglob("*")) == []


# --------------------------------------------------------------------------- 读取错误
def test_load_report_not_found(service: IngestService) -> None:
    with pytest.raises(AppError) as excinfo:
        service.load_report("rp_missing")
    assert excinfo.value.code == ErrorCode.NOT_FOUND


@pytest.mark.parametrize("bad_id", ["../escape", "a/b", "", "."])
def test_load_report_rejects_unsafe_ids(service: IngestService, bad_id: str) -> None:
    with pytest.raises(AppError) as excinfo:
        service.load_report(bad_id)
    assert excinfo.value.code == ErrorCode.NOT_FOUND


# --------------------------------------------------------------------------- 对象存储
def test_local_object_store_put_get_exists(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    uri = store.put_bytes("pdf/abc.pdf", b"%PDF-1.7 data")

    assert uri == "local://pdf/abc.pdf"
    assert store.exists(uri) is True
    assert store.get_bytes(uri) == b"%PDF-1.7 data"
    assert store.public_url(uri) is None
    assert store.exists("local://pdf/missing.pdf") is False


def test_local_object_store_is_immutable(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    store.put_bytes("pdf/abc.pdf", b"same")

    # 相同内容可重复写入（复用）
    assert store.put_bytes("pdf/abc.pdf", b"same") == "local://pdf/abc.pdf"

    # 不同内容必须冲突，不能覆盖
    with pytest.raises(AppError) as excinfo:
        store.put_bytes("pdf/abc.pdf", b"different")
    assert excinfo.value.code == ErrorCode.CONFLICT
    assert store.get_bytes("local://pdf/abc.pdf") == b"same"


def test_local_object_store_rejects_path_traversal(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    with pytest.raises(AppError) as excinfo:
        store.put_bytes("../escape.pdf", b"data")
    assert excinfo.value.code == ErrorCode.INTERNAL


def test_local_object_store_get_missing_raises_not_found(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    with pytest.raises(AppError) as excinfo:
        store.get_bytes("local://pdf/none.pdf")
    assert excinfo.value.code == ErrorCode.NOT_FOUND


def test_local_object_store_rejects_unknown_uri_scheme(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    with pytest.raises(AppError) as excinfo:
        store.get_bytes("s3://bucket/key.pdf")
    assert excinfo.value.code == ErrorCode.INTERNAL
    assert store.exists("s3://bucket/key.pdf") is False
