"""SkillLoader 契约测试（docs/05、docs/07）。

覆盖：13 指标登记与 csv_column、resolve 的 common→industry→metric 顺序与 digest 稳定性、
超预算裁剪 omitted、未知指标 / 缺失 Skill 的错误码、front matter 解析。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.core.errors import AppError, ErrorCode
from app.services.skill_loader import (
    SkillBundle,
    SkillLoader,
    parse_front_matter,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = PROJECT_ROOT / "skills"

ALL_METRIC_IDS = [
    "water_withdrawal",
    "water_consumption",
    "water_recycling_rate",
    "water_recycled_volume",
    "water_discharge",
    "water_intensity",
    "water_policy",
    "water_risk",
    "cod_emission",
    "ammonia_nitrogen_emission",
    "total_nitrogen_emission",
    "total_phosphorus_emission",
    "revenue",
]

# metric_id -> (name, name_en, value_kind, canonical_unit, csv_column)
EXPECTED_METRICS = {
    "water_withdrawal": ("总取水量", "Total water withdrawal", "VOLUME", "m³", "总取水量（m³）"),
    "water_consumption": ("总耗水量", "Total water consumption", "VOLUME", "m³", "总耗水量（m³）"),
    "water_recycling_rate": ("水循环利用率", "Water recycling rate", "RATIO", "%", "水循环利用率（%）"),
    "water_recycled_volume": (
        "循环利用水量",
        "Recycled water volume",
        "VOLUME",
        "m³",
        "循环利用水量（m³）",
    ),
    "water_discharge": ("总排水量", "Total water discharge", "VOLUME", "m³", "总排水量（m³）"),
    "water_intensity": ("水资源消耗强度", "Water intensity", "INTENSITY", None, None),
    "water_policy": ("水资源政策", "Water policy", "POLICY", None, None),
    "water_risk": ("水风险", "Water risk", "NARRATIVE", None, None),
    "cod_emission": ("COD 排放量", "COD emission", "MASS", "t", "COD 排放量（t）"),
    "ammonia_nitrogen_emission": (
        "氨氮排放量",
        "Ammonia nitrogen emission",
        "MASS",
        "t",
        "氨氮排放量（t）",
    ),
    "total_nitrogen_emission": (
        "总氮排放量",
        "Total nitrogen emission",
        "MASS",
        "t",
        "总氮排放量（t）",
    ),
    "total_phosphorus_emission": (
        "总磷排放量",
        "Total phosphorus emission",
        "MASS",
        "t",
        "总磷排放量（t）",
    ),
    "revenue": ("营业收入", "Revenue", "MONEY", "CNY_million", "营业收入（百万）"),
}


@pytest.fixture(scope="module")
def loader() -> SkillLoader:
    return SkillLoader(SKILLS_DIR)


# ---------------------------------------------------------------------------
# 指标登记
# ---------------------------------------------------------------------------


def test_registry_registers_all_13_metrics(loader: SkillLoader) -> None:
    registry = loader.load_registry()
    assert len(registry) == 13
    assert set(registry) == set(ALL_METRIC_IDS)

    for metric_id, (name, name_en, value_kind, canonical_unit, csv_column) in EXPECTED_METRICS.items():
        definition = registry[metric_id]
        assert definition.metric_id == metric_id
        assert definition.version == "1.0.0"
        assert definition.name == name
        assert definition.name_en == name_en
        assert definition.value_kind == value_kind
        assert definition.canonical_unit == canonical_unit
        assert definition.csv_column == csv_column
        assert definition.status == "ACTIVE"


def test_list_metrics_and_get_metric(loader: SkillLoader) -> None:
    metrics = loader.list_metrics()
    assert [m.metric_id for m in metrics] == sorted(ALL_METRIC_IDS)

    definition = loader.get_metric("water_withdrawal")
    assert definition.csv_column == "总取水量（m³）"
    assert loader.get_metric("water_withdrawal", "1.0.0").version == "1.0.0"
    assert loader.get_metric("revenue").canonical_unit == "CNY_million"


def test_get_metric_unknown_raises(loader: SkillLoader) -> None:
    with pytest.raises(AppError) as excinfo:
        loader.get_metric("no_such_metric")
    assert excinfo.value.code is ErrorCode.METRIC_VERSION_UNKNOWN

    with pytest.raises(AppError) as excinfo:
        loader.get_metric("water_withdrawal", "9.9.9")
    assert excinfo.value.code is ErrorCode.METRIC_VERSION_UNKNOWN
    assert excinfo.value.details["available_versions"] == ["1.0.0"]


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


def _section_index(text: str, skill_id: str) -> int:
    marker = f"## [{skill_id}@"
    idx = text.find(marker)
    assert idx >= 0, f"SkillBundle 缺少 Skill 段落: {skill_id}"
    return idx


def test_resolve_contains_common_industry_metrics(loader: SkillLoader) -> None:
    metric_ids = ["water_withdrawal", "cod_emission"]
    bundle = loader.resolve("extraction", "fb-nb", metric_ids, max_tokens=60000)

    assert isinstance(bundle, SkillBundle)
    assert bundle.omitted == []
    assert bundle.token_estimate > 0
    assert len(bundle.digest) == 64
    int(bundle.digest, 16)  # 合法 hex

    # common → industry → metric 顺序
    common_pos = min(
        _section_index(bundle.text, "common.evidence"),
        _section_index(bundle.text, "common.normalization"),
    )
    industry_pos = _section_index(bundle.text, "industry.fb-nb")
    withdrawal_pos = _section_index(bundle.text, "metric.water_withdrawal")
    cod_pos = _section_index(bundle.text, "metric.cod_emission")
    assert common_pos < industry_pos < withdrawal_pos < cod_pos

    # 版本登记
    assert bundle.metric_versions == {
        "water_withdrawal": "1.0.0",
        "cod_emission": "1.0.0",
    }
    assert bundle.versions["common.evidence"] == "1.0.0"
    assert bundle.versions["industry.fb-nb"] == "1.0.0"
    assert bundle.versions["metric.water_withdrawal"] == "1.0.0"
    assert set(bundle.files) >= {
        "common/evidence.md",
        "common/normalization.md",
        "industries/fb-nb/recognition.md",
        "metrics/water_withdrawal/1.0.0.md",
        "metrics/cod_emission/1.0.0.md",
    }

    # 正文内容确实进入 text
    assert "总取水量" in bundle.text
    assert "COD 排放量" in bundle.text
    assert "## [industry.fb-nb@1.0.0]" in bundle.text


def test_resolve_metric_order_follows_metric_ids(loader: SkillLoader) -> None:
    forward = loader.resolve("extraction", "fb-ab", ["water_withdrawal", "revenue"], max_tokens=60000)
    backward = loader.resolve("extraction", "fb-ab", ["revenue", "water_withdrawal"], max_tokens=60000)

    assert forward.text.index("metric.water_withdrawal@") < forward.text.index("metric.revenue@")
    assert backward.text.index("metric.revenue@") < backward.text.index("metric.water_withdrawal@")
    # digest 按 skill id 排序后计算（集合语义），请求顺序不同不应影响同一 Skill 集合的 digest
    assert forward.digest == backward.digest
    assert forward.versions == backward.versions


def test_resolve_industry_skill_matches_company(loader: SkillLoader) -> None:
    nb = loader.resolve("extraction", "fb-nb", ["water_withdrawal"], max_tokens=60000)
    ab = loader.resolve("extraction", "fb-ab", ["water_withdrawal"], max_tokens=60000)
    assert "饮料行业披露模式" in nb.text
    assert "啤酒行业披露模式" in ab.text
    assert "industry.fb-ab" not in nb.text
    assert "industry.fb-nb" not in ab.text


def test_resolve_digest_and_versions_are_stable(loader: SkillLoader) -> None:
    first = loader.resolve("extraction", "fb-nb", ALL_METRIC_IDS, max_tokens=60000)
    second = loader.resolve("extraction", "fb-nb", ALL_METRIC_IDS, max_tokens=60000)
    assert first.text == second.text
    assert first.digest == second.digest
    assert first.versions == second.versions
    assert first.metric_versions == second.metric_versions

    # 新实例（重新从磁盘解析）仍应得到同一 digest
    third = SkillLoader(SKILLS_DIR).resolve("extraction", "fb-nb", ALL_METRIC_IDS, max_tokens=60000)
    assert third.digest == first.digest

    # 全部 13 指标 + common + industry 都进入正文章节
    for metric_id in ALL_METRIC_IDS:
        assert f"metric.{metric_id}@1.0.0" in first.text
    assert len(first.metric_versions) == 13


def test_resolve_all_13_metrics_within_generous_budget(loader: SkillLoader) -> None:
    bundle = loader.resolve("extraction", "fb-nb", ALL_METRIC_IDS, max_tokens=60000)
    assert bundle.token_estimate <= 60000
    assert bundle.omitted == []
    assert len(bundle.files) == 2 + 1 + 13  # common ×2 + industry ×1 + metric ×13


def test_resolve_tiny_budget_trims_with_omitted_and_does_not_raise(loader: SkillLoader) -> None:
    bundle = loader.resolve("extraction", "fb-nb", ALL_METRIC_IDS, max_tokens=10)

    assert bundle.omitted, "超小预算必须产生 omitted 记录"
    assert bundle.text, "裁剪后仍应保留至少一份 Skill 正文"
    assert bundle.text.startswith("## [common.evidence@")
    assert len(bundle.files) == len(bundle.versions) == 1
    assert len(bundle.omitted) == 15  # 2 common + 1 industry + 13 metric - 1 保留

    # 裁剪结果也必须稳定
    again = loader.resolve("extraction", "fb-nb", ALL_METRIC_IDS, max_tokens=10)
    assert again.digest == bundle.digest
    assert again.omitted == bundle.omitted


def test_resolve_unknown_metric_raises(loader: SkillLoader) -> None:
    with pytest.raises(AppError) as excinfo:
        loader.resolve("extraction", "fb-nb", ["water_withdrawal", "not_registered"])
    assert excinfo.value.code is ErrorCode.METRIC_VERSION_UNKNOWN


def test_resolve_unknown_industry_raises_skill_missing(loader: SkillLoader) -> None:
    with pytest.raises(AppError) as excinfo:
        loader.resolve("extraction", "no-such-industry", ["water_withdrawal"])
    assert excinfo.value.code is ErrorCode.SKILL_MISSING
    assert excinfo.value.details["kind"] == "industry"


def test_resolve_empty_metric_ids_raises_skill_missing(loader: SkillLoader) -> None:
    with pytest.raises(AppError) as excinfo:
        loader.resolve("extraction", "fb-nb", [])
    assert excinfo.value.code is ErrorCode.SKILL_MISSING


# ---------------------------------------------------------------------------
# 缺失 Skill
# ---------------------------------------------------------------------------


def _copy_skills(tmp_path: Path, *, common: bool, industries: bool, metrics: bool) -> Path:
    target = tmp_path / "skills"
    target.mkdir()
    for name, enabled in (
        ("common", common),
        ("industries", industries),
        ("metrics", metrics),
    ):
        if enabled:
            shutil.copytree(SKILLS_DIR / name, target / name)
    return target


def test_missing_industry_skill_raises_skill_missing(tmp_path: Path) -> None:
    skills_dir = _copy_skills(tmp_path, common=True, industries=False, metrics=True)
    loader = SkillLoader(skills_dir)
    with pytest.raises(AppError) as excinfo:
        loader.resolve("extraction", "fb-nb", ["water_withdrawal"])
    assert excinfo.value.code is ErrorCode.SKILL_MISSING
    assert excinfo.value.details["kind"] == "industry"


def test_missing_common_skill_raises_skill_missing(tmp_path: Path) -> None:
    skills_dir = _copy_skills(tmp_path, common=False, industries=True, metrics=True)
    loader = SkillLoader(skills_dir)
    with pytest.raises(AppError) as excinfo:
        loader.resolve("extraction", "fb-nb", ["water_withdrawal"])
    assert excinfo.value.code is ErrorCode.SKILL_MISSING
    assert excinfo.value.details["kind"] == "common"


def test_missing_metric_skill_dir_raises_skill_missing(tmp_path: Path) -> None:
    skills_dir = _copy_skills(tmp_path, common=True, industries=True, metrics=False)
    loader = SkillLoader(skills_dir)
    with pytest.raises(AppError) as excinfo:
        loader.resolve("extraction", "fb-nb", ["water_withdrawal"])
    assert excinfo.value.code is ErrorCode.SKILL_MISSING
    assert excinfo.value.details["kind"] == "metric"


def test_missing_skills_dir_raises_skill_missing(tmp_path: Path) -> None:
    loader = SkillLoader(tmp_path / "does-not-exist")
    with pytest.raises(AppError) as excinfo:
        loader.resolve("extraction", "fb-nb", ["water_withdrawal"])
    assert excinfo.value.code is ErrorCode.SKILL_MISSING


def test_missing_metric_dependency_raises_skill_missing(tmp_path: Path) -> None:
    """metric Skill 声明的 depends_on 不存在时必须报 SKILL_MISSING，不能静默继续。"""
    skills_dir = _copy_skills(tmp_path, common=True, industries=True, metrics=True)
    metric_file = skills_dir / "metrics" / "water_withdrawal" / "1.0.0.md"
    text = metric_file.read_text(encoding="utf-8")
    metric_file.write_text(
        text.replace(
            "depends_on: [common.evidence, common.normalization]",
            "depends_on: [common.evidence, common.missing_rules]",
        ),
        encoding="utf-8",
    )
    loader = SkillLoader(skills_dir)
    with pytest.raises(AppError) as excinfo:
        loader.resolve("extraction", "fb-nb", ["water_withdrawal"])
    assert excinfo.value.code is ErrorCode.SKILL_MISSING
    assert "common.missing_rules" in excinfo.value.details["missing"]


# ---------------------------------------------------------------------------
# front matter 解析
# ---------------------------------------------------------------------------


def test_parse_front_matter_types() -> None:
    content = """---
id: common.demo
version: 1.0.0
kind: common
applies_to: []
depends_on: [common.evidence, common.normalization]
priority: 42
token_estimate: 123
enabled: true
disabled: false
note: "含,逗号的字符串"
empty: null
---

# 标题

正文内容。
"""
    meta, body = parse_front_matter(content)
    assert meta["id"] == "common.demo"
    assert meta["version"] == "1.0.0"
    assert meta["depends_on"] == ["common.evidence", "common.normalization"]
    assert meta["applies_to"] == []
    assert meta["priority"] == 42
    assert meta["token_estimate"] == 123
    assert meta["enabled"] is True
    assert meta["disabled"] is False
    assert meta["note"] == "含,逗号的字符串"
    assert meta["empty"] is None
    assert body == "# 标题\n\n正文内容。"


def test_parse_front_matter_requires_delimiters() -> None:
    with pytest.raises(AppError) as excinfo:
        parse_front_matter("# 没有 front matter\n")
    assert excinfo.value.code is ErrorCode.SCHEMA_INVALID
