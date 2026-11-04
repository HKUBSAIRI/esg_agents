"""单测用 Qwen fake 客户端（不访问网络）。

`QwenPDFClient` 支持注入任意 OpenAI 兼容 client；本模块提供最小 fake：

- `FakeOpenAI(script)`：`script` 是每次 `create()` 调用的返回值/异常列表
  （按调用次数取；不足时重复最后一项）；
- `make_stream(text, ...)`：返回模拟流式 chunk 的可迭代对象；
- `rate_limit_error()` / `server_error()` / `timeout_error()` / `connection_error()`：
  构造真实的 openai 异常实例，用于验证重试与错误映射。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

try:
    import httpx2
except ImportError:
    import httpx as httpx2  # SDK installations using the original httpx package.
from openai import (
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)


def make_usage(
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    total_tokens: int = 15,
) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


class FakeChunk:
    """模拟 `ChatCompletionChunk`：有 `.choices[0].delta.content` 与 `.usage`。"""

    def __init__(
        self,
        content: str | None = None,
        finish_reason: str | None = None,
        usage: Any = None,
    ) -> None:
        self.id = "chatcmpl-fake"
        self.model = "qwen3.8-flash"
        self.choices: list[SimpleNamespace] = []
        if content is not None or finish_reason is not None:
            self.choices = [
                SimpleNamespace(
                    index=0,
                    delta=SimpleNamespace(content=content, role="assistant"),
                    finish_reason=finish_reason,
                )
            ]
        self.usage = usage


class FakeStream(list):
    """模拟 OpenAI `Stream`：可迭代，带 request id / response headers 属性。"""

    def __init__(
        self,
        chunks: list[FakeChunk] | None = None,
        *,
        request_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(chunks or [])
        self._request_id = request_id
        self.response = SimpleNamespace(headers=headers or {})


class FakeCompletion:
    """模拟非流式 `ChatCompletion`（用于 `ping()`）。"""

    def __init__(
        self,
        content: str = "pong",
        *,
        usage: Any = None,
        request_id: str | None = None,
        finish_reason: str = "stop",
    ) -> None:
        self.id = "chatcmpl-fake"
        self.model = "qwen3.8-flash"
        self.choices = [
            SimpleNamespace(
                index=0,
                message=SimpleNamespace(content=content, role="assistant"),
                finish_reason=finish_reason,
            )
        ]
        self.usage = usage if usage is not None else make_usage(3, 1, 4)
        self._request_id = request_id


class FakeCompletions:
    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self._script) - 1)
        item = self._script[index]
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item(kwargs)
        return item


class FakeOpenAI:
    """最小 OpenAI 兼容客户端：`client.chat.completions.create(**kwargs)`。"""

    def __init__(self, script: list[Any]) -> None:
        self.completions = FakeCompletions(script)
        self.chat = SimpleNamespace(completions=self.completions)

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.completions.calls


def make_stream(
    text: str,
    *,
    request_id: str | None = "req_fake_123",
    usage: Any = None,
    finish_reason: str = "stop",
    headers: dict[str, str] | None = None,
) -> FakeStream:
    """把文本包装为模拟流：内容 chunk + finish chunk + usage chunk。"""
    chunks: list[FakeChunk] = []
    if text:
        chunks.append(FakeChunk(content=text))
        chunks.append(FakeChunk(finish_reason=finish_reason))
    chunks.append(FakeChunk(usage=usage if usage is not None else make_usage()))
    return FakeStream(chunks, request_id=request_id, headers=headers)


def _make_response(status_code: int, message: str = "fake error") -> httpx2.Response:
    request = httpx2.Request(
        "POST",
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    )
    return httpx2.Response(
        status_code,
        request=request,
        json={"error": {"message": message}},
    )


def make_status_error(
    error_cls: type,
    status_code: int,
    message: str = "fake error",
) -> BaseException:
    body = {"error": {"message": message}}
    return error_cls(message, response=_make_response(status_code, message), body=body)


def rate_limit_error() -> RateLimitError:
    return make_status_error(RateLimitError, 429, "rate limited")  # type: ignore[return-value]


def server_error(message: str = "internal server error") -> InternalServerError:
    return make_status_error(InternalServerError, 500, message)  # type: ignore[return-value]


def bad_request_error(message: str = "invalid request") -> BadRequestError:
    return make_status_error(BadRequestError, 400, message)  # type: ignore[return-value]


def timeout_error() -> APITimeoutError:
    request = httpx2.Request(
        "POST",
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    )
    return APITimeoutError(request=request)


def connection_error() -> APIConnectionError:
    request = httpx2.Request(
        "POST",
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    )
    return APIConnectionError(request=request)


__all__ = [
    "FakeChunk",
    "FakeCompletion",
    "FakeOpenAI",
    "FakeStream",
    "bad_request_error",
    "connection_error",
    "make_status_error",
    "make_stream",
    "make_usage",
    "rate_limit_error",
    "server_error",
    "timeout_error",
]
