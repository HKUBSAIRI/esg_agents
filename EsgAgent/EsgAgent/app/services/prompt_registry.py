"""Prompt 模板注册器（docs/05、docs/07）。

Prompt 是 Agent 行为与输出契约的版本化载体：模板文件 ``prompts/{name}/{version}.j2``，
用 Jinja2 渲染；变量缺失直接报错（不允许静默渲染出半成品 Prompt），渲染结果记 digest，
随模型调用存档，保证同一 Prompt + Skill 输入可复现。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound, TemplateSyntaxError
from jinja2 import UndefinedError

from app.core.errors import AppError, ErrorCode


@dataclass(slots=True)
class RenderedPrompt:
    """渲染后的 Prompt。``digest`` 为渲染结果的 sha256。"""

    text: str
    name: str
    version: str
    digest: str


class PromptRegistry:
    """从 ``prompts/`` 目录按 name + version 渲染 Jinja2 模板。"""

    def __init__(self, prompts_dir: Path) -> None:
        self.prompts_dir = Path(prompts_dir)
        self._env = Environment(
            loader=FileSystemLoader(str(self.prompts_dir)),
            undefined=StrictUndefined,
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )

    def template_path(self, name: str, version: str = "v1") -> str:
        """返回模板相对路径；拒绝路径穿越。"""
        rel = f"{name}/{version}.j2"
        candidate = (self.prompts_dir / rel).resolve()
        try:
            candidate.relative_to(self.prompts_dir.resolve())
        except ValueError:
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"Prompt 模板路径非法: {rel}",
                details={"name": name, "version": version},
            ) from None
        return rel

    def render(self, name: str, version: str = "v1", **context: Any) -> RenderedPrompt:
        """渲染模板；模板缺失抛 NOT_FOUND，变量缺失/语法错误抛 SCHEMA_INVALID。"""
        rel = self.template_path(name, version)
        if not (self.prompts_dir / rel).is_file():
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"Prompt 模板不存在: {rel}",
                details={"name": name, "version": version, "prompts_dir": str(self.prompts_dir)},
            )
        try:
            template = self._env.get_template(rel)
            text = template.render(**context)
        except TemplateNotFound as exc:
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"Prompt 模板不存在: {rel}",
                details={"name": name, "version": version},
            ) from exc
        except UndefinedError as exc:
            raise AppError(
                ErrorCode.SCHEMA_INVALID,
                f"Prompt 变量缺失或未定义: {exc}",
                details={
                    "name": name,
                    "version": version,
                    "template": rel,
                    "error": str(exc),
                    "provided_keys": sorted(context),
                },
            ) from exc
        except TemplateSyntaxError as exc:
            raise AppError(
                ErrorCode.SCHEMA_INVALID,
                f"Prompt 模板语法错误: {rel}: {exc.message}",
                details={"name": name, "version": version, "template": rel, "line": exc.lineno},
            ) from exc
        except TypeError as exc:
            raise AppError(
                ErrorCode.SCHEMA_INVALID,
                f"Prompt 渲染参数不合法: {exc}",
                details={"name": name, "version": version, "template": rel, "error": str(exc)},
            ) from exc

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return RenderedPrompt(text=text, name=name, version=version, digest=digest)
