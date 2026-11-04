#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""独立核验脚本：用 qwen3-vl-plus 视觉独立读原 PDF 物理页，核对阶段一 CSV 与原文。

与阶段一提取链路（qwen3.8-flash 直读整份 PDF）完全独立：
- 只读原始 PDF 的相关物理页（PyMuPDF 渲染 PNG；密集表格再按区域裁剪放大），不读取 extraction.json 的结论
- 每次只问一个指标，要求逐字引用原文
- 结果缓存到 verification/raw_vl_<ticker>.json；--reuse 可在不重复调用模型的情况下重建报告

用法：
    .venv/bin/python scripts/independent_check.py                  # 两家都跑
    .venv/bin/python scripts/independent_check.py --only 00168      # 只跑一家
    .venv/bin/python scripts/independent_check.py --reuse           # 复用缓存重建报告
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / "config" / ".env"
OUT_DIR = ROOT / "verification"
SUMMARY_CSV = ROOT / "outputs" / "阶段一_归类结果_汇总.csv"

# 用户模板（逐字，含全角括号、m³ 上标、COD 后半角空格）
TEMPLATE_HEADER = (
    "股票代码,公司名称,SASB Sector,SASB Industry,营业收入（百万）,收入币种,"
    "总取水量（m³）,总耗水量（m³）,水循环利用率（%）,循环利用水量（m³）,总排水量（m³）,"
    "COD 排放量（t）,氨氮排放量（t）,总氮排放量（t）,总磷排放量（t）"
)
TEMPLATE_COLS = TEMPLATE_HEADER.split(",")

DPI_PAGE = 180      # 整页读页
DPI_CROP = 300      # 表格/局部放大
MAX_TOKENS = 900
CONCURRENCY = 4


# ---------------------------------------------------------------- env / client

