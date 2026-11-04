"""把阶段一归类结果与标准答案逐格对比，只输出到 stdout（不生成任何报告文件）。

用法：
    PYTHONPATH=. .venv/bin/python scripts/compare_with_gold.py
"""

from __future__ import annotations

import csv
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "tests" / "fixtures" / "gold_stage1.json"
RESULT_CSV = ROOT / "outputs" / "阶段一_归类结果_汇总.csv"
NUM_RE = re.compile(r"^-?[\d,]+(\.\d+)?%?$")


def to_number(text: str) -> Decimal | None:
    text = (text or "").strip().replace(",", "").rstrip("%")
    if not text or text.upper() == "N/A":
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def compare_cell(gold: str, got: str) -> str:
    gold = (gold or "").strip()
    got = (got or "").strip()
    if gold == got:
        return "一致"
    gold_num, got_num = to_number(gold), to_number(got)
    if gold_num is not None and got_num is not None:
        if gold_num == got_num:
            return "数值一致(格式不同)"
        # 舍入差容差：相对 1e-5
        if abs(gold_num - got_num) <= max(abs(gold_num) * Decimal("1e-5"), Decimal("1e-9")):
            return "数值一致(舍入差)"
    return "差异"


def main() -> int:
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    columns = gold["columns"]
    with RESULT_CSV.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    got_by_code = {row["股票代码"].split(".")[0]: row for row in rows}

    lines = ["# 阶段一结果 vs 标准答案：逐格对比", ""]
    lines.append(f"- 标准答案：`{GOLD.relative_to(ROOT)}`（当前 00291 / 00168 / 02460 三行）")
    lines.append(f"- 本次输出：`{RESULT_CSV.relative_to(ROOT)}`")
    lines.append("")
    lines.append(
        "> 说明：本脚本只打印对比结果，不生成 md；过程核验报告已按项目要求清理。"
        "校准规则与差异原因见 `docs/09` §11。"
    )
    lines.append("")

    total = matched = 0
    diffs: list[tuple[str, str, str, str]] = []
    for gold_row in gold["rows"]:
        code = gold_row["股票代码"]
        got_row = got_by_code.get(code)
        lines.append(f"## {code} {gold_row['公司名称']}")
        lines.append("")
        lines.append("| 列 | 标准答案 | 本次输出 | 结论 |")
        lines.append("|---|---|---|---|")
        for col in columns:
            total += 1
            gold_val = gold_row.get(col, "")
            got_val = (got_row or {}).get(col, "")
            verdict = compare_cell(gold_val, got_val)
            if verdict.startswith("一致") or verdict.startswith("数值一致"):
                matched += 1
            else:
                diffs.append((code, col, gold_val, got_val))
            lines.append(f"| {col} | {gold_val} | {got_val} | {verdict} |")
        lines.append("")

    lines.append("## 汇总")
    lines.append("")
    lines.append(f"- 单元格总数：{total}；一致：{matched}；差异：{len(diffs)}；一致率：{matched / total:.1%}")
    if diffs:
        lines.append("")
        lines.append("### 差异清单")
        lines.append("")
        lines.append("| 公司 | 列 | 标准答案 | 本次输出 | 说明 |")
        lines.append("|---|---|---|---|---|")
        for code, col, gold_val, got_val in diffs:
            note = gold.get("notes", {}).get(col, "")
            lines.append(f"| {code} | {col} | {gold_val} | {got_val} | {note} |")
    lines.append("")

    print("\n".join(lines))
    return 0 if not diffs else 2


if __name__ == "__main__":
    sys.exit(main())
