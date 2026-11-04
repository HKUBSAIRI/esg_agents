"""统一错误模型。

所有模块抛 `AppError`，API 层按 `http_status` 映射；Agent 层据 `retryable` 决定重试。
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    # 输入 / PDF
    PDF_INVALID = "PDF_INVALID"
    PDF_LIMIT_EXCEEDED = "PDF_LIMIT_EXCEEDED"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    # 模型
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_RATE_LIMIT = "MODEL_RATE_LIMIT"
    MODEL_BAD_JSON = "MODEL_BAD_JSON"
    MODEL_API_ERROR = "MODEL_API_ERROR"
    # 证据 / 分类
    EVIDENCE_NOT_LOCATED = "EVIDENCE_NOT_LOCATED"
    METRIC_VERSION_UNKNOWN = "METRIC_VERSION_UNKNOWN"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    SKILL_MISSING = "SKILL_MISSING"
    # 通用
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    INTERNAL = "INTERNAL"


_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.PDF_INVALID: 400,
    ErrorCode.PDF_LIMIT_EXCEEDED: 400,
    ErrorCode.SOURCE_CHANGED: 409,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.SCHEMA_INVALID: 422,
    ErrorCode.EVIDENCE_NOT_LOCATED: 422,
    ErrorCode.METRIC_VERSION_UNKNOWN: 422,
    ErrorCode.SKILL_MISSING: 500,
    ErrorCode.MODEL_RATE_LIMIT: 429,
    ErrorCode.MODEL_TIMEOUT: 503,
    ErrorCode.MODEL_BAD_JSON: 503,
    ErrorCode.MODEL_API_ERROR: 503,
    ErrorCode.INTERNAL: 500,
}


class AppError(Exception):
    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.code = ErrorCode(code) if not isinstance(code, ErrorCode) else code
        self.message = message
        self.retryable = retryable
        self.details = details or {}
        self.correlation_id = correlation_id
        super().__init__(f"[{self.code.value}] {message}")

    @property
    def http_status(self) -> int:
        return _HTTP_STATUS.get(self.code, 500)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
            "correlation_id": self.correlation_id,
            "http_status": self.http_status,
        }
