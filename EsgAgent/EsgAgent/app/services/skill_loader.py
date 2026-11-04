"""Skill 加载器（docs/05、docs/07）。

职责边界：
- Skill 是**版本化知识**（markdown + YAML 风格 front matter），不运行代码；
- 本模块只负责：解析 front matter、登记指标、按任务/行业/指标拼装 SkillBundle、
  计算稳定 digest、按 token 预算裁剪并记录 omitted；
- 单位换算、数值标准化由 Python 的 normalizer 负责，**不在 Skill 或模型侧换算**。

front matter 解析为自实现（不引入 PyYAML 依赖），支持的值形态：
字符串、整数、bool、null、`[a, b]` 流式列表、以及以点号命名的引用型 ID
（如 `common.evidence`）。不支持块式列表/锚点/多行标量，Skill 编写时不要使用。

目录约定（docs/05）::

    skills/common/{evidence,normalization}.md
    skills/industries/{industry_code}/recognition.md
    skills/metrics/{metric_id}/{version}.md
    skills/evaluation/{rule_id}/{version}.md

加载顺序：common → industry → metric（按 metric_ids 顺序）→ evaluation；
`depends_on` 递归去重；同 kind 内按 `priority` 降序（metric 同优先级时保持
metric_ids 请求顺序）；超过 `max_tokens` 时从低优先级开始裁剪并记入 `omitted`；
缺少必需 skill（common/industry/metric）抛 `AppError(SKILL_MISSING)`。
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from app.core.errors import AppError, ErrorCode
from app.models.contracts import MetricDefinition

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

FRONT_MATTER_DELIM = "---"

_KINDS = ("common", "industry", "metric", "evaluation")
_KIND_ORDER = {kind: i for i, kind in enumerate(_KINDS)}
_REQUIRED_FIELDS = ("id", "version", "kind")

_VALUE_KINDS = {"VOLUME", "MASS", "RATIO", "INTENSITY", "MONEY", "POLICY", "NARRATIVE"}
_CSV_COLUMN_REQUIRED_KINDS = {"VOLUME", "MASS", "RATIO", "MONEY"}
_CSV_COLUMN_FORBIDDEN_KINDS = {"POLICY", "NARRATIVE"}

_DEFAULT_PRIORITY = 50
_CHARS_PER_TOKEN = 1.8  # 中文近似：1 token ≈ 1.8 字符
_DEFAULT_MAX_TOKENS = 12000


# ---------------------------------------------------------------------------
# front matter 解析（YAML 子集，无第三方依赖）
# ---------------------------------------------------------------------------


def _strip_inline_comment(value: str) -> str:
    """去掉未加引号值后面的 `` # 注释``；引号内的 ``#`` 保留。"""
    quote: str | None = None
    for i, ch in enumerate(value):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or value[i - 1].isspace()):
            return value[:i].rstrip()
    return value.rstrip()


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        inner = text[1:-1]
        if text[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        return inner
    return text


def _parse_scalar(text: str) -> Any:
    raw = text.strip()
    if raw == "":
        return None
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return _unquote(raw)
    lowered = raw.lower()
    if lowered in ("null", "~", "none"):
        return None
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if re.fullmatch(r"[+-]?\d+", raw):
        return int(raw)
    if re.fullmatch(r"[+-]?\d+\.\d+", raw):
        return float(raw)
    return raw


def _split_flow_list(inner: str) -> list[str]:
    """按逗号切分流式列表，引号内的逗号不切。"""
    items: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == ",":
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        items.append(tail)
    return items


def _parse_value(raw: str) -> Any:
    text = _strip_inline_comment(raw).strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(item) for item in _split_flow_list(inner)]
    return _parse_scalar(text)


