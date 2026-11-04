"""PromptRegistry 契约测试（docs/05、docs/07）。

覆盖：render 成功并注入 skill_text/report_context、digest 稳定、缺变量/缺模板报错。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import AppError, ErrorCode
from app.services.prompt_registry import PromptRegistry, RenderedPrompt

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = PROJECT_ROOT / "prompts"

REPORT_CONTEXT = {
    "report_id": "rp_01",
    "company_id": "co_crbeverage",
    "company_name": "华润饮料",
    "report_year": 2025,
    "page_count": 120,
    "industry_code": "fb-nb",
    "sasb_sector": "Food & Beverage",
    "sasb_industry": "Non-Alcoholic Beverages",
}

SKILL_TEXT = "## [metric.water_withdrawal@1.0.0] 总取水量\n\n从外部水源取得的水量总和。"


@pytest.fixture(scope="module")
def registry() -> PromptRegistry:
    return PromptRegistry(PROMPTS_DIR)


def _render(registry: PromptRegistry, **overrides) -> RenderedPrompt:
    context = {
        "skill_text": SKILL_TEXT,
        "report_context": REPORT_CONTEXT,
        "metric_ids": ["water_withdrawal", "revenue"],
        "schema_version": "1",
        "prompt_version": "v1",
    }
    context.update(overrides)
    return registry.render("extraction", version="v1", **context)


def test_render_success(registry: PromptRegistry) -> None:
    prompt = _render(registry)

    assert isinstance(prompt, RenderedPrompt)
    assert prompt.name == "extraction"
    assert prompt.version == "v1"
    assert prompt.text
    assert len(prompt.digest) == 64
    int(prompt.digest, 16)

    # skill_text 与 report_context 内容都进入渲染结果
    assert "## [metric.water_withdrawal@1.0.0] 总取水量" in prompt.text
    assert "华润饮料" in prompt.text
    assert "co_crbeverage" in prompt.text
    assert "2025" in prompt.text

    # 指标范围与契约版本注入
    assert "water_withdrawal" in prompt.text
    assert "revenue" in prompt.text
    assert '"schema_version": "1"' in prompt.text
    assert "prompt_version" in prompt.text

    # 关键契约措辞存在
    assert "ExtractionBatch v1" in prompt.text
    assert '"candidates"' in prompt.text
    assert '"evidence"' in prompt.text
    assert '"coverage"' in prompt.text
    assert "物理页码" in prompt.text
    assert "只输出" in prompt.text
    assert "不要 markdown 代码块" in prompt.text

    # 不应残留未渲染的 Jinja 标记
    assert "{{" not in prompt.text
    assert "{%" not in prompt.text


def test_render_context_values_are_used(registry: PromptRegistry) -> None:
    other = dict(REPORT_CONTEXT)
    other["company_name"] = "青岛啤酒"
    other["industry_code"] = "fb-ab"
    other["page_count"] = 48
    prompt = _render(registry, report_context=other, metric_ids=["cod_emission"])
    assert "青岛啤酒" in prompt.text
    assert "fb-ab" in prompt.text
    assert "cod_emission" in prompt.text
    assert "48" in prompt.text


def test_render_missing_variable_raises_schema_invalid(registry: PromptRegistry) -> None:
    with pytest.raises(AppError) as excinfo:
        registry.render(
            "extraction",
            version="v1",
            report_context=REPORT_CONTEXT,
            metric_ids=["water_withdrawal"],
            schema_version="1",
            prompt_version="v1",
            # 缺少 skill_text
        )
    assert excinfo.value.code is ErrorCode.SCHEMA_INVALID
    assert "skill_text" in excinfo.value.details["error"]


def test_render_missing_template_raises_not_found(registry: PromptRegistry) -> None:
    with pytest.raises(AppError) as excinfo:
        registry.render("no_such_prompt", skill_text="x", report_context=REPORT_CONTEXT)
    assert excinfo.value.code is ErrorCode.NOT_FOUND

    with pytest.raises(AppError) as excinfo:
        registry.render(
            "extraction",
            version="v9",
            skill_text=SKILL_TEXT,
            report_context=REPORT_CONTEXT,
            metric_ids=[],
            schema_version="1",
            prompt_version="v9",
        )
    assert excinfo.value.code is ErrorCode.NOT_FOUND


def test_render_rejects_path_traversal(registry: PromptRegistry) -> None:
    with pytest.raises(AppError) as excinfo:
        registry.render("../config/.env", skill_text="x", report_context=REPORT_CONTEXT)
    assert excinfo.value.code is ErrorCode.NOT_FOUND


def test_digest_is_stable_and_context_sensitive(registry: PromptRegistry) -> None:
    first = _render(registry)
    second = _render(registry)
    assert first.text == second.text
    assert first.digest == second.digest

    changed_skill = _render(registry, skill_text=SKILL_TEXT + "\n补充规则。")
    assert changed_skill.digest != first.digest

    other_context = dict(REPORT_CONTEXT)
    other_context["report_year"] = 2024
    changed_context = _render(registry, report_context=other_context)
    assert changed_context.digest != first.digest

    # 新实例重新渲染仍得到同一 digest
    again = _render(PromptRegistry(PROMPTS_DIR))
    assert again.digest == first.digest


def test_render_tolerates_partial_report_context(registry: PromptRegistry) -> None:
    """CLI smoke 等调用方可能只传部分 report_context；顶层变量仍必须严格校验。"""
    prompt = registry.render(
        "extraction",
        version="v1",
        skill_text=SKILL_TEXT,
        report_context={
            "report_id": "rp_smoke",
            "company_name": "冒烟测试",
            "report_year": 2025,
            "page_count": 1,
        },
        metric_ids=["water_withdrawal"],
        schema_version="1",
        prompt_version="v1",
    )
    assert "冒烟测试" in prompt.text
    assert "未提供" in prompt.text
    assert "{{" not in prompt.text
    assert "{%" not in prompt.text
