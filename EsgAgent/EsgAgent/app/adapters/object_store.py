"""本地对象存储适配器。

契约见 `docs/07_工程目录与开发接口.md`：PDF 原件以不可变对象存储，URI 形如
`local://pdf/<sha256>.pdf`。生产可替换为 MinIO/S3 实现（`get_bytes` / `exists` /
`public_url` 保持同一接口）。

关键约束：
- 同一个 key 只允许写入一次；重复写入相同内容直接复用，内容不同则 `CONFLICT`；
- key 必须是相对路径，禁止绝对路径与 `..` 穿越；
- 本地存储没有公网 URL，`public_url()` 返回 `None`（调用方回退到 base64 `file_data`）。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from app.core.errors import AppError, ErrorCode

LOCAL_SCHEME = "local://"


class LocalObjectStore:
    """基于本地文件系统的不可变对象存储。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 写
    def put_bytes(self, key: str, data: bytes, content_type: str = "application/pdf") -> str:
        """写入对象并返回 `local://<key>`。

        Args:
            key: 相对 key，如 `pdf/<sha256>.pdf`。
            data: 对象字节。
            content_type: 内容类型（本地实现仅记录在调用方；不落 sidecar 文件）。

        Raises:
            AppError: 非法 key（`INTERNAL`）或已有同 key 不同内容（`CONFLICT`）。
        """
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data 必须为 bytes")
        payload = bytes(data)
        path = self._path_from_key(key)
        if path.exists():
            try:
                existing = path.read_bytes()
            except OSError as exc:  # 读不到已有对象时保守报冲突，绝不覆盖
                raise AppError(
                    ErrorCode.CONFLICT,
                    f"对象已存在但无法读取，拒绝覆盖: {key}",
                    retryable=False,
                    details={"key": key, "error": str(exc)},
                ) from exc
            if existing == payload:
                return LOCAL_SCHEME + key
            raise AppError(
                ErrorCode.CONFLICT,
                f"对象已存在且内容不同，不可变存储拒绝覆盖: {key}",
                retryable=False,
                details={"key": key, "existing_size": len(existing), "new_size": len(payload)},
            )

        path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写：先写同目录临时文件，再 os.replace（避免半写对象被读到）。
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as fh:
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
        return LOCAL_SCHEME + key

    # ------------------------------------------------------------------ 读
    def get_bytes(self, uri: str) -> bytes:
        """按 `local://<key>` 读取对象字节。"""
        path = self._path_from_uri(uri)
        if not path.is_file():
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"对象不存在: {uri}",
                retryable=False,
                details={"uri": uri},
            )
        try:
            return path.read_bytes()
        except OSError as exc:
            raise AppError(
                ErrorCode.INTERNAL,
                f"对象读取失败: {uri}",
                retryable=False,
                details={"uri": uri, "error": str(exc)},
            ) from exc

    def exists(self, uri: str) -> bool:
        """对象是否存在；非法 URI 返回 False（不抛异常，便于探测）。"""
        try:
            path = self._path_from_uri(uri)
        except (AppError, ValueError, TypeError):
            return False
        return path.is_file()

    def public_url(self, uri: str, ttl_s: int = 900) -> str | None:
        """本地对象存储没有公网 URL；返回 None，调用方回退到 base64 `file_data`。"""
        _ = (uri, ttl_s)
        return None

    # ------------------------------------------------------------------ 路径工具
    def _path_from_key(self, key: str) -> Path:
        if not isinstance(key, str) or not key.strip():
            raise AppError(ErrorCode.INTERNAL, "对象 key 不得为空", retryable=False)
        if key.startswith("/") or "\\" in key:
            raise AppError(
                ErrorCode.INTERNAL,
                f"对象 key 必须是相对路径: {key!r}",
                retryable=False,
            )
        parts = [p for p in key.split("/") if p not in ("", ".")]
        if not parts or any(p == ".." for p in parts):
            raise AppError(
                ErrorCode.INTERNAL,
                f"对象 key 含非法路径片段: {key!r}",
                retryable=False,
            )
        path = self.root.joinpath(*parts)
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise AppError(
                ErrorCode.INTERNAL,
                f"对象 key 越界: {key!r}",
                retryable=False,
            ) from exc
        return resolved

    def _path_from_uri(self, uri: str) -> Path:
        if not isinstance(uri, str) or not uri.startswith(LOCAL_SCHEME):
            raise AppError(
                ErrorCode.INTERNAL,
                f"不支持的本地对象 URI: {uri!r}",
                retryable=False,
                details={"uri": str(uri)},
            )
        key = uri[len(LOCAL_SCHEME) :]
        return self._path_from_key(key)