def parse_front_matter(content: str) -> tuple[dict[str, Any], str]:
    """解析 markdown 的 front matter。

    返回 ``(meta, body)``；body 为闭合 ``---`` 之后的正文（已去掉首尾空行）。
    若无 front matter，抛 ``AppError(SCHEMA_INVALID)``。
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != FRONT_MATTER_DELIM:
        raise AppError(
            ErrorCode.SCHEMA_INVALID,
            "缺少 front matter：文件须以 '---' 开头",
            details={},
        )
    close: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == FRONT_MATTER_DELIM:
            close = idx
            break
    if close is None:
        raise AppError(
            ErrorCode.SCHEMA_INVALID,
            "front matter 未闭合：缺少结尾 '---'",
            details={},
        )

    meta: dict[str, Any] = {}
    for lineno, line in enumerate(lines[1:close], start=2):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in line:
            raise AppError(
                ErrorCode.SCHEMA_INVALID,
                f"front matter 第 {lineno} 行无法解析（缺少 ':'）: {stripped!r}",
                details={"line": lineno, "text": stripped},
            )
        key, _, raw_value = line.partition(":")
        key = key.strip()
        if not key:
            raise AppError(
                ErrorCode.SCHEMA_INVALID,
                f"front matter 第 {lineno} 行缺少字段名",
                details={"line": lineno, "text": stripped},
            )
        meta[key] = _parse_value(raw_value)

    body = "\n".join(lines[close + 1 :]).strip()
    return meta, body


# ---------------------------------------------------------------------------
# 内部数据结构
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Skill:
    """一份已解析的 Skill 文件。"""

    id: str
    version: str
    kind: str
    name: str
    body: str
    path: Path
    rel_path: str
    applies_to: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    priority: int = _DEFAULT_PRIORITY
    token_estimate: int = 0
    metric_ids: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def header(self) -> str:
        return f"## [{self.id}@{self.version}] {self.name}"


@dataclass(slots=True)
class SkillBundle:
    """一次 resolve 的结果：可直接作为 prompt 的 skill_text。"""

    text: str
    digest: str
    versions: dict[str, str] = field(default_factory=dict)
    metric_versions: dict[str, str] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    token_estimate: int = 0
    omitted: list[str] = field(default_factory=list)


def _version_key(version: str) -> tuple[tuple[int, Any], ...]:
    """版本号排序键：数字段按数值比较，非数字段按字符串。"""
    key: list[tuple[int, Any]] = []
    for part in re.split(r"[.\-+_]", version):
        key.append((0, int(part)) if part.isdigit() else (1, part))
    return tuple(key)


def _estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN))


def _coerce_str_list(value: Any, field_name: str, path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return tuple(value)
    raise AppError(
        ErrorCode.SCHEMA_INVALID,
        f"Skill {path.name} 的 {field_name} 必须是字符串列表",
        details={"path": str(path), "field": field_name, "value": repr(value)},
    )


# ---------------------------------------------------------------------------
# SkillLoader
# ---------------------------------------------------------------------------


class SkillLoader:
    """从 `skills/` 目录加载、拼装 Skill 片段。"""

    def __init__(self, skills_dir: Path) -> None:
        self.skills_dir = Path(skills_dir)
        self._skills: list[_Skill] | None = None
        self._by_id: dict[str, _Skill] | None = None
        self._metric_skills: dict[str, dict[str, _Skill]] | None = None
        self._registry: dict[str, MetricDefinition] | None = None

    # -- 扫描与解析 ---------------------------------------------------------

    def _scan(self) -> list[_Skill]:
        if self._skills is not None:
            return self._skills
        if not self.skills_dir.is_dir():
            raise AppError(
                ErrorCode.SKILL_MISSING,
                f"skills 目录不存在: {self.skills_dir}",
                details={"skills_dir": str(self.skills_dir)},
            )
        skills: list[_Skill] = []
        seen: dict[str, Path] = {}
        for path in sorted(self.skills_dir.rglob("*.md")):
            rel = path.relative_to(self.skills_dir)
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8")
            try:
                meta, body = parse_front_matter(content)
            except AppError:
                # metrics 目录下的文件必须有 front matter（防止 Skill 被静默漏登记）；
                # README 等说明文件允许无 front matter。
                if rel.parts and rel.parts[0] == "metrics":
                    raise
                continue

            for required in _REQUIRED_FIELDS:
                if not meta.get(required):
                    raise AppError(
                        ErrorCode.SCHEMA_INVALID,
                        f"Skill {rel} 缺少必需字段 {required}",
                        details={"path": str(path), "field": required},
                    )
            skill_id = str(meta["id"])
            kind = str(meta["kind"])
            if kind not in _KINDS:
                raise AppError(
                    ErrorCode.SCHEMA_INVALID,
                    f"Skill {rel} 的 kind={kind!r} 非法，允许值: {', '.join(_KINDS)}",
                    details={"path": str(path), "kind": kind},
                )
            if skill_id in seen:
                raise AppError(
                    ErrorCode.SCHEMA_INVALID,
                    f"Skill id 重复: {skill_id}（{seen[skill_id]} 与 {path}）",
                    details={"id": skill_id, "paths": [str(seen[skill_id]), str(path)]},
                )
            seen[skill_id] = path

            priority = meta.get("priority", _DEFAULT_PRIORITY)
            if not isinstance(priority, int) or isinstance(priority, bool):
                raise AppError(
                    ErrorCode.SCHEMA_INVALID,
                    f"Skill {rel} 的 priority 必须是整数",
                    details={"path": str(path), "priority": repr(priority)},
                )
            token_estimate = meta.get("token_estimate")
            if token_estimate is None:
                token_estimate = _estimate_tokens(body)
            elif not isinstance(token_estimate, int) or isinstance(token_estimate, bool) or token_estimate < 0:
                raise AppError(
                    ErrorCode.SCHEMA_INVALID,
                    f"Skill {rel} 的 token_estimate 必须是非负整数",
                    details={"path": str(path), "token_estimate": repr(token_estimate)},
                )

            name = str(meta.get("name") or skill_id)
            skills.append(
                _Skill(
                    id=skill_id,
                    version=str(meta["version"]),
                    kind=kind,
                    name=name,
                    body=body,
                    path=path,
                    rel_path=rel.as_posix(),
                    applies_to=_coerce_str_list(meta.get("applies_to"), "applies_to", path),
                    depends_on=_coerce_str_list(meta.get("depends_on"), "depends_on", path),
                    priority=priority,
                    token_estimate=int(token_estimate),
                    metric_ids=_coerce_str_list(meta.get("metric_ids"), "metric_ids", path),
                    meta=meta,
                )
            )
        if not skills:
            raise AppError(
                ErrorCode.SKILL_MISSING,
                f"skills 目录下没有任何合法 Skill: {self.skills_dir}",
                details={"skills_dir": str(self.skills_dir)},
            )
        self._skills = skills
        self._by_id = {s.id: s for s in skills}
        return skills

    def _index_metric_skills(self) -> dict[str, dict[str, _Skill]]:
        if self._metric_skills is not None:
            return self._metric_skills
        index: dict[str, dict[str, _Skill]] = {}
        for skill in self._scan():
            if skill.kind != "metric":
                continue
            if not skill.metric_ids:
                raise AppError(
                    ErrorCode.SCHEMA_INVALID,
                    f"metric Skill {skill.rel_path} 的 metric_ids 不得为空",
                    details={"path": str(skill.path)},
                )
            for metric_id in skill.metric_ids:
                versions = index.setdefault(metric_id, {})
                if skill.version in versions:
                    raise AppError(
                        ErrorCode.SCHEMA_INVALID,
                        f"指标 Skill 重复定义: {metric_id}@{skill.version}"
                        f"（{versions[skill.version].rel_path} 与 {skill.rel_path}）",
                        details={
                            "metric_id": metric_id,
                            "version": skill.version,
                            "paths": [versions[skill.version].rel_path, skill.rel_path],
                        },
                    )
                versions[skill.version] = skill
        self._metric_skills = index
        return index

    # -- 指标登记 -----------------------------------------------------------

    def load_registry(self) -> dict[str, MetricDefinition]:
        """从 ``skills/metrics/*/*.md`` front matter 构建指标登记。

        同一 metric_id 存在多版本时，返回最高版本（get_metric 可按版本精确取）。
        校验：value_kind 合法、csv_column 与 value_kind 匹配、csv_column 全局唯一、
        metric_id 唯一、必需字段齐全。
        """
        if self._registry is not None:
            return dict(self._registry)

        registry: dict[str, MetricDefinition] = {}
        csv_columns: dict[str | None, str] = {}
        for metric_id, versions in sorted(self._index_metric_skills().items()):
            version, skill = max(versions.items(), key=lambda item: _version_key(item[0]))
            registry[metric_id] = self._build_metric_definition(metric_id, version, skill)

        for metric_id, definition in registry.items():
            if definition.csv_column is None:
                continue
            key = definition.csv_column
            if key in csv_columns:
                raise AppError(
                    ErrorCode.SCHEMA_INVALID,
                    f"csv_column 重复: {key!r}（{csv_columns[key]} 与 {metric_id}）",
                    details={"csv_column": key, "metric_ids": [csv_columns[key], metric_id]},
                )
            csv_columns[key] = metric_id

        self._registry = registry
        return dict(registry)

    def _build_metric_definition(self, metric_id: str, version: str, skill: _Skill) -> MetricDefinition:
        meta = skill.meta
        value_kind = meta.get("value_kind")
        if value_kind not in _VALUE_KINDS:
            raise AppError(
                ErrorCode.SCHEMA_INVALID,
                f"指标 {metric_id}@{version} 的 value_kind={value_kind!r} 非法",
                details={"path": str(skill.path), "metric_id": metric_id, "value_kind": value_kind},
            )
        csv_column = meta.get("csv_column")
        if csv_column is not None and not isinstance(csv_column, str):
            raise AppError(
                ErrorCode.SCHEMA_INVALID,
                f"指标 {metric_id}@{version} 的 csv_column 必须是字符串或 null",
                details={"path": str(skill.path), "csv_column": repr(csv_column)},
            )
        if value_kind in _CSV_COLUMN_REQUIRED_KINDS and not csv_column:
            raise AppError(
                ErrorCode.SCHEMA_INVALID,
                f"指标 {metric_id}@{version} 的 value_kind={value_kind} 必须提供 csv_column",
                details={"path": str(skill.path), "metric_id": metric_id},
            )
        if value_kind in _CSV_COLUMN_FORBIDDEN_KINDS and csv_column is not None:
            raise AppError(
                ErrorCode.SCHEMA_INVALID,
                f"指标 {metric_id}@{version} 的 value_kind={value_kind} 不应有 csv_column",
                details={"path": str(skill.path), "metric_id": metric_id, "csv_column": csv_column},
            )
        metric_ids = skill.metric_ids
        if len(metric_ids) != 1 or metric_ids[0] != metric_id:
            raise AppError(
                ErrorCode.SCHEMA_INVALID,
                f"指标 Skill {skill.rel_path} 的 metric_ids 必须恰好为 [{metric_id!r}]",
                details={"path": str(skill.path), "metric_ids": list(metric_ids)},
            )
        name = meta.get("name")
        if not isinstance(name, str) or not name.strip():
            raise AppError(
                ErrorCode.SCHEMA_INVALID,
                f"指标 {metric_id}@{version} 缺少 name",
                details={"path": str(skill.path)},
            )
        try:
            return MetricDefinition(
                metric_id=metric_id,
                version=version,
                name=name,
                name_en=meta.get("name_en"),
                value_kind=value_kind,
                canonical_unit=meta.get("canonical_unit"),
                status=meta.get("status", "ACTIVE"),
                csv_column=csv_column,
                description=meta.get("description"),
            )
        except Exception as exc:  # pydantic ValidationError 等
            raise AppError(
                ErrorCode.SCHEMA_INVALID,
                f"指标 {metric_id}@{version} 定义不合法: {exc}",
                details={"path": str(skill.path), "metric_id": metric_id, "version": version},
            ) from exc

    def get_metric(self, metric_id: str, version: str | None = None) -> MetricDefinition:
        """按 metric_id 取登记；version 为空取最高版本。"""
        registry = self.load_registry()
        if version is None:
            if metric_id not in registry:
                raise AppError(
                    ErrorCode.METRIC_VERSION_UNKNOWN,
                    f"未登记的指标: {metric_id}",
                    details={"metric_id": metric_id, "version": None},
                )
            return registry[metric_id]

        versions = self._index_metric_skills().get(metric_id, {})
        if version not in versions:
            raise AppError(
                ErrorCode.METRIC_VERSION_UNKNOWN,
                f"未登记的指标版本: {metric_id}@{version}",
                details={
                    "metric_id": metric_id,
                    "version": version,
                    "available_versions": sorted(versions, key=_version_key),
                },
            )
        registered = self.load_registry()[metric_id]
        if registered.version == version:
            return registered
        return self._build_metric_definition(metric_id, version, versions[version])

    def list_metrics(self) -> list[MetricDefinition]:
        """按 metric_id 字典序返回全部已登记指标（最高版本）。"""
        registry = self.load_registry()
        return [registry[metric_id] for metric_id in sorted(registry)]

    # -- resolve ------------------------------------------------------------

    def resolve(
        self,
        task: str,
        industry_code: str,
        metric_ids: list[str],
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> SkillBundle:
        """按任务拼装 SkillBundle。

        - 必需 skill 缺失抛 ``AppError(SKILL_MISSING)``；
        - 未登记指标抛 ``AppError(METRIC_VERSION_UNKNOWN)``；
        - 超预算时从低优先级开始裁剪，裁剪结果记入 ``SkillBundle.omitted``（不抛错）。
        """
        skills = self._scan()
        by_id = self._by_id or {}
        if max_tokens <= 0:
            raise AppError(
                ErrorCode.SCHEMA_INVALID,
                f"max_tokens 必须为正整数，收到: {max_tokens}",
                details={"max_tokens": max_tokens},
            )

        requested: list[_Skill] = []

        # 1) common（必需）
        common = sorted(
            (s for s in skills if s.kind == "common"),
            key=lambda s: (-s.priority, s.id),
        )
        if not common:
            raise AppError(
                ErrorCode.SKILL_MISSING,
                "缺少 common Skill（skills/common/*.md）",
                details={"skills_dir": str(self.skills_dir), "kind": "common"},
            )
        requested.extend(common)

        # 2) industry（必需；applies_to 命中 industry_code）
        industry = sorted(
            (
                s
                for s in skills
                if s.kind == "industry" and industry_code in (s.applies_to or ())
            ),
            key=lambda s: (-s.priority, s.id),
        )
        if not industry:
            raise AppError(
                ErrorCode.SKILL_MISSING,
                f"缺少行业 Skill：industry_code={industry_code!r} 无匹配的 skills/industries/*/recognition.md",
                details={
                    "skills_dir": str(self.skills_dir),
                    "kind": "industry",
                    "industry_code": industry_code,
                },
            )
        requested.extend(industry)

        # 3) metric（必需；按 metric_ids 顺序）
        if not metric_ids:
            raise AppError(
                ErrorCode.SKILL_MISSING,
                "未指定任何指标 Skill（metric_ids 为空）",
                details={"skills_dir": str(self.skills_dir), "kind": "metric"},
            )
        metric_skills = self._index_metric_skills()
        if not metric_skills:
            raise AppError(
                ErrorCode.SKILL_MISSING,
                "缺少 metric Skill（skills/metrics/*/*.md）",
                details={"skills_dir": str(self.skills_dir), "kind": "metric"},
            )
        for metric_id in metric_ids:
            definition = self.get_metric(metric_id)
            skill = metric_skills.get(metric_id, {}).get(definition.version)
            if skill is None:
                raise AppError(
                    ErrorCode.SKILL_MISSING,
                    f"指标 {metric_id}@{definition.version} 的 Skill 文件缺失",
                    details={
                        "skills_dir": str(self.skills_dir),
                        "kind": "metric",
                        "metric_id": metric_id,
                        "version": definition.version,
                    },
                )
            requested.append(skill)

        # 4) evaluation（可选；applies_to 为空或命中 task）
        evaluation = sorted(
            (
                s
                for s in skills
                if s.kind == "evaluation" and (not s.applies_to or task in s.applies_to)
            ),
            key=lambda s: (-s.priority, s.id),
        )
        requested.extend(evaluation)

        # 5) depends_on 递归去重（依赖先于依赖者）
        selected: dict[str, _Skill] = {}

        def add_with_deps(skill: _Skill, stack: tuple[str, ...]) -> None:
            if skill.id in selected:
                return
            if skill.id in stack:
                raise AppError(
                    ErrorCode.SCHEMA_INVALID,
                    f"Skill 依赖成环: {' -> '.join((*stack, skill.id))}",
                    details={"cycle": list((*stack, skill.id))},
                )
            missing = [dep for dep in skill.depends_on if dep not in by_id]
            if missing:
                raise AppError(
                    ErrorCode.SKILL_MISSING,
                    f"Skill {skill.id} 依赖的 Skill 不存在: {', '.join(missing)}",
                    details={"skill_id": skill.id, "missing": missing},
                )
            for dep in skill.depends_on:
                add_with_deps(by_id[dep], (*stack, skill.id))
            selected[skill.id] = skill

        for skill in requested:
            add_with_deps(skill, ())

        # 6) 排序：kind → priority 降序 → 请求顺序 → id
        requested_index = {skill.id: idx for idx, skill in enumerate(requested)}
        def order_key(skill: _Skill) -> tuple[int, int, int, str]:
            return (
                _KIND_ORDER[skill.kind],
                -skill.priority,
                requested_index.get(skill.id, len(requested_index)),
                skill.id,
            )

        ordered = sorted(selected.values(), key=order_key)

        # 7) token 预算：从低优先级（排序末尾）开始裁剪，至少保留 1 份
        kept = list(ordered)
        total = sum(skill.token_estimate for skill in kept)
        omitted: list[str] = []
        for skill in reversed(ordered):
            if total <= max_tokens or len(kept) <= 1:
                break
            kept.remove(skill)
            total -= skill.token_estimate
            omitted.append(skill.id)

        # 8) 拼装
        text = "\n\n".join(f"{skill.header}\n\n{skill.body}" for skill in kept)
        digest = self._digest(kept)
        metric_versions = {
            metric_id: self.get_metric(metric_id).version for metric_id in metric_ids
        }

        return SkillBundle(
            text=text,
            digest=digest,
            versions={skill.id: skill.version for skill in kept},
            metric_versions=metric_versions,
            files=[skill.rel_path for skill in kept],
            token_estimate=total,
            omitted=omitted,
        )

    @staticmethod
    def _digest(skills: Iterable[_Skill]) -> str:
        """digest = sha256(按 id@version 排序后逐份 id@version + 正文)。"""
        hasher = hashlib.sha256()
        for skill in sorted(skills, key=lambda s: (s.id, _version_key(s.version))):
            hasher.update(f"{skill.id}@{skill.version}\n".encode("utf-8"))
            hasher.update(skill.body.encode("utf-8"))
            hasher.update(b"\n")
        return hasher.hexdigest()
