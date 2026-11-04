"""Rebuild explicitly synthetic fixtures: python -m review_workbench.make_demo."""
import json
from pathlib import Path

from .contracts import SCHEMA_VERSION, validate_bundle

WITHDRAWAL = "Total Water Withdrawal (from all sources in m³)"
INTENSITY = "Water Intensity this year"
NITROGEN = "Total Nitrogen discharge"
RECYCLED = "Total water recycled/reused"


def build():
    descriptions = [
        ("consumption", "<table><tr><th>指标定义</th><th>2025 年集团实际值</th></tr><tr><td>Water consumption 耗水量（蒸发、产品带走等未返回水环境的水量）</td><td>200 m3</td></tr></table>", 200, 2025, "Group"),
        ("unit", "<table><tr><th>集团指标</th><th>2025</th><th>单位</th></tr><tr><td>Total water withdrawal</td><td>1,234</td><td>千立方米</td></tr></table>", 1234, 2025, "Group"),
        ("year", "集团 2024 年全年 Total water withdrawal 为 800 m3；本段未披露 2025 年数值。", 800, 2025, "Group"),
        ("scope", "2025 年 A 工厂全年取水量为 350 m3。本段仅覆盖 A 工厂，不含其他厂区。", 350, 2025, "Group"),
        ("value", "集团 2025 年 Total water withdrawal 实际值为 1,234 m3，本段未披露其他取水数值。", 999, 2025, "Group"),
    ]
    evidence, records = [], []
    for page, (name, text, value, year, scope) in enumerate(descriptions, 12):
        evidence.append({"evidence_id": "demo_e_" + name, "page_no": page, "kind": "table_cell" if text.startswith("<table>") else "text",
                         "content": text, "bbox": [40, 100, 550, 280], "tier": "high"})
        records.append({"record_uid": "demo_r_" + name, "indicator_id": "E2.1.1", "variable": WITHDRAWAL,
                        "value": value, "unit": "m3", "year": year, "breakdown": scope, "source_page": page,
                        "evidence_id": "demo_e_" + name, "provenance_source": "evidence_id", "classification_status": "ASSIGNED"})
    evidence.extend([
        {"evidence_id": "demo_e_zero", "page_no": 18, "kind": "text", "content": "2025 年集团全年废水中总氮实际排放量为 0 t；0 为实测披露值，并非缺失或未检出。", "bbox": [40, 100, 550, 180]},
        {"evidence_id": "demo_e_reuse", "page_no": 19, "kind": "text", "content": "2025 年集团内部 water recycled/reused 实际量为 5,000 m3，统计口径包含同一水流多次循环使用。"},
    ])
    records.append({"record_uid": "demo_r_zero", "indicator_id": "E2.1.3", "variable": NITROGEN, "value": 0,
                    "unit": "t", "year": 2025, "breakdown": "Group", "source_page": 18, "evidence_id": "demo_e_zero",
                    "provenance_source": "evidence_id", "classification_status": "ASSIGNED"})
    bundle = {"schema_version": SCHEMA_VERSION,
              "report_context": {"pid": "SYNTHETIC-DEMO-001", "company_name": "澄水示例集团（合成数据）", "report_year": 2025,
                                 "period_start": "2025-01-01", "period_end": "2025-12-31"},
              "catalogue": {"version": "DEMO-SUBSET-1.0-NOT-PRODUCTION", "indicators": [
                  {"indicator_id": "E2.1.1", "definition": "集团完整报告期内从外部获取的总取水量及明确披露的取水强度。耗水量须依原文定义区分，不能直接等同取水量。", "variables": [WITHDRAWAL, INTENSITY], "keywords": ["water withdrawal", "water consumption", "取水", "耗水"]},
                  {"indicator_id": "E2.1.3", "definition": "完整报告期内废水污染物实际排放总量；不接受浓度、限值或目标代替。", "variables": [NITROGEN], "keywords": ["总氮"]},
                  {"indicator_id": "E2.1.4", "definition": "内部实际回用水量；可包含多次循环，大于取水量不能直接判错。", "variables": [RECYCLED], "keywords": ["water recycled", "回用"]}]},
              "review_policy": {"policy_version": "RP-DEMO-1.0", "water_rules_version": "WR-DEMO-1.0",
                                "check_missing_key": True, "block_confidence": .7, "max_model_calls": 12, "max_output_tokens": 6000},
              "evidence": evidence, "records": records}
    specs = [("consumption", "A-IND-MISMATCH", "原文明确为耗水量，不能直接归为总取水量。"),
             ("unit", "A-UNIT-MISMATCH", "原文单位为千立方米，记录单位为 m3，需交由 S5 核对量级。"),
             ("year", "A-YEAR-MISMATCH", "证据披露 2024 年，而记录填写 2025 年。"),
             ("scope", "A-SCOPE-MIX", "证据只覆盖 A 工厂，记录却标为集团口径。"),
             ("value", "A-EVIDENCE-WEAK", "证据未支持记录中的 999，需核对原始数值。")]
    findings = [{"record_uid": "demo_r_" + name, "code": code, "severity": "BLOCK", "confidence": .95,
                 "evidence_ids": ["demo_e_" + name], "reason": reason, "proposed_action": "ESCALATE"}
                for name, code, reason in specs]
    responses = {
        "E2.1.1": {"record_verdicts": [{"record_uid": "demo_r_" + name, "verdict": "SUSPECT", "reason": reason} for name, _, reason in specs], "findings": findings},
        "E2.1.3": {"record_verdicts": [{"record_uid": "demo_r_zero", "verdict": "CONFIRMED", "reason": "证据明确披露集团全年实际总氮排放为 0 t，年度、单位和范围一致。"}], "findings": []},
        "E2.1.4": {"record_verdicts": [], "findings": [{"record_uid": None, "code": "A-MISSING-KEY", "severity": "BLOCK", "confidence": .95,
                       "evidence_ids": ["demo_e_reuse"], "reason": "证据披露回用量，但本指标没有对应归类记录，需回到 S3 核对遗漏。", "proposed_action": "MARK_MISSING"}]},
    }
    validate_bundle(bundle)
    return bundle, responses


if __name__ == "__main__":
    target = Path(__file__).parent / "examples"
    target.mkdir(exist_ok=True)
    bundle, responses = build()
    for name, data in [("demo.bundle.json", bundle), ("demo.responses.json", responses)]:
        (target / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("已生成合成演示输入及预设响应。未调用模型。")
