"""结构化日志：事件写 JSONL，禁止写入密钥与长效 PDF URL。"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key\"?\s*[:=]\s*\"?)[A-Za-z0-9_\-]{8,}"),
]
_LONG_B64 = re.compile(r"(base64,)[A-Za-z0-9+/=]{200,}")
_LONG_URL = re.compile(r"(https?://[^\s\"]{100,})")


def sanitize(value: Any) -> Any:
    """递归脱敏：密钥、超长 base64、长效签名 URL。"""
    if isinstance(value, str):
        out = value
        for pat in _SECRET_PATTERNS:
            out = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "***REDACTED***", out)
        out = _LONG_B64.sub(r"\1***REDACTED_BASE64***", out)
        out = _LONG_URL.sub(lambda m: m.group(1)[:60] + "...***REDACTED_URL***", out)
        return out
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    return value


class JsonlWriter:
    """线程安全的 JSONL 追加写入器。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, payload: dict[str, Any]) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(), **sanitize(payload)}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")


class RunLogger:
    """单次运行的事件日志，带 correlation_id。"""

    def __init__(self, log_dir: Path, run_id: str) -> None:
        self.run_id = run_id
        self._writer = JsonlWriter(Path(log_dir) / f"{run_id}.jsonl")

    def event(self, name: str, **fields: Any) -> None:
        self._writer.write({"run_id": self.run_id, "event": name, **fields})

    def model_call(self, **fields: Any) -> None:
        self._writer.write({"run_id": self.run_id, "event": "model_call", **fields})


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
