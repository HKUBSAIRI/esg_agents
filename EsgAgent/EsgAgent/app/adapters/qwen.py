"""Qwen PDF 理解适配器（OpenAI 兼容 chat.completions）。

契约见 `docs/01_Qwen文档理解与提取归类Agent.md` 与 `docs/07_工程目录与开发接口.md`：

- 模型 `qwen3.8-flash` 直读整份 PDF（平台按页解析），后端不做本地文本/OCR/渲染；
- PDF 通过 `file_url`（短时签名 URL，首选）或 `file_data`（base64 + filename）传入；
- 必须流式（首 token 可能几十秒）并设置大 timeout，聚合增量文本后整体返回；
- 429/超时/5xx/连接错误指数退避，其它 4xx 不重试；
- 所有调用写 `RunLogger.model_call`，只记录元数据，禁止写入 API Key、完整 base64、
  prompt 全文或长效签名 URL。
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

from app.core.errors import AppError, ErrorCode
from app.core.logging import RunLogger

#: 日志中成功/失败状态取值
STATUS_SUCCESS = "SUCCESS"
STATUS_ERROR = "ERROR"

#: 常见 request id 响应头（DashScope / 通用网关）
_REQUEST_ID_HEADERS = (
    "x-request-id",
    "x-dashscope-request-id",
    "x-dashscope-requestid",
    "request-id",
    "x-acs-request-id",
)

_JSON_FENCE_PREFIXES = ("```json", "```JSON", "```")


@dataclass
class ModelResult:
    """一次模型调用的结果（已聚合流式增量文本）。"""

    text: str
    model: str
    request_id: str
    latency_ms: int
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    attempts: int = 1
    # 首个非空文本增量相对请求发出的毫秒延迟（区分“平台解析+预填”与“生成”耗时）
    first_token_ms: int | None = None


class QwenPDFClient:
    """PDF 直读 + 纯文本修复调用的薄封装。

    Args:
        settings: `app.core.settings.Settings`（模型名、超时、重试、token 上限）。
        run_logger: 可选 `RunLogger`；提供时每次调用写 `model_call` 事件。
        client: 可选注入的 OpenAI 兼容客户端（单测注入 fake，不访问网络）。
    """

    def __init__(self, settings: Any, run_logger: RunLogger | None = None, client: OpenAI | None = None) -> None:
        self.settings = settings
        self.run_logger = run_logger
        self._client = client

    # ------------------------------------------------------------------ 客户端
    @property
    def client(self) -> OpenAI:
        """惰性构造真实 OpenAI 客户端（注入 fake 时不会创建）。"""
        if self._client is None:
            timeout = float(getattr(self.settings, "qwen_timeout_s", 600.0))
            self._client = OpenAI(
                api_key=self.settings.dashscope_api_key,
                base_url=self.settings.dashscope_base_url,
                timeout=timeout,
            )
        return self._client

    # ------------------------------------------------------------------ 对外接口
    def ping(self) -> dict:
        """极小文本调用，用于部署环境冒烟。

        返回 `{"ok", "model", "latency_ms", "request_id"}`；失败时额外带
        `error_code` / `error`，不抛异常（冒烟需要拿到可判定的结果）。
        """
        model = str(self.settings.qwen_model)
        t0 = time.perf_counter()
        try:
            resp = self.client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=8,
                temperature=0.0,
            )
            elapsed = int((time.perf_counter() - t0) * 1000)
            request_id = _extract_request_id(resp) or _new_request_id()
            usage = _usage_to_dict(getattr(resp, "usage", None))
            finish_reason = None
            choices = getattr(resp, "choices", None) or []
            if choices:
                finish_reason = getattr(choices[0], "finish_reason", None)
            self._log_model_call(
                model=model,
                request_id=request_id,
                usage=usage,
                latency_ms=elapsed,
                finish_reason=finish_reason,
                status=STATUS_SUCCESS,
                error_code=None,
                attempts=1,
                pdf_bytes=0,
                pdf_sha256=None,
                prompt_chars=len("ping"),
            )
            return {
                "ok": True,
                "model": model,
                "latency_ms": elapsed,
                "request_id": request_id,
                "usage": usage,
            }
        except Exception as exc:  # noqa: BLE001 - 冒烟结果以 dict 返回，不向上抛
            elapsed = int((time.perf_counter() - t0) * 1000)
            app_error = self._to_app_error(exc)
            request_id = _extract_request_id(exc) or _new_request_id()
            self._log_model_call(
                model=model,
                request_id=request_id,
                usage={},
                latency_ms=elapsed,
                finish_reason=None,
                status=STATUS_ERROR,
                error_code=app_error.code.value,
                attempts=1,
                pdf_bytes=0,
                pdf_sha256=None,
                prompt_chars=len("ping"),
            )
            return {
                "ok": False,
                "model": model,
                "latency_ms": elapsed,
                "request_id": request_id,
                "error_code": app_error.code.value,
                "error": app_error.message,
            }

    def extract_pdf(
        self,
        pdf_bytes: bytes,
        filename: str,
        prompt: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        file_url: str | None = None,
    ) -> ModelResult:
        """把整份 PDF 与 prompt 发给模型，返回聚合后的完整文本。

        Args:
            pdf_bytes: PDF 原始字节（base64 传输路径使用；file_url 路径也要用于哈希/日志）。
            filename: 传给平台的 PDF 文件名（base64 路径必填）。
            prompt: 提取 prompt（含 Skill 正文），不写入日志。
            max_tokens: 覆盖默认输出上限。
            temperature: 覆盖默认温度。
            file_url: 有值时优先使用短时签名 URL，否则 base64 内联。

        Raises:
            AppError: `MODEL_RATE_LIMIT` / `MODEL_TIMEOUT` / `MODEL_API_ERROR` /
                `MODEL_BAD_JSON`（空内容），`retryable` 按失败类型标记。
        """
        if not isinstance(pdf_bytes, (bytes, bytearray)) or not pdf_bytes:
            raise AppError(ErrorCode.PDF_INVALID, "PDF 字节为空，无法调用模型", retryable=False)
        pdf_bytes = bytes(pdf_bytes)

        model = str(self.settings.qwen_model)
        messages = [
            {"role": "user", "content": _build_file_content(pdf_bytes, filename, prompt, file_url)}
        ]
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "response_format": {"type": "json_object"},
            "max_tokens": int(max_tokens)
            if max_tokens is not None
            else int(self.settings.qwen_max_output_tokens),
            "temperature": float(temperature)
            if temperature is not None
            else float(self.settings.qwen_temperature),
        }
        pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
        return self._call_with_retry(
            kwargs,
            model=model,
            pdf_bytes=len(pdf_bytes),
            pdf_sha256=pdf_sha256,
            prompt_chars=len(prompt),
        )

    def repair_json(self, broken_text: str, validation_error: str) -> str:
        """纯文本修复调用：把不合法的 JSON 修成合法 JSON（不带 PDF，一次调用）。

        返回去除 Markdown 代码围栏后的合法 JSON 文本；仍不合法时抛
        `AppError(MODEL_BAD_JSON, retryable=False)`。
        """
        model = str(self.settings.qwen_model)
        prompt = _build_repair_prompt(broken_text, validation_error)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "stream_options": {"include_usage": True},
            "response_format": {"type": "json_object"},
            "max_tokens": int(self.settings.qwen_max_output_tokens),
            "temperature": float(self.settings.qwen_temperature),
        }
        t0 = time.perf_counter()
        try:
            stream = self.client.chat.completions.create(**kwargs)
            text, usage, finish_reason, request_id, _first_token_ms = _consume_stream(stream, t0)
        except Exception as exc:  # noqa: BLE001 - 统一映射为 AppError
            elapsed = int((time.perf_counter() - t0) * 1000)
            app_error = self._to_app_error(exc)
            request_id = _extract_request_id(exc) or _new_request_id()
            self._log_model_call(
                model=model,
                request_id=request_id,
                usage={},
                latency_ms=elapsed,
                finish_reason=None,
                status=STATUS_ERROR,
                error_code=app_error.code.value,
                attempts=1,
                pdf_bytes=0,
                pdf_sha256=None,
                prompt_chars=len(prompt),
            )
            raise app_error from exc

        elapsed = int((time.perf_counter() - t0) * 1000)
        cleaned = _strip_json_fences(text)
        if not cleaned:
            err = AppError(
                ErrorCode.MODEL_BAD_JSON,
                "JSON 修复调用返回空内容",
                retryable=False,
                details={"usage": usage},
            )
            self._log_model_call(
                model=model,
                request_id=request_id,
                usage=usage,
                latency_ms=elapsed,
                finish_reason=finish_reason,
                status=STATUS_ERROR,
                error_code=err.code.value,
                attempts=1,
                pdf_bytes=0,
                pdf_sha256=None,
                prompt_chars=len(prompt),
            )
            raise err
        try:
            json.loads(cleaned)
        except (json.JSONDecodeError, ValueError) as exc:
            err = AppError(
                ErrorCode.MODEL_BAD_JSON,
                "JSON 修复调用仍返回非法 JSON",
                retryable=False,
                details={"error": str(exc), "usage": usage},
            )
            self._log_model_call(
                model=model,
                request_id=request_id,
                usage=usage,
                latency_ms=elapsed,
                finish_reason=finish_reason,
                status=STATUS_ERROR,
                error_code=err.code.value,
                attempts=1,
                pdf_bytes=0,
                pdf_sha256=None,
                prompt_chars=len(prompt),
            )
            raise err
        self._log_model_call(
            model=model,
            request_id=request_id,
            usage=usage,
            latency_ms=elapsed,
            finish_reason=finish_reason,
            status=STATUS_SUCCESS,
            error_code=None,
            attempts=1,
            pdf_bytes=0,
            pdf_sha256=None,
            prompt_chars=len(prompt),
        )
        return cleaned

    # ------------------------------------------------------------------ 重试
    def _call_with_retry(
        self,
        kwargs: dict[str, Any],
        *,
        model: str,
        pdf_bytes: int,
        pdf_sha256: str,
        prompt_chars: int,
    ) -> ModelResult:
        max_attempts = max(1, int(getattr(self.settings, "model_max_retries", 3)) + 1)
        base_s = max(0.0, float(getattr(self.settings, "model_retry_base_s", 2.0)))
        t0 = time.perf_counter()
        request_id = _new_request_id()

        for attempt in range(1, max_attempts + 1):
            try:
                stream = self.client.chat.completions.create(**kwargs)
                text, usage, finish_reason, request_id, first_token_ms = _consume_stream(stream, t0)
                if not text.strip():
                    raise AppError(
                        ErrorCode.MODEL_BAD_JSON,
                        "模型返回内容为空",
                        retryable=False,
                        details={"attempts": attempt, "usage": usage},
                    )
                elapsed = int((time.perf_counter() - t0) * 1000)
                result = ModelResult(
                    text=text,
                    model=model,
                    request_id=request_id,
                    latency_ms=elapsed,
                    usage=usage,
                    finish_reason=finish_reason,
                    attempts=attempt,
                    first_token_ms=first_token_ms,
                )
                self._log_model_call(
                    model=model,
                    request_id=request_id,
                    usage=usage,
                    latency_ms=elapsed,
                    finish_reason=finish_reason,
                    status=STATUS_SUCCESS,
                    error_code=None,
                    attempts=attempt,
                    pdf_bytes=pdf_bytes,
                    pdf_sha256=pdf_sha256,
                    prompt_chars=prompt_chars,
                    first_token_ms=first_token_ms,
                )
                return result
            except AppError as err:
                elapsed = int((time.perf_counter() - t0) * 1000)
                self._log_model_call(
                    model=model,
                    request_id=request_id,
                    usage={},
                    latency_ms=elapsed,
                    finish_reason=None,
                    status=STATUS_ERROR,
                    error_code=err.code.value,
                    attempts=attempt,
                    pdf_bytes=pdf_bytes,
                    pdf_sha256=pdf_sha256,
                    prompt_chars=prompt_chars,
                )
                raise
            except Exception as exc:  # noqa: BLE001 - 未知异常统一映射
                app_error = self._to_app_error(exc)
                request_id = _extract_request_id(exc) or request_id
                if _is_retryable(exc) and attempt < max_attempts:
                    time.sleep(base_s * (2 ** (attempt - 1)))
                    continue
                elapsed = int((time.perf_counter() - t0) * 1000)
                self._log_model_call(
                    model=model,
                    request_id=request_id,
                    usage={},
                    latency_ms=elapsed,
                    finish_reason=None,
                    status=STATUS_ERROR,
                    error_code=app_error.code.value,
                    attempts=attempt,
                    pdf_bytes=pdf_bytes,
                    pdf_sha256=pdf_sha256,
                    prompt_chars=prompt_chars,
                )
                raise app_error from exc

        # 理论上不可达（循环内必定 return 或 raise）；保留兜底。
        raise AppError(ErrorCode.INTERNAL, "模型调用重试流程异常终止", retryable=False)

    # ------------------------------------------------------------------ 错误映射 / 日志
    def _to_app_error(self, exc: Exception) -> AppError:
        if isinstance(exc, AppError):
            return exc
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int) and status_code == 429:
            return AppError(
                ErrorCode.MODEL_RATE_LIMIT,
                f"模型接口限流（HTTP {status_code}），重试后仍失败",
                retryable=True,
                details={"status_code": status_code, "error_type": type(exc).__name__},
            )
        if isinstance(exc, (APITimeoutError, TimeoutError)):
            return AppError(
                ErrorCode.MODEL_TIMEOUT,
                "模型调用超时（PDF 解析首 token 可能较慢，建议增大 qwen_timeout_s）",
                retryable=True,
                details={"error_type": type(exc).__name__},
            )
        if isinstance(exc, APIConnectionError):
            return AppError(
                ErrorCode.MODEL_API_ERROR,
                "无法连接模型服务",
                retryable=True,
                details={"error_type": type(exc).__name__},
            )
        if isinstance(status_code, int) and status_code >= 500:
            return AppError(
                ErrorCode.MODEL_API_ERROR,
                f"模型接口服务端错误（HTTP {status_code}），重试后仍失败",
                retryable=True,
                details={"status_code": status_code, "error_type": type(exc).__name__},
            )
        details: dict[str, Any] = {"error_type": type(exc).__name__}
        if isinstance(status_code, int):
            details["status_code"] = status_code
        if isinstance(exc, APIStatusError):
            # DashScope 对 base64 file_data 有单字符串上限（约 28,000,000 字符），
            # 超限时返回 400 + Jackson StreamReadConstraints 报错；转换为可操作的错误。
            body_text = str(getattr(exc, "body", "") or "")
            if "exceeds the maximum allowed" in body_text or "MaxStringLength" in body_text:
                return AppError(
                    ErrorCode.PDF_LIMIT_EXCEEDED,
                    "PDF base64 超过接口单字符串上限（约 28MB，对应 PDF 约 20MB）；"
                    "请上传对象存储后用签名 URL 传 file_url（CLI: --pdf-url）",
                    retryable=False,
                    details=details,
                )
            # 4xx（除 429）通常为请求参数/鉴权错误，重试无意义。
            return AppError(
                ErrorCode.MODEL_API_ERROR,
                f"模型接口返回 HTTP {status_code}",
                retryable=False,
                details=details,
            )
        details["error"] = str(exc)[:300]
        if isinstance(exc, APIError) and not isinstance(exc, APIStatusError):
            # 无 HTTP 状态码的 APIError 多为流式传输中断/上游瞬时错误，可按可重试处理。
            return AppError(
                ErrorCode.MODEL_API_ERROR,
                f"模型调用/流式中断: {type(exc).__name__}",
                retryable=True,
                details=details,
            )
        return AppError(
            ErrorCode.MODEL_API_ERROR,
            f"模型调用失败: {type(exc).__name__}",
            retryable=False,
            details=details,
        )

    def _log_model_call(
        self,
        *,
        model: str,
        request_id: str,
        usage: dict[str, Any],
        latency_ms: int,
        finish_reason: str | None,
        status: str,
        error_code: str | None,
        attempts: int,
        pdf_bytes: int,
        pdf_sha256: str | None,
        prompt_chars: int,
        first_token_ms: int | None = None,
    ) -> None:
        """写模型调用日志。

        只记录元数据；`prompt`/base64 全文/API Key 一律不落盘（RunLogger 另有脱敏兜底）。
        """
        if self.run_logger is None:
            return
        self.run_logger.model_call(
            model=model,
            request_id=request_id,
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=finish_reason,
            status=status,
            error_code=error_code,
            attempts=attempts,
            pdf_bytes=pdf_bytes,
            pdf_sha256=pdf_sha256,
            prompt_chars=prompt_chars,
            first_token_ms=first_token_ms,
        )


# ---------------------------------------------------------------------- 模块级工具
def _build_file_content(
    pdf_bytes: bytes,
    filename: str,
    prompt: str,
    file_url: str | None,
) -> list[dict[str, Any]]:
    """构造 `messages[0].content`：PDF 文件块 + 文本块。"""
    if file_url:
        file_part: dict[str, Any] = {"type": "file", "file": {"file_url": file_url}}
    else:
        b64 = base64.b64encode(pdf_bytes).decode("ascii")
        file_part = {
            "type": "file",
            "file": {
                "file_data": f"data:application/pdf;base64,{b64}",
                "filename": filename,
            },
        }
    return [file_part, {"type": "text", "text": prompt}]


def _build_repair_prompt(broken_text: str, validation_error: str) -> str:
    """构造 JSON 修复 prompt（纯文本，不带 PDF）。"""
    return (
        "你是一个严格的 JSON 修复器。下面是一段原本应为 JSON 但校验失败的模型输出。\n"
        "请只输出一个合法 JSON 对象：保持原有信息与字段，不新增不存在的事实，不输出解释或 Markdown 代码围栏。\n"
        f"校验错误：{validation_error}\n"
        "----- 待修复内容开始 -----\n"
        f"{broken_text}\n"
        "----- 待修复内容结束 -----\n"
        "只输出 JSON。"
    )


def _consume_stream(
    stream: Any, start_perf: float | None = None
) -> tuple[str, dict[str, Any], str | None, str, int | None]:
    """消费流式响应，返回 `(text, usage, finish_reason, request_id, first_token_ms)`。"""
    chunks: list[str] = []
    usage: dict[str, Any] = {}
    finish_reason: str | None = None
    request_id = ""
    t0 = start_perf if start_perf is not None else time.perf_counter()
    first_token_ms: int | None = None
    try:
        for event in stream:
            if not request_id:
                request_id = _extract_request_id(event)
            event_usage = getattr(event, "usage", None)
            if event_usage:
                usage = _usage_to_dict(event_usage)
            choices = getattr(event, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            delta = getattr(choice, "delta", None)
            content = getattr(delta, "content", None) if delta is not None else None
            if content:
                if first_token_ms is None:
                    first_token_ms = int((time.perf_counter() - t0) * 1000)
                chunks.append(content)
            reason = getattr(choice, "finish_reason", None)
            if reason:
                finish_reason = reason
    finally:
        if not request_id:
            request_id = _extract_request_id(stream)
        # 关闭流可能触发的异常不覆盖主流程异常；OpenAI Stream 自带 close()
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响已聚合结果
                pass
    if not request_id:
        request_id = _new_request_id()
    return "".join(chunks), usage, finish_reason, request_id, first_token_ms


def _strip_json_fences(text: str) -> str:
    """去掉模型可能输出的 Markdown 代码围栏。"""
    cleaned = (text or "").strip()
    for prefix in _JSON_FENCE_PREFIXES:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    if cleaned.endswith("```"):
        cleaned = cleaned[: -len("```")].strip()
    return cleaned


def _usage_to_dict(usage: Any) -> dict[str, Any]:
    """把 usage（pydantic 对象 / dict / SimpleNamespace）转为 JSON 友好 dict。"""
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return dict(usage)
    dump = getattr(usage, "model_dump", None)
    if callable(dump):
        try:
            return dict(dump(exclude_none=True))
        except Exception:  # noqa: BLE001 - 降级处理
            pass
    try:
        return dict(vars(usage))
    except TypeError:
        return {}


def _extract_request_id(obj: Any) -> str:
    """从响应/流/异常中尽力提取 request id；取不到返回空串。"""
    if obj is None or isinstance(obj, (str, bytes)):
        return ""
    for attr in ("_request_id", "request_id"):
        value = getattr(obj, attr, None)
        if isinstance(value, str) and value:
            return value
    response = getattr(obj, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if headers is None:
        headers = getattr(obj, "headers", None)
    if headers:
        try:
            items = headers.items()
        except AttributeError:
            items = []
        lowered = {str(k).lower(): v for k, v in items}
        for name in _REQUEST_ID_HEADERS:
            value = lowered.get(name)
            if isinstance(value, str) and value:
                return value
    return ""


def _new_request_id() -> str:
    return f"req_{uuid.uuid4().hex}"


def _is_retryable(exc: Exception) -> bool:
    """429 / 超时 / 连接错误 / 5xx 可重试；其它 4xx 不重试。"""
    if isinstance(exc, (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError)):
        return True
    status_code = getattr(exc, "status_code", None)
    return isinstance(status_code, int) and status_code >= 500