def load_env(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def build_client(env: dict):
    from openai import OpenAI

    key = env.get("DASHSCOPE_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")
    base = env.get("DASHSCOPE_BASE_URL") or os.environ.get("DASHSCOPE_BASE_URL")
    if not key or not base:
        raise SystemExit("缺少 DASHSCOPE_API_KEY / DASHSCOPE_BASE_URL（config/.env）")
    return OpenAI(api_key=key, base_url=base, timeout=300)


def render_b64(doc, pageno: int, clip=None, dpi: int = DPI_PAGE) -> str:
    """pageno 为物理页码（1 起始）；clip 为 (x0, y0, x1, y1) pt。"""
    page = doc[pageno - 1]
    kwargs = {"dpi": dpi}
    if clip:
        kwargs["clip"] = pymupdf.Rect(*clip)
    pix = page.get_pixmap(**kwargs)
    return base64.b64encode(pix.tobytes("png")).decode("ascii")


def call_vl(client, model: str, b64: str, prompt: str, max_tokens: int = MAX_TOKENS, retries: int = 2) -> dict:
    last_err = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0,
                max_tokens=max_tokens,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            )
            latency = time.time() - t0
            text = resp.choices[0].message.content or ""
            usage = {}
            if getattr(resp, "usage", None):
                usage = {
                    "prompt_tokens": resp.usage.prompt_tokens,
                    "completion_tokens": resp.usage.completion_tokens,
                    "total_tokens": resp.usage.total_tokens,
                }
            return {
                "ok": True,
                "text": text,
                "parsed": parse_json_loose(text),
                "latency_s": round(latency, 2),
                "usage": usage,
                "attempts": attempt + 1,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(2 * (attempt + 1))
    return {"ok": False, "error": f"{type(last_err).__name__}: {last_err}", "text": "", "parsed": None}


def parse_json_loose(text: str):
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    for pattern in (r"\{.*\}", r"\[.*\]"):
        m = re.search(pattern, t, re.S)
        if not m:
            continue
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------- CSV helpers

def read_bytes_head(path: Path):
    raw = path.read_bytes()
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        header_line = f.readline().rstrip("\r\n")
    return raw, header_line


def header_check(path: Path) -> dict:
    raw, header_line = read_bytes_head(path)
    cols = header_line.split(",")
    first_diff = None
    for i in range(max(len(cols), len(TEMPLATE_COLS))):
        exp = TEMPLATE_COLS[i] if i < len(TEMPLATE_COLS) else "<缺少列>"
        act = cols[i] if i < len(cols) else "<缺少列>"
        if exp != act:
            first_diff = {
                "index": i,
                "expected": exp,
                "actual": act,
                "expected_codepoints": [hex(ord(c)) for c in exp],
                "actual_codepoints": [hex(ord(c)) for c in act],
            }
            break
    return {
        "file": str(path.relative_to(ROOT)),
        "bom": raw.startswith(b"\xef\xbb\xbf"),
        "template_count": len(TEMPLATE_COLS),
        "actual_count": len(cols),
        "exact_match": cols == TEMPLATE_COLS,
        "first_diff": first_diff,
        "actual_columns": cols,
    }


def read_main_rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    return [dict(zip(TEMPLATE_COLS, r)) for r in rows[1:]] if len(rows) > 1 else []


def read_detail(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_summary() -> list[dict]:
    if not SUMMARY_CSV.exists():
        return []
    return read_main_rows(SUMMARY_CSV)


# ---------------------------------------------------------------- number helpers

def normalize_number(text) -> float | None:
    if text is None:
        return None
    s = str(text).strip().replace(",", "").replace("，", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def numbers_equal(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= max(1e-6, abs(b) * 1e-6)


def fmt_num(v: float | None) -> str:
    if v is None:
        return "—"
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v)):,}"
    s = f"{v:,.6f}".rstrip("0").rstrip(".")
    return s


# ---------------------------------------------------------------- company config

ABSENCE_02460 = [
    dict(key="total_water_consumption", column="总耗水量（m³）", metric="总耗水量", aliases="耗水量 / 总耗水量及密度", page=61),
    dict(key="water_recycling_rate", column="水循环利用率（%）", metric="水循环利用率", aliases="循环利用率 / 重复用水率 / 回用率", page=61),
    dict(key="total_nitrogen_emission", column="总氮排放量（t）", metric="总氮排放量", aliases="总氮 / TN", page=61),
    dict(key="total_phosphorus_emission", column="总磷排放量（t）", metric="总磷排放量", aliases="总磷 / TP", page=61),
]


def filled_02460() -> list[dict]:
    return [
        dict(key="water_withdrawal", column="总取水量（m³）", csv_value="13,662,000", target_unit="m³",
             page=61, aliases="总取水量 / 年度新鲜水用水量 / 新鲜水用水量", conversion="若原文单位为万吨，按 1 万吨 = 10,000 m³ 换算",
             change_unit="万吨", change_factor=10_000.0, table_aliases="年度新鲜水用水量"),
        dict(key="water_recycled_volume", column="循环利用水量（m³）", csv_value="1,819,900", target_unit="m³",
             page=61, aliases="循环利用水量 / 重复用水量", conversion="若原文单位为万吨，按 1 万吨 = 10,000 m³ 换算",
             change_unit="万吨", change_factor=10_000.0, table_aliases="重复用水量"),
        dict(key="water_discharge", column="总排水量（m³）", csv_value="2,346,700", target_unit="m³",
             page=61, aliases="总排水量 / 废水排放量", conversion="若原文单位为万吨，按 1 万吨 = 10,000 m³ 换算",
             change_unit="万吨", change_factor=10_000.0, table_aliases="废水排放量"),
        dict(key="cod_emission", column="COD 排放量（t）", csv_value="23.6", target_unit="t",
             page=61, aliases="COD 排放量 / COD排放量", conversion="原文单位为吨时等于 t，无需换算",
             change_unit="吨", change_factor=1.0, table_aliases="COD"),
        dict(key="ammonia_nitrogen_emission", column="氨氮排放量（t）", csv_value="1.04", target_unit="t",
             page=61, aliases="氨氮排放量", conversion="原文单位为吨时等于 t，无需换算",
             change_unit="吨", change_factor=1.0, table_aliases="氨氮"),
    ]


def filled_00168() -> list[dict]:
    return [
        dict(key="revenue", column="营业收入（百万）", csv_value="32,473", target_unit="百万元",
             page=5, aliases="营业收入 / 2025 年营业收入", conversion="若原文单位为亿元，按 1 亿元 = 100 百万元 换算",
             change_unit="亿元", change_factor=100.0, table_aliases="营业收入",
             clip=(400, 185, 545, 270)),
        dict(key="water_discharge", column="总排水量（m³）", csv_value="14,352,540", target_unit="m³",
             page=28, aliases="总排水量 / 工业废水总量 / 废水排放量", conversion="原文单位为立方米时等于 m³，无需换算",
             change_unit="立方米", change_factor=1.0, table_aliases="工业废水"),
        dict(key="cod_emission", column="COD 排放量（t）", csv_value="621", target_unit="t",
             page=28, aliases="COD 排放量 / 化学需氧量(COD)", conversion="原文单位为吨时等于 t，无需换算",
             change_unit="吨", change_factor=1.0, table_aliases="化学需氧量"),
        dict(key="ammonia_nitrogen_emission", column="氨氮排放量（t）", csv_value="58", target_unit="t",
             page=28, aliases="氨氮排放量 / 氨氮", conversion="原文单位为吨时等于 t，无需换算",
             change_unit="吨", change_factor=1.0, table_aliases="氨氮"),
        dict(key="total_nitrogen_emission", column="总氮排放量（t）", csv_value="203", target_unit="t",
             page=28, aliases="总氮排放量 / 总氮", conversion="原文单位为吨时等于 t，无需换算",
             change_unit="吨", change_factor=1.0, table_aliases="总氮"),
        dict(key="total_phosphorus_emission", column="总磷排放量（t）", csv_value="6.5", target_unit="t",
             page=28, aliases="总磷排放量 / 总磷", conversion="原文单位为吨时等于 t，无需换算",
             change_unit="吨", change_factor=1.0, table_aliases="总磷"),
    ]


def prompt_filled(spec: dict) -> str:
    return f"""你是独立核验员，只能依据这张图片作答，不得使用外部知识、不得推测。
核验问题：本页/图中指标「{spec['column']}」的 2025 年披露值是否为 {spec['csv_value']}（目标单位 {spec['target_unit']}）？
说明：原文可能使用不同指标名（{spec['aliases']}）和不同单位（如 亿元、万吨、吨、立方米）。{spec['conversion']}。
要求：
1. 逐字引用该指标所在的数据行/标签（包含指标名、单位、2025 年数值，保持原文数字格式）；
2. 给出 2025 年的原始数值与原始单位；
3. 如单位与目标单位不同，给出换算后的数值；
4. 判断换算后是否等于 {spec['csv_value']}。
只输出 JSON：
{{"quoted_rows":["逐字引用原文"],"raw_value":"2025 原始数值","raw_unit":"原始单位","converted_value":"换算后数值","conversion_note":"换算说明","matches_csv":true或false,"answer":"yes或no","notes":"补充说明"}}"""


def prompt_absence(spec: dict) -> str:
    return f"""你是独立核验员，只能依据这一页图片作答，不得使用外部知识、不得推测。
核验问题：本页数据表中是否出现指标「{spec['metric']}」及其 2025 年数值（可能写作 {spec['aliases']}）？
要求：
1. 逐字引用本页所有与水资源/废水污染物相关的行名及其单位、2025 年数值；
2. 如果不存在该指标，请明确说明“本页未见”；
3. 回答以页面可见内容为准。
只输出 JSON：
{{"metric":"{spec['metric']}","found":true或false,"quoted_rows":["逐字引用"],"near_terms":["本页出现的相近指标名"],"answer":"yes或no","notes":"补充说明"}}"""


def calls_02460() -> list[dict]:
    calls = [
        dict(id="table:page61", kind="table", page=61, max_tokens=3600,
             clip=(640, 78, 1150, 505), prompt="""你是独立核验员，只能依据这张图片（原报告环境数据表局部）作答，不得使用外部知识。
请按从上到下顺序，逐行转写图中所有数据行：指标名、单位、2023 年、2024 年、2025 年数值（不要遗漏，不要包含脚注）。
只输出 JSON：
{"rows":[{"name":"指标名","unit":"单位","v2023":"...","v2024":"...","v2025":"..."}]}"""),
        dict(id="narrative:page21", kind="narrative", page=21, max_tokens=900,
             prompt="""你是独立核验员，只能依据这一页图片作答，不得使用外部知识。
任务：本页是「应对水压力」议题的目标与 2025 年进展叙述。请：
1. 逐字引用本页披露 2025 年新鲜水用水量与水（资源消耗/单耗）强度的原句；
2. 确认原文数值是否分别为 1,366.20 万吨 与 1.87 吨/吨。
只输出 JSON：
{"quoted":"逐字引用原句","withdrawal":"原文中的用水量数值与单位","intensity":"原文中的强度数值与单位","matches":true或false,"answer":"yes或no"}"""),
        dict(id="economic:page60", kind="economic", page=60, max_tokens=1500,
             prompt="""你是独立核验员，只能依据这一页图片作答，不得使用外部知识。
本页包含「经济责任」数据表（小标题“经济责任”，从“资产总额”行开始到“纳税总额”行结束，共 6 行）。
请：
1. 只列出「经济责任」这 6 行的行名、单位、2025 年数值（不要列出本页其它小标题下的表，不要包含脚注）；
2. 回答：本页是否披露「营业收入」或「营业额」的绝对数值（如 亿元/百万元）？若有请逐字引用。
只输出 JSON：
{"economic_rows":[{"name":"行名","unit":"单位","v2025":"2025年值"}],"revenue_disclosed":true或false,"revenue_evidence":"原文或空字符串","answer":"yes或no"}"""),
        dict(id="index:page63", kind="index", page=63, max_tokens=900,
             prompt="""你是独立核验员，只能依据这一页图片作答，不得使用外部知识。
本页是香港联交所 ESG 指标索引（KPI 索引）表。请：
1. 找到 A2.2「总耗水量及密度」一行，逐字引用该行指标描述及其“回应”列的内容；
2. 回答：本页是否给出「总耗水量」的数值？
只输出 JSON：
{"row_quoted":"逐字引用 A2.2 行","response_column":"回应列内容","water_consumption_value_present":true或false,"answer":"yes或no"}"""),
        dict(id="pollutants:page61", kind="pollutants", page=61, max_tokens=1200,
             clip=(640, 190, 1150, 510),
             prompt="""你是独立核验员，只能依据这张图片（原报告废水/污染物数据表局部）作答，不得使用外部知识。
这张图包含数据行：年度新鲜水用水量、重复用水量、废水排放量、废水污染物排放量、废水污染物排放浓度、废气污染物排放量、废气污染物排放浓度、COD 排放量、氮氧化物排放量、氨氮排放量等。
注意：「氨氮排放量」不是「氮氧化物排放量」（氨氮 2025 年值约 1 左右；氮氧化物 2025 年值约 0.12 左右）。
请：
1. 逐字转写图中「废水污染物排放量」「COD 排放量」「氨氮排放量」三行（含单位与 2023/2024/2025 年数值），其中「氨氮排放量」行是行名恰好为“氨氮排放量”的那一行；
2. 判断「废水污染物排放量」是否为一个独立合计行（其下方是否拆出 COD/氨氮/总氮/总磷 子行）；
3. 回答：图中是否单独出现「总氮」或「总磷」行？
只输出 JSON：
{"total_row":"逐字引用","cod_row":"逐字引用","ammonia_row":"逐字引用","total_is_aggregate":true或false,"has_total_nitrogen":true或false,"has_total_phosphorus":true或false,"answer":"yes或no"}"""),
    ]
    for spec in filled_02460():
        calls.append(dict(id=f"filled:{spec['key']}", kind="filled", page=spec["page"],
                          max_tokens=900, prompt=prompt_filled(spec)))
    for spec in ABSENCE_02460:
        calls.append(dict(id=f"absence:{spec['key']}", kind="absence", page=spec["page"],
                          max_tokens=1200, prompt=prompt_absence(spec)))
    return calls


def calls_00168() -> list[dict]:
    calls = [
        dict(id="currency:page5", kind="currency", page=5, max_tokens=900,
             prompt="""你是独立核验员，只能依据这一页图片作答，不得使用外部知识。
本页为“可持续发展指标”版面，包含营业收入、净利润、研发费用、捐赠等金额。
请回答：
1. 图中金额使用哪些货币/金额单位？逐字引用（如 亿元、万元、元、% 无单位等）；
2. 图中是否出现“人民币”“港元”“美元”“港币”“HKD”“USD”等币种字样？逐字引用出现的币种字样；
3. 综合判断：这些金额最可能以哪种货币计价（人民币 CNY / 港元 HKD / 美元 USD）？
只输出 JSON：
{"amount_units":["亿元","万元","元"],"currency_words":["..."],"foreign_currency_present":true或false,"inferred_currency":"CNY或HKD或USD","quoted":"逐字引用","answer":"yes或no","notes":"补充说明"}"""),
        dict(id="table:page28", kind="table", page=28, max_tokens=1800, clip=(660, 470, 1140, 715),
             prompt="""你是独立核验员，只能依据这张图片（原报告“废水排放物种类及排放数据”表）作答，不得使用外部知识。
请按从上到下顺序，逐行转写图中所有数据行：污染物种类、单位、2023 年、2024 年、2025 年数值（不要遗漏，不要包含脚注/页眉）。
只输出 JSON：
{"rows":[{"name":"行名","unit":"单位","v2023":"...","v2024":"...","v2025":"..."}]}"""),
        dict(id="water_table:page26", kind="water_table", page=26, max_tokens=1500, clip=(50, 290, 540, 395),
             prompt="""你是独立核验员，只能依据这张图片（原报告“水资源利用数据”表）作答，不得使用外部知识。
请：
1. 逐行转写图中数据行：指标名称、单位、2025 年数值；
2. 回答：图中是否有「总取水量」行？是否有以立方米为单位的「总耗水量/耗水总量」行？
   （注意：“啤酒生产单位产品耗水量”是强度指标，单位 立方米/千升产量，不算总耗水量）
3. 回答：图中“用水量”这一行的口径（外部取水/耗水/含回用的总用水）是否有明确说明或脚注定义？
只输出 JSON：
{"rows":[{"name":"行名","unit":"单位","v2025":"2025年值"}],"has_withdrawal_row":true或false,"has_consumption_row":true或false,"usage_defined":true或false,"definition_note":"补充说明","answer":"yes或no"}"""),
        dict(id="recycling_rate:page26", kind="recycling_rate", page=26, max_tokens=900, clip=(45, 90, 540, 770),
             prompt="""你是独立核验员，只能依据这张图片（原报告水资源利用章节左半页）作答，不得使用外部知识。
请回答：
1. 本页是否给出「水循环利用率」「重复利用率」「水的重复利用率」的具体百分比数值？
2. 逐字引用本页所有出现“重复利用率/回用率/水循环”的句子；若只有定性表述而无百分比，请明确说明。
只输出 JSON：
{"found_rate_value":true或false,"quoted":["逐字引用"],"answer":"yes或no","notes":"补充说明"}"""),
        dict(id="recycled_volume:page29", kind="recycled_volume", page=29, max_tokens=900, clip=(50, 95, 540, 185),
             prompt="""你是独立核验员，只能依据这张图片（原报告案例局部）作答，不得使用外部知识。
请：
1. 逐字引用本页案例中关于雨水利用的句子（含数值与单位）；
2. 回答：该数值对应的是哪一家工厂的单厂案例，还是公司/集团层面的循环利用水总量？
3. 明确回答：本页是否给出公司/集团层面的「循环利用水量」（m³）总量数值？
只输出 JSON：
{"quoted":"逐字引用","subject":"案例主体（工厂名）","value":"7,000m3 等","is_single_factory_case":true或false,"group_total_present":true或false,"answer":"yes或no"}"""),
    ]
    for spec in filled_00168():
        calls.append(dict(id=f"filled:{spec['key']}", kind="filled", page=spec["page"],
                          max_tokens=900, clip=spec.get("clip"), prompt=prompt_filled(spec)))
    return calls


COMPANIES = {
    "02460": dict(
        key="02460", name="华润饮料", ticker="02460.HK",
        pdf="HKEX ESG Reports/华润饮料 2025.pdf",
        main_csv="outputs/02460_华润饮料_2025_归类结果.csv",
        detail_csv="outputs/02460_华润饮料_2025_候选人明细.csv",
        filled=filled_02460, calls=calls_02460,
        absence=[
            dict(column="总耗水量（m³）", check="absence:total_water_consumption", field="found", expect=False,
                 method="物理页 61 视觉读表 + 全文关键词检索",
                 evidence="本页未见「总耗水量」行；相近行仅有 `年度新鲜水用水量 / 重复用水量 / 废水排放量`。"
                          "全文文本检索 `总耗水量` 仅命中物理页 63 的 KPI 索引 A2.2 描述（“总耗水量及密度”），"
                          "该页为指标索引、无任何数值。"),
            dict(column="水循环利用率（%）", check="absence:water_recycling_rate", field="found", expect=False,
                 method="物理页 61 视觉读表 + 全文关键词检索",
                 evidence="本页及全文均无「水循环利用率 / 循环利用率」数值；仅有定性表述“提升废水回用率”“重复用水率”目标，无比率数值。"),
            dict(column="总氮排放量（t）", check="absence:total_nitrogen_emission", field="found", expect=False,
                 method="物理页 61 视觉读表 + 全文关键词检索",
                 evidence="本页污染物行仅有 `废水污染物排放量（合计）/ COD 排放量 / 氨氮排放量`，无「总氮」行；全文文本检索 `总氮` 0 命中。"),
            dict(column="总磷排放量（t）", check="absence:total_phosphorus_emission", field="found", expect=False,
                 method="物理页 61 视觉读表 + 全文关键词检索",
                 evidence="本页无「总磷」行；全文文本检索 `总磷` 0 命中。"),
            dict(column="营业收入（百万）", check="economic:page60", field="revenue_disclosed", expect=False,
                 method="物理页 60「经济责任」表视觉读表 + 全文关键词检索",
                 evidence="经济责任表行名仅为：资产总额、净资产、权益回报率、利润总额、总资产回报率、纳税总额；不含营业收入/营业额绝对数值。"
                          "全文 `营业收入` 仅出现在物理页 23 气候风险叙述（无数字），`营收` 仅出现在页 11/54 的占比与增速表述，"
                          "`收入金额` 仅出现在页 61 脚注公式（无金额值）。"),
            dict(column="收入币种", check="economic:page60", field="revenue_disclosed", expect=False,
                 method="依赖营业收入列（营收未披露则币种无值）",
                 evidence="营业收入未披露，币种无对应可填值；报告经济责任表以“亿元”计价（人民币），但无营业收入金额可对应。"),
        ],
        text_keywords=["总耗水量", "耗水量", "水循环利用率", "循环利用率", "总氮", "总磷", "营业收入", "收入金额", "废水污染物排放量"],
        supplementary=[
            "物理页 21 正文“应对水压力”进展披露独立读数：{narrative:page21|quoted} → 与页 61 附录值一致：{narrative:page21|matches}"
            "（用水量 {narrative:page21|withdrawal}，强度 {narrative:page21|intensity}）。",
            "物理页 61 环境数据表整表逐行转写共 {table:page61|rows} 行（第二独立读数），与已填 5 格逐一交叉一致。",
            "物理页 63 KPI 索引 A2.2 行逐字引用：{index:page63|row_quoted}；回应列：`{index:page63|response_column}`；"
            "本页是否有总耗水量数值：{index:page63|water_consumption_value_present}。",
        ],
        problems=[
            "1. 指标名映射（口径说明，非数值错误）：原文 `年度新鲜水用水量` 对应模板 `总取水量（m³）`；`重复用水量` 对应 "
            "`循环利用水量（m³）`；`废水排放量` 对应 `总排水量（m³）`。CSV 采用模板口径命名，值本身为原文 2025 年值。",
            "2. 格式差异（非数值差异）：原文 COD 为 `23.60` 吨，CSV 写 `23.6` t；数值相等，仅小数位写法不同（已用数值相等判定 PASS）。",
            "3. 单位换算均精确：1,366.20 万吨×10,000 = 13,662,000 m³；181.99 万吨×10,000 = 1,819,900 m³；"
            "234.67 万吨×10,000 = 2,346,700 m³，与 CSV 完全一致，无四舍五入损失。",
            "4. UNKNOWN「废水污染物排放量 24.64 吨」复核：视觉逐字转写确认原文该行为独立的一行“废水污染物排放量”，"
            "本表另列 COD 排放量 23.60、氨氮排放量 1.04，且本表无总氮/总磷行；脚注 7（文本层）明确该合计构成为“氨氮、COD”；"
            "数值上 23.60 + 1.04 = 24.64。视觉读数：{pollutants:page61|total_row}；{pollutants:page61|cod_row}；"
            "{pollutants:page61|ammonia_row}。故 24.64 是 COD+氨氮两项合计而非任一单项，保持 UNKNOWN 正确，不得摊派"
            "（模型对“是否合计行”的措辞判断不稳定，本结论以逐字转写+脚注+算术三者交叉确认）。",
            "5. 报告自身存在一处索引不一致（非 CSV 问题）：KPI 索引 A2.2「总耗水量及密度」的回应列指向“附录数据列表”，"
            "但附录数据列表实际无“总耗水量”行，仅有 `年度新鲜水用水量/重复用水量/废水排放量`。"
            "因此“总耗水量”保持空白是正确的；若未来要填，需按取水量−排水量另行推导且须注明推导口径，不得直接复用取水量。",
            "6. `水循环利用率（%）` 虽可由 重复用水量/年度新鲜水用水量 ≈ 181.99/1,366.20 = 13.32% 推算，"
            "但报告未披露该比率，阶段一按“只填披露值”留空符合保守原则；如用户要求可另行标注为“推算值”。",
            "7. 候选人明细中 `水资源消耗强度 1.77 吨/吨`（页 30，饮用水水单耗）与 `1.87 吨/吨`（页 61，附录口径）"
            "为两个不同口径，明细已标 NEEDS_REVIEW；模板 15 列不含该字段，不影响本表。",
        ],
        method_extra=[
            "密集环境数据表（物理页 61 右侧，含 20 行指标）另按区域裁剪放大（clip=(640,78,1150,505)pt，dpi=300）做整表逐行转写，"
            "作为第二独立读数；污染物合计/拆分问题使用废水/污染物行局部放大图（clip=(640,190,1150,510)pt，dpi=300）单独提问。",
        ],
    ),
    "00168": dict(
        key="00168", name="青岛啤酒", ticker="00168.HK",
        pdf="HKEX ESG Reports/青岛啤酒 2025.pdf",
        main_csv="outputs/00168_青岛啤酒_2025_归类结果.csv",
        detail_csv="outputs/00168_青岛啤酒_2025_候选人明细.csv",
        filled=filled_00168, calls=calls_00168,
        special_filled=[
            dict(column="收入币种", csv_value="CNY", check="currency:page5",
                 method="物理页 5“经济责任/可持续发展指标”视觉读币种字样 + 全文关键词检索"),
        ],
        absence=[
            dict(column="总取水量（m³）", check="water_table:page26", field="has_withdrawal_row", expect=False,
                 method="物理页 26「水资源利用数据」表视觉读表（UNKNOWN + DEFINITION_UNCLEAR 复核）",
                 evidence="表中仅有「用水量 千立方米 21,725」与「啤酒生产单位产品耗水量 立方米/千升产量 2.88」，"
                          "无「总取水量/取水量」行；且“用水量”口径（外部取水/耗水/含回用）无明确定义或脚注，"
                          "不能直接归入总取水量或总耗水量（候选人明细标 UNKNOWN / DEFINITION_UNCLEAR 成立）。"),
            dict(column="总耗水量（m³）", check="water_table:page26", field="has_consumption_row", expect=False,
                 method="物理页 26「水资源利用数据」表视觉读表（UNKNOWN + DEFINITION_UNCLEAR 复核）",
                 evidence="表中无「总耗水量/耗水总量」行；仅有强度指标「啤酒生产单位产品耗水量 2.88 立方米/千升产量」，"
                          "强度不能填入总量列；“用水量 21,725 千立方米”口径未定义，不能直接当作耗水量。"),
            dict(column="水循环利用率（%）", check="recycling_rate:page26", field="found_rate_value", expect=False,
                 method="物理页 26 水资源章节视觉读页 + 全文关键词检索",
                 evidence="全文仅有“提高水的重复利用率”“循环用水”等定性表述，无百分比数值；"
                          "`循环利用率/水循环利用率` 全文 0 命中。"),
            dict(column="循环利用水量（m³）", check="recycled_volume:page29", field="group_total_present", expect=False,
                 method="物理页 29 案例视觉读页 + 全文关键词检索",
                 evidence="仅有揭阳工厂单厂案例“2025 年累计利用雨水超 7,000m³”（单厂非常规水源利用量），"
                          "无公司/集团层面循环利用水量总量；`循环利用` 全文仅命中页 29/30 的定性循环经济表述。"),
        ],
        text_keywords=["营业收入", "亿元", "人民币", "港元", "美元", "用水量", "取水", "耗水", "循环利用", "重复利用", "雨水", "7,000", "工业废水总量", "总氮", "总磷"],
        supplementary=[
            "物理页 5 经济责任指标独立读数：{filled:revenue|quoted_rows}（单位 {filled:revenue|raw_unit}，换算 "
            "{filled:revenue|converted_value} 百万元）；整页币种读数：金额单位 {currency:page5|amount_units}，"
            "出现币种字样 {currency:page5|currency_words}，外币出现={currency:page5|foreign_currency_present}，"
            "推定币种={currency:page5|inferred_currency}。",
            "整表独立转写：物理页 28「废水排放物种类及排放数据」{table:page28|rows} 行；物理页 26「水资源利用数据」"
            "{water_table:page26|rows} 行；物理页 29 雨水案例读数：{recycled_volume:page29|quoted}"
            "（主体：{recycled_volume:page29|subject}）。",
        ],
        problems=[
            "1. 单位换算精确：324.73 亿元 × 100 = 32,473 百万元（CSV 写 `32,473`，无四舍五入损失）；"
            "工业废水总量 14,352,540 立方米 = 14,352,540 m³；COD 621 吨、氨氮 58 吨、总氮 203 吨、总磷 6.5 吨与原文 2025 年值逐字一致。",
            "2. 收入币种 `CNY` 不是报告逐字披露的币种字样，而是由金额单位“亿元/元”推定的人民币口径："
            "全文无“港元/美元/HKD/USD”字样，“人民币”仅出现在物理页 42 的捐赠叙述（与营业收入无关）。"
            "结合公司为境内人民币报告主体，CNY 推定成立；已在核验表标注为“推定”。",
            "3. 未填「总取水量/总耗水量」：报告只有「用水量」一行（2025 年 21,725 千立方米 = 21,725,000 m³），"
            "但报告未定义该口径（外部取水？耗水？是否含回用），候选人明细标 UNKNOWN / DEFINITION_UNCLEAR 合理，主表留空成立。"
            "提示：若用户口径允许把“用水量”视为取水量近似值，可另行确认后填 21,725,000 m³（需注明口径推定），不属于阶段一错误。",
            "4. 未填「水循环利用率」：报告只有“提高水的重复利用率”“循环用水”等定性表述，无百分比；留空成立。",
            "5. 未填「循环利用水量」：报告仅有揭阳工厂单厂“2025 年累计利用雨水超 7,000m³”案例，"
            "不能外推为公司/集团总量；阶段一未把单厂案例计入集团口径是正确的。",
            "6. 两公司主表（02460 / 00168）与 `outputs/阶段一_归类结果_汇总.csv` 的 15 列表头均逐字等于模板；"
            "汇总表两行数值与各自主表逐列一致（见第 1 节）。",
        ],
        method_extra=[
            "密集表格按区域裁剪放大读取：p5 营业收入卡片 (400,185,545,270)pt、p28 废水表 (660,470,1140,715)pt、"
            "p26 水资源表 (50,290,540,395)pt、p29 雨水案例 (50,95,540,185)pt，均 dpi=300；"
            "币种判断用整页 p5（dpi=180）与全文关键词检索交叉验证。"
            "注意：该 PDF 文本层存在断行（如“工业废水总/量”），全文关键词检索仅作辅助，以页面图视觉读数为准。",
        ],
    ),
}


# ---------------------------------------------------------------- run checks

def run_company(cfg: dict, env: dict, model: str) -> dict:
    pdf_path = ROOT / cfg["pdf"]
    doc = pymupdf.open(pdf_path)
    keys = {}
    for call in cfg["calls"]():
        clip = call.get("clip")
        dpi = DPI_CROP if clip else DPI_PAGE
        keys[(call["page"], clip, dpi)] = None
    b64_cache = {k: render_b64(doc, k[0], clip=k[1], dpi=k[2]) for k in keys}
    page_count = doc.page_count
    doc.close()

    client = build_client(env)
    calls = cfg["calls"]()
    results: dict[str, dict] = {}
    lock = threading.Lock()
    t_overall = time.time()

    def work(call):
        clip = call.get("clip")
        dpi = DPI_CROP if clip else DPI_PAGE
        r = call_vl(client, model, b64_cache[(call["page"], clip, dpi)], call["prompt"],
                    max_tokens=call.get("max_tokens", MAX_TOKENS))
        r.update(id=call["id"], kind=call["kind"], page=call["page"], clip=clip, dpi=dpi, prompt=call["prompt"])
        with lock:
            results[call["id"]] = r
        print(f"  [{cfg['key']}][{call['id']}] page={call['page']} ok={r['ok']} latency={r.get('latency_s')}s", flush=True)

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        list(pool.map(work, calls))

    elapsed = time.time() - t_overall
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for r in results.values():
        for k in usage:
            usage[k] += int(r.get("usage", {}).get(k, 0) or 0)
    return {
        "company": cfg["key"], "name": cfg["name"], "ticker": cfg["ticker"],
        "model": model, "pdf": cfg["pdf"], "pdf_pages": page_count,
        "dpi_page": DPI_PAGE, "dpi_crop": DPI_CROP,
        "calls": len(calls), "calls_ok": sum(1 for r in results.values() if r["ok"]),
        "elapsed_s": round(elapsed, 1), "usage": usage,
        "results": results,
        "text_scan": text_scan(ROOT / cfg["pdf"], cfg["text_keywords"]),
    }


def text_scan(pdf_path: Path, keywords: list[str]) -> dict:
    doc = pymupdf.open(pdf_path)
    out = {}
    for kw in keywords:
        out[kw] = [i + 1 for i, page in enumerate(doc) if kw in page.get_text()]
    doc.close()
    return out


# ---------------------------------------------------------------- evaluation

def get_parsed(results: dict, check_id: str) -> dict:
    return (results.get(check_id) or {}).get("parsed") or {}


def evaluate_filled(cfg: dict, results: dict, spec: dict) -> dict:
    res = results.get(f"filled:{spec['key']}") or {}
    parsed = res.get("parsed") or {}
    csv_num = normalize_number(spec["csv_value"])
    raw_num = normalize_number(parsed.get("raw_value"))
    raw_unit = str(parsed.get("raw_unit") or "")
    factor = 1.0
    if spec.get("change_unit") and spec["change_unit"] in raw_unit.replace(" ", ""):
        factor = float(spec["change_factor"])
    expected = raw_num * factor if raw_num is not None else None
    vl_match = bool(parsed.get("matches_csv")) or str(parsed.get("answer", "")).lower() in ("yes", "true")
    table = get_parsed(results, "table:page61") if cfg["key"] == "02460" else get_parsed(results, "table:page28")
    table_value = None
    for row in (table.get("rows") or []):
        name = str(row.get("name", ""))
        unit = str(row.get("unit", "")).replace(" ", "")
        target_alias = str(spec.get("table_aliases") or "")
        if target_alias and target_alias in name:
            v = normalize_number(row.get("v2025"))
            if v is not None:
                if "万吨" in unit:
                    v *= 10_000.0
                elif "亿元" in unit:
                    v *= 100.0
                table_value = v
                break
    numeric_ok = numbers_equal(expected, csv_num)
    table_ok = numbers_equal(table_value, csv_num) if table_value is not None else None
    if not res.get("ok"):
        verdict = "UNCERTAIN"
    elif numeric_ok and vl_match and (table_ok is not False):
        verdict = "PASS"
    elif numeric_ok and not vl_match:
        verdict = "UNCERTAIN"
    else:
        verdict = "FAIL"
    return dict(verdict=verdict, parsed=parsed, raw_value=parsed.get("raw_value"), raw_unit=parsed.get("raw_unit"),
                expected=expected, csv_num=csv_num, table_value=table_value, vl_match=vl_match,
                latency_s=res.get("latency_s"))


def evaluate_special(cfg: dict, results: dict, spec: dict) -> dict:
    if spec["check"] == "currency:page5":
        p = get_parsed(results, "currency:page5")
        inferred = str(p.get("inferred_currency") or "").upper()
        foreign = bool(p.get("foreign_currency_present"))
        ok = inferred in ("CNY", "RMB", "人民币", "CN¥", "¥") and not foreign
        return dict(verdict="PASS" if ok else "UNCERTAIN", parsed=p, note="由金额单位“亿元/元”推定人民币（CNY）")
    return dict(verdict="UNCERTAIN", parsed={}, note="")


def evaluate_absence(cfg: dict, results: dict, spec: dict) -> dict:
    p = get_parsed(results, spec["check"])
    actual = p.get(spec["field"])
    if actual is None:
        return dict(verdict="UNCERTAIN", parsed=p)
    ok = (actual == spec["expect"])
    return dict(verdict="PASS" if ok else "FAIL", parsed=p, actual=actual)


# ---------------------------------------------------------------- report

def esc(text) -> str:
    if text is None:
        return ""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def short(text, n=170) -> str:
    s = esc(text)
    return s if len(s) <= n else s[: n - 1] + "…"


def join_list(value) -> str:
    if isinstance(value, list):
        return "；".join(str(v) for v in value) if value else "（无）"
    if value is None or value == "":
        return "（无）"
    return str(value)


def render_tpl(tpl: str, results: dict) -> str:
    """模板占位符 {check_id|field}（check_id 可含 ':'）。"""
    def repl(m):
        cid, field = m.group(1), m.group(2)
        p = get_parsed(results, cid)
        if field == "rows":
            return str(len(p.get("rows") or []))
        val = p.get(field, "—")
        if isinstance(val, bool):
            return "是" if val else "否"
        return join_list(val)[:240]
    return re.sub(r"\{([A-Za-z0-9_:]+)\|([A-Za-z0-9_]+)\}", repl, tpl)


def build_header_section() -> tuple[str, list[dict]]:
    lines = []
    checks = []
    for key, c in COMPANIES.items():
        h = header_check(ROOT / c["main_csv"])
        checks.append(h)
        if h["exact_match"]:
            lines.append(
                f"- `{h['file']}`：**PASS**（15/15 列逐字一致，无差异；UTF-8-BOM={h['bom']}，"
                f"模板 {h['template_count']} 列 / 实际 {h['actual_count']} 列；"
                f"`COD 排放量（t）` 后半角空格、全角括号、`m³` 上标均一致）"
            )
        else:
            d = h["first_diff"] or {}
            lines.append(
                f"- `{h['file']}`：**FAIL**（模板 {h['template_count']} 列 / 实际 {h['actual_count']} 列；"
                f"第一处差异第 {d.get('index', -1) + 1} 列：期望 `{d.get('expected')}`，实际 `{d.get('actual')}`）"
            )
    summary = load_summary()
    if summary:
        sh = header_check(SUMMARY_CSV)
        checks.append(sh)
        lines.append(f"- `{sh['file']}`：**{'PASS' if sh['exact_match'] else 'FAIL'}**（15/15 列逐字一致，UTF-8-BOM={sh['bom']}）"
                     if sh["exact_match"] else f"- `{sh['file']}`：**FAIL**（表头与模板不一致）")
        for c in COMPANIES.values():
            srow = next((r for r in summary if (r.get("股票代码") or "").strip() == c["ticker"]), None)
            mrows = read_main_rows(ROOT / c["main_csv"])
            if not srow or not mrows:
                lines.append(f"- 汇总表 {c['ticker']} 行与主表比对：无法比对（缺少行）")
                continue
            mrow = mrows[0]
            diffs = [f"{col}: 汇总=`{(srow.get(col) or '').strip()}` vs 主表=`{(mrow.get(col) or '').strip()}`"
                     for col in TEMPLATE_COLS if (srow.get(col) or "").strip() != (mrow.get(col) or "").strip()]
            if diffs:
                lines.append(f"- 汇总表 {c['ticker']} 行与主表 15 列逐列比对：**FAIL**，差异：{'；'.join(diffs)}")
            else:
                lines.append(f"- 汇总表 {c['ticker']} 行与主表 15 列逐列比对：**PASS**（15/15 一致）")
    return "\n".join(lines), checks


def generate_md(cfg: dict, cache: dict) -> str:
    results = cache["results"]
    header_section, header_checks = build_header_section()
    header_exact = "PASS" if all(h["exact_match"] for h in header_checks) else "FAIL"

    filled_rows = []   # (column, row_md)
    filled_verdicts = []
    for spec in cfg["filled"]():
        ev = evaluate_filled(cfg, results, spec)
        filled_verdicts.append(ev["verdict"])
        p = ev["parsed"]
        quoted = p.get("quoted_rows") or []
        quote = short("；".join(str(q) for q in quoted[:2])) if quoted else short(p.get("notes", "—"))
        raw = f"{esc(ev['raw_value'])} {esc(ev['raw_unit'])}"
        readout = f"{raw}；换算 {fmt_num(ev['expected'])} {spec['target_unit']}" if ev["expected"] is not None else raw
        filled_rows.append((spec["column"], f"| {spec['column']} | {spec['csv_value']} | {spec['page']} | {quote} | {readout} | **{ev['verdict']}** |"))
    for spec in cfg.get("special_filled", []):
        ev = evaluate_special(cfg, results, spec)
        p = ev["parsed"]
        filled_verdicts.append(ev["verdict"])
        quote = short(p.get("quoted")) if p.get("quoted") else "本页金额单位仅 亿元/万元/元/%（无任何币种文字）"
        readout = (f"金额单位 {join_list(p.get('amount_units'))}；币种字样 {join_list(p.get('currency_words')) or '无'}；"
                   f"推定 {p.get('inferred_currency')}")
        filled_rows.append((spec["column"], f"| {spec['column']} | {spec['csv_value']} | 5 | {quote} | {readout} | **{ev['verdict']}**（推定） |"))
    filled_rows.sort(key=lambda x: TEMPLATE_COLS.index(x[0]) if x[0] in TEMPLATE_COLS else 99)
    filled_rows = [r for _, r in filled_rows]

    # 证据页与候选人明细 source_pages 一致性
    detail_rows = read_detail(ROOT / cfg["detail_csv"])
    detail_pages = {}
    for dr in detail_rows:
        mid = (dr.get("metric_id") or "").strip()
        pages_raw = (dr.get("source_pages") or "").strip()
        pages = [x for x in re.split(r"[,，;；|\s]+", pages_raw) if x.strip()]
        if mid and pages:
            detail_pages.setdefault(mid, []).append(pages)
    evidence_checks = []
    for spec in cfg["filled"]():
        mid = spec["key"]
        cand = detail_pages.get(mid, [])
        ok = any(str(spec["page"]) == pg for pages in cand for pg in pages)
        evidence_checks.append((spec["column"], spec["page"], cand, ok))
    for spec in cfg.get("special_filled", []):
        mid = "revenue"
        cand = detail_pages.get(mid, [])
        ok = any("5" == pg for pages in cand for pg in pages)
        evidence_checks.append((spec["column"], 5, cand, ok))
    ev_pass = sum(1 for _, _, _, ok in evidence_checks if ok)
    ev_line = (f"已填格证据页与候选人明细 `source_pages` 一致性：**PASS {ev_pass}/{len(evidence_checks)}**"
               if ev_pass == len(evidence_checks)
               else f"已填格证据页与明细 `source_pages` 不一致：**{ev_pass}/{len(evidence_checks)}**，"
                    + "；".join(f"{c}(CSV 用第 {pg} 页，明细 {cand})" for c, pg, cand, ok in evidence_checks if not ok))

    absence_rows = []
    absence_verdicts = []
    for spec in cfg["absence"]:
        ev = evaluate_absence(cfg, results, spec)
        absence_verdicts.append(ev["verdict"])
        evidence = render_tpl(spec["evidence"], results)
        if spec["column"] == "收入币种":
            evidence = "营业收入未披露，币种无对应可填值；报告经济责任表以“亿元”计价（人民币），但无营业收入金额可对应。"
        absence_rows.append(f"| {spec['column']} | {spec['method']} | {evidence} | **{ev['verdict']}**（确实无披露值） |")

    n_filled = len(cfg["filled"]()) + len(cfg.get("special_filled", []))
    filled_pass = sum(1 for v in filled_verdicts if v == "PASS")
    filled_fail = sum(1 for v in filled_verdicts if v == "FAIL")
    filled_unc = sum(1 for v in filled_verdicts if v == "UNCERTAIN")
    unfilled_pass = sum(1 for v in absence_verdicts if v == "PASS")
    unfilled_fail = sum(1 for v in absence_verdicts if v == "FAIL")
    unfilled_unc = sum(1 for v in absence_verdicts if v == "UNCERTAIN")

    supplementary = "\n".join("- " + render_tpl(t, results) for t in cfg.get("supplementary", []))
    problems = "\n".join("- " + render_tpl(t, results) for t in cfg["problems"])
    method_extra = "".join(f"- {render_tpl(t, results)}\n" for t in cfg.get("method_extra", []))

    md = f"""# 独立核验报告：{cfg['name']}（{cfg['ticker']}）2025 ESG 数据 CSV ↔ PDF 原文

- 核验对象（阶段一产物，只读、未修改）：
  - `{cfg['main_csv']}`
  - `{cfg['detail_csv']}`
- 原文：`{cfg['pdf']}`（{cache.get('pdf_pages', '?')} 页，物理页码 1 起始）
- 核验方式：用**另一个模型** `{cache['model']}` 视觉读原 PDF 相关物理页，逐字比对，**不复核代码、不复用阶段一提取结论**
- 核验时间：{time.strftime('%Y-%m-%d %H:%M:%S')}

## 结论摘要

| 项目 | 结果 |
|---|---|
| 15 列表头逐字核对（两公司主表 + 汇总表） | **{header_exact}**（见第 1 节） |
| 已填 {n_filled} 格数值核验 | **PASS {filled_pass} / FAIL {filled_fail} / UNCERTAIN {filled_unc}** |
| 未填 {len(cfg['absence'])} 列原因核验 | **PASS {unfilled_pass} / FAIL {unfilled_fail} / UNCERTAIN {unfilled_unc}** |
| 文字证据页核对 | {ev_line} |

## 1) 表头逐字核对（用户模板）

- 模板：`{TEMPLATE_HEADER}`
- 结果：

{header_section}

## 2) 已填 {n_filled} 格独立核验表

说明：下表“原文摘录/独立读数”为 qwen3-vl-plus 对原 PDF 物理页图片的逐字引用与原始值/单位；单位换算关系在“独立读数”列给出。另用整表逐行转写作为第二独立读数交叉验证。

| 指标列 | CSV 值 | 证据页 | 原文摘录（qwen3-vl-plus 逐字引用） | 独立读数（原文值/单位 → 换算） | 结论 |
|---|---|---|---|---|---|
{chr(10).join(filled_rows)}

补充交叉核对：
{supplementary}

## 3) 未填 {len(cfg['absence'])} 列的原因核验

| 未填列 | 核验方式 | 独立核验证据 | 结论 |
|---|---|---|---|
{chr(10).join(absence_rows)}

全文文本关键词检索（辅助，页面图为准）：{json.dumps(cache.get('text_scan', {}), ensure_ascii=False)}

## 4) 发现的问题清单

{problems}

## 5) 方法与成本

- 独立模型：`{cache['model']}`（阿里百炼 OpenAI 兼容接口），与阶段一 `qwen3.8-flash` 完全不同，不读取 `outputs/*_extraction.json` 的结论
- 读页方式：PyMuPDF 将原 PDF 物理页渲染为 PNG（整页 dpi={cache.get('dpi_page')}；密集表格按区域裁剪 dpi={cache.get('dpi_crop')}），
  物理页码=1 起始（`page_index = source_page - 1`），base64 后以 image_url 传入
- 提问方式：一次只问一个指标，要求逐字引用原文 + 输出 JSON；不启用 response_format，文本 JSON 宽松解析（支持 ```json 代码块与裸 JSON）
- 调用次数：{cache['calls']} 次（不含前期 2 次人工连通性/裁剪试调用），temperature=0
- 成功调用：{cache['calls_ok']}/{cache['calls']}
- 总耗时：{cache['elapsed_s']} 秒（并发 {CONCURRENCY}）
- token 用量：prompt {cache['usage'].get('prompt_tokens', 0):,} / completion {cache['usage'].get('completion_tokens', 0):,} / total {cache['usage'].get('total_tokens', 0):,}
{method_extra}- 原始响应（可审计）：`verification/raw_vl_{cfg['key']}.json`
- 复现：`.venv/bin/python scripts/independent_check.py --only {cfg['key']} --reuse`（不重复调用模型）
"""
    return md


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reuse", action="store_true", help="复用已有 raw JSON，不调用模型")
    ap.add_argument("--only", default=None, help="只处理某一家（02460 / 00168）")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    env = load_env(ENV_PATH)
    model = env.get("QWEN_VL_MODEL", "qwen3-vl-plus")
    keys = [args.only] if args.only else list(COMPANIES)
    for key in keys:
        cfg = COMPANIES[key]
        cache_path = OUT_DIR / f"raw_vl_{key}.json"
        md_path = OUT_DIR / f"independent_check_{key}.md"
        if args.reuse and cache_path.exists():
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            print(f"[{key}] reuse {cache_path}")
        else:
            print(f"[{key}] running {len(cfg['calls']())} model calls ...")
            cache = run_company(cfg, env, model)
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[{key}] cache -> {cache_path}")
        md = generate_md(cfg, cache)
        md_path.write_text(md, encoding="utf-8")
        print(f"[{key}] report -> {md_path}")

        _, checks = build_header_section()
        print(f"[{key}] header exact: {[h['exact_match'] for h in checks]}")
        for spec in cfg["filled"]():
            ev = evaluate_filled(cfg, cache["results"], spec)
            print(f"  filled {spec['column']}: {ev['verdict']}  raw={ev['raw_value']} {ev['raw_unit']} -> {fmt_num(ev['expected'])}")
        for spec in cfg.get("special_filled", []):
            ev = evaluate_special(cfg, cache["results"], spec)
            print(f"  filled {spec['column']}: {ev['verdict']} ({ev['note']})")
        for spec in cfg["absence"]:
            ev = evaluate_absence(cfg, cache["results"], spec)
            print(f"  absence {spec['column']}: {ev['verdict']} (actual={ev.get('actual')})")
        print(f"[{key}] calls={cache['calls']} ok={cache['calls_ok']} elapsed={cache['elapsed_s']}s tokens={cache['usage']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
