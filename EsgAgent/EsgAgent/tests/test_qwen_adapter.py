"""Qwen PDF 适配器单测：使用 fake client，绝不访问网络。"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from app.adapters.qwen import ModelResult, QwenPDFClient
from app.core.errors import AppError, ErrorCode
from app.core.logging import RunLogger
from app.core.settings import Settings
from tests.fakes.qwen_fake import (
    FakeCompletion,
    FakeOpenAI,
    bad_request_error,
    connection_error,
    make_stream,
    make_usage,
    rate_limit_error,
    server_error,
    timeout_error,
)

PDF_BYTES = b"%PDF-1.7\n% fake pdf bytes for adapter tests\n"
PDF_SHA256 = hashlib.sha256(PDF_BYTES).hexdigest()
PROMPT = "逐页提取水资源披露，只输出 ExtractionBatch v1 JSON。"
API_KEY = "sk-unit-test-fake-key-0123456789"


def make_settings(**overrides) -> Settings:
    values = {
        "dashscope_api_key": API_KEY,
        "qwen_model": "qwen3.8-flash",
        "qwen_max_output_tokens": 32768,
        "qwen_temperature": 0.0,
        "model_max_retries": 2,
        "model_retry_base_s": 0.0,
        "qwen_timeout_s": 600.0,
    }
    values.update(overrides)
    return Settings(**values)


def read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def make_client(script: list, settings: Settings | None = None, log_dir: Path | None = None):
    settings = settings or make_settings()
    run_logger = RunLogger(log_dir, "run_adapter_test") if log_dir is not None else None
    fake = FakeOpenAI(script)
    return QwenPDFClient(settings, run_logger=run_logger, client=fake), fake


# --------------------------------------------------------------------------- 正常路径
def test_extract_pdf_success_returns_model_result_and_logs(tmp_path: Path) -> None:
    usage = make_usage(3000, 500, 3500)
    client, fake = make_client(
        [make_stream('{"pages": []}', request_id="req_test_123", usage=usage)],
        log_dir=tmp_path / "logs",
    )

    result = client.extract_pdf(PDF_BYTES, "report.pdf", PROMPT)

    assert isinstance(result, ModelResult)
    assert result.text == '{"pages": []}'
    assert result.model == "qwen3.8-flash"
    assert result.request_id == "req_test_123"
    assert result.latency_ms >= 0
    assert result.usage == {"prompt_tokens": 3000, "completion_tokens": 500, "total_tokens": 3500}
    assert result.finish_reason == "stop"
    assert result.attempts == 1
    assert len(fake.calls) == 1

    records = read_log(tmp_path / "logs" / "run_adapter_test.jsonl")
    assert len(records) == 1
    record = records[0]
    assert record["event"] == "model_call"
    assert record["model"] == "qwen3.8-flash"
    assert record["request_id"] == "req_test_123"
    assert record["usage"] == {"prompt_tokens": 3000, "completion_tokens": 500, "total_tokens": 3500}
    assert record["finish_reason"] == "stop"
    assert record["status"] == "SUCCESS"
    assert record["error_code"] is None
    assert record["attempts"] == 1
    assert record["pdf_bytes"] == len(PDF_BYTES)
    assert record["pdf_sha256"] == PDF_SHA256
    assert record["prompt_chars"] == len(PROMPT)

    # 日志不得包含 API Key / 完整 base64 / prompt 全文。
    raw = (tmp_path / "logs" / "run_adapter_test.jsonl").read_text(encoding="utf-8")
    assert API_KEY not in raw
    assert base64.b64encode(PDF_BYTES).decode() not in raw
    assert PROMPT not in raw


def test_extract_pdf_defaults_and_overrides_reach_sdk(tmp_path: Path) -> None:
    settings = make_settings()
    client, fake = make_client([make_stream('{"ok": 1}')], settings=settings)

    client.extract_pdf(PDF_BYTES, "report.pdf", "p1")
    kwargs = fake.calls[0]
    assert kwargs["stream"] is True
    assert kwargs["stream_options"] == {"include_usage": True}
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["model"] == "qwen3.8-flash"
    assert kwargs["max_tokens"] == 32768
    assert kwargs["temperature"] == 0.0
    assert kwargs["messages"][0]["role"] == "user"

    client.extract_pdf(PDF_BYTES, "report.pdf", "p2", max_tokens=1234, temperature=0.7)
    kwargs = fake.calls[1]
    assert kwargs["max_tokens"] == 1234
    assert kwargs["temperature"] == 0.7


# --------------------------------------------------------------------------- content 结构
def test_extract_pdf_base64_file_data_structure(tmp_path: Path) -> None:
    client, fake = make_client([make_stream('{"ok": 1}')])
    client.extract_pdf(PDF_BYTES, "report.pdf", PROMPT)

    content = fake.calls[0]["messages"][0]["content"]
    assert isinstance(content, list) and len(content) == 2
    file_part, text_part = content
    assert file_part == {
        "type": "file",
        "file": {
            "file_data": "data:application/pdf;base64," + base64.b64encode(PDF_BYTES).decode("ascii"),
            "filename": "report.pdf",
        },
    }
    assert text_part == {"type": "text", "text": PROMPT}


def test_extract_pdf_prefers_file_url_when_provided() -> None:
    client, fake = make_client([make_stream('{"ok": 1}')])
    url = "https://example.com/signed/report.pdf?sig=abc"
    client.extract_pdf(PDF_BYTES, "report.pdf", PROMPT, file_url=url)

    content = fake.calls[0]["messages"][0]["content"]
    assert content[0] == {"type": "file", "file": {"file_url": url}}
    assert "file_data" not in content[0]["file"]
    assert content[1] == {"type": "text", "text": PROMPT}


def test_extract_pdf_request_id_falls_back_to_generated_uuid() -> None:
    client, _ = make_client([make_stream('{"ok": 1}', request_id=None)])
    result = client.extract_pdf(PDF_BYTES, "report.pdf", PROMPT)
    assert result.request_id.startswith("req_")
    assert len(result.request_id) > 10


def test_extract_pdf_request_id_from_response_header() -> None:
    stream = make_stream('{"ok": 1}', request_id=None, headers={"x-request-id": "req-from-header"})
    client, _ = make_client([stream])
    result = client.extract_pdf(PDF_BYTES, "report.pdf", PROMPT)
    assert result.request_id == "req-from-header"


# --------------------------------------------------------------------------- 重试
def test_extract_pdf_retries_rate_limit_twice_then_succeeds() -> None:
    client, fake = make_client(
        [rate_limit_error(), rate_limit_error(), make_stream('{"ok": true}')]
    )

    result = client.extract_pdf(PDF_BYTES, "report.pdf", PROMPT)

    assert result.attempts == 3
    assert result.text == '{"ok": true}'
    assert len(fake.calls) == 3


def test_extract_pdf_retries_exhausted_on_5xx_maps_model_api_error(tmp_path: Path) -> None:
    client, fake = make_client(
        [server_error()],
        settings=make_settings(model_max_retries=2),
        log_dir=tmp_path / "logs",
    )

    with pytest.raises(AppError) as excinfo:
        client.extract_pdf(PDF_BYTES, "report.pdf", PROMPT)

    error = excinfo.value
    assert error.code == ErrorCode.MODEL_API_ERROR
    assert error.retryable is True
    assert len(fake.calls) == 3  # 1 次首调 + 2 次重试

    records = read_log(tmp_path / "logs" / "run_adapter_test.jsonl")
    assert len(records) == 1
    assert records[0]["status"] == "ERROR"
    assert records[0]["error_code"] == "MODEL_API_ERROR"
    assert records[0]["attempts"] == 3
    assert records[0]["pdf_sha256"] == PDF_SHA256


def test_extract_pdf_rate_limit_exhausted_maps_429() -> None:
    client, fake = make_client([rate_limit_error()], settings=make_settings(model_max_retries=1))

    with pytest.raises(AppError) as excinfo:
        client.extract_pdf(PDF_BYTES, "report.pdf", PROMPT)

    assert excinfo.value.code == ErrorCode.MODEL_RATE_LIMIT
    assert excinfo.value.retryable is True
    assert len(fake.calls) == 2


def test_extract_pdf_timeout_maps_model_timeout() -> None:
    client, fake = make_client([timeout_error()], settings=make_settings(model_max_retries=0))

    with pytest.raises(AppError) as excinfo:
        client.extract_pdf(PDF_BYTES, "report.pdf", PROMPT)

    assert excinfo.value.code == ErrorCode.MODEL_TIMEOUT
    assert excinfo.value.retryable is True
    assert len(fake.calls) == 1


def test_extract_pdf_connection_error_maps_retryable_api_error() -> None:
    client, _ = make_client(
        [connection_error(), make_stream('{"ok": 1}')],
        settings=make_settings(model_max_retries=2),
    )
    result = client.extract_pdf(PDF_BYTES, "report.pdf", PROMPT)
    assert result.attempts == 2


def test_extract_pdf_4xx_is_not_retried() -> None:
    client, fake = make_client(
        [bad_request_error()],
        settings=make_settings(model_max_retries=3),
    )

    with pytest.raises(AppError) as excinfo:
        client.extract_pdf(PDF_BYTES, "report.pdf", PROMPT)

    assert excinfo.value.code == ErrorCode.MODEL_API_ERROR
    assert excinfo.value.retryable is False
    assert len(fake.calls) == 1


# --------------------------------------------------------------------------- 空内容 / 坏 JSON
def test_extract_pdf_empty_content_maps_model_bad_json(tmp_path: Path) -> None:
    client, fake = make_client([make_stream("")], log_dir=tmp_path / "logs")

    with pytest.raises(AppError) as excinfo:
        client.extract_pdf(PDF_BYTES, "report.pdf", PROMPT)

    assert excinfo.value.code == ErrorCode.MODEL_BAD_JSON
    assert excinfo.value.retryable is False
    assert len(fake.calls) == 1

    records = read_log(tmp_path / "logs" / "run_adapter_test.jsonl")
    assert records[0]["status"] == "ERROR"
    assert records[0]["error_code"] == "MODEL_BAD_JSON"


def test_extract_pdf_rejects_empty_pdf_bytes() -> None:
    client, fake = make_client([make_stream('{"ok": 1}')])
    with pytest.raises(AppError) as excinfo:
        client.extract_pdf(b"", "report.pdf", PROMPT)
    assert excinfo.value.code == ErrorCode.PDF_INVALID
    assert fake.calls == []


# --------------------------------------------------------------------------- repair_json
def test_repair_json_returns_valid_json_without_pdf() -> None:
    broken = '{"candidates": [{"raw_value": "100",'
    script = [make_stream('```json\n{"candidates": []}\n```')]
    client, fake = make_client(script)

    repaired = client.repair_json(broken, "Expecting ',' delimiter")

    assert repaired == '{"candidates": []}'
    assert json.loads(repaired) == {"candidates": []}
    kwargs = fake.calls[0]
    content = kwargs["messages"][0]["content"]
    assert isinstance(content, str)
    assert broken in content
    assert "Expecting ',' delimiter" in content
    assert '"type": "file"' not in content
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["stream"] is True


def test_repair_json_invalid_json_raises_bad_json() -> None:
    client, fake = make_client([make_stream("still not json")], settings=make_settings())

    with pytest.raises(AppError) as excinfo:
        client.repair_json("broken", "Expecting value")

    assert excinfo.value.code == ErrorCode.MODEL_BAD_JSON
    assert excinfo.value.retryable is False
    assert len(fake.calls) == 1


def test_repair_json_empty_content_raises_bad_json() -> None:
    client, _ = make_client([make_stream("")])
    with pytest.raises(AppError) as excinfo:
        client.repair_json("broken", "Expecting value")
    assert excinfo.value.code == ErrorCode.MODEL_BAD_JSON


def test_repair_json_maps_api_error() -> None:
    client, _ = make_client([bad_request_error()])
    with pytest.raises(AppError) as excinfo:
        client.repair_json("broken", "Expecting value")
    assert excinfo.value.code == ErrorCode.MODEL_API_ERROR
    assert excinfo.value.retryable is False


# --------------------------------------------------------------------------- ping
def test_ping_success_returns_smoke_dict() -> None:
    client, fake = make_client(
        [FakeCompletion("pong", usage=make_usage(3, 1, 4), request_id="req-ping")]
    )
    result = client.ping()
    assert result["ok"] is True
    assert result["model"] == "qwen3.8-flash"
    assert result["request_id"] == "req-ping"
    assert result["latency_ms"] >= 0
    assert len(fake.calls) == 1
    assert "stream" not in fake.calls[0]


def test_ping_failure_returns_dict_instead_of_raising(tmp_path: Path) -> None:
    client, _ = make_client([connection_error()], log_dir=tmp_path / "logs")
    result = client.ping()
    assert result["ok"] is False
    assert result["error_code"] == "MODEL_API_ERROR"

    records = read_log(tmp_path / "logs" / "run_adapter_test.jsonl")
    assert records[0]["status"] == "ERROR"


def test_consume_stream_records_first_token_ms():
    """首 token 计时：第一个非空 content chunk 的时间被记录，用于拆分解析/预填与生成耗时。"""
    from types import SimpleNamespace

    from app.adapters.qwen import _consume_stream

    def event(content=None, finish=None, usage=None):
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=content), finish_reason=finish)],
            usage=usage,
        )

    stream = [
        event(content=None),
        event(content="{"),
        event(content="}"),
        event(content=None, finish="stop", usage=make_usage()),
    ]
    text, usage, finish_reason, _request_id, first_token_ms = _consume_stream(stream)
    assert text == "{}"
    assert finish_reason == "stop"
    assert usage["total_tokens"] == 15
    assert first_token_ms is not None and first_token_ms >= 0


def test_extract_pdf_returns_first_token_ms():
    client, _ = make_client([make_stream('{"ok": true}')])
    result = client.extract_pdf(PDF_BYTES, "x.pdf", PROMPT)
    assert isinstance(result, ModelResult)
    assert result.first_token_ms is not None and result.first_token_ms >= 0
