"""配置解析测试：分块并发参数、默认值与边界。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.settings import Settings

API_KEY = "sk-unit-test-fake-key-0123456789"


def _load(tmp_path, monkeypatch, **env):
    env_file = tmp_path / "empty.env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("DASHSCOPE_API_KEY", API_KEY)
    monkeypatch.delenv("EXTRACT_CHUNK_CONCURRENCY", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings.load(env_file=env_file)


def test_chunk_concurrency_default_and_env_override(tmp_path, monkeypatch):
    assert _load(tmp_path, monkeypatch).extract_chunk_concurrency == 3
    assert _load(tmp_path, monkeypatch, EXTRACT_CHUNK_CONCURRENCY="2").extract_chunk_concurrency == 2


def test_chunk_concurrency_out_of_range_rejected(tmp_path, monkeypatch):
    with pytest.raises(ValidationError):
        _load(tmp_path, monkeypatch, EXTRACT_CHUNK_CONCURRENCY="9")
