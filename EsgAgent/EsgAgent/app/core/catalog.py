"""项目级常量目录：阶段一归类结果表的指标顺序与列名。

列名逐字对应用户提供的阶段一归类结果表头（含全角括号），CSV 输出不做改写。
"""

from __future__ import annotations

# 阶段一归类结果表：15 列（A-O）
STAGE1_COLUMNS: list[str] = [
    "股票代码",
    "公司名称",
    "SASB Sector",
    "SASB Industry",
    "营业收入（百万）",
    "收入币种",
    "总取水量（m³）",
    "总耗水量（m³）",
    "水循环利用率（%）",
    "循环利用水量（m³）",
    "总排水量（m³）",
    "COD 排放量（t）",
    "氨氮排放量（t）",
    "总氮排放量（t）",
    "总磷排放量（t）",
]

# 每个指标列对应的 metric_id（列标题 -> metric_id）；不含公司与收入币种列。
COLUMN_METRIC_IDS: dict[str, str] = {
    "总取水量（m³）": "water_withdrawal",
    "总耗水量（m³）": "water_consumption",
    "水循环利用率（%）": "water_recycling_rate",
    "循环利用水量（m³）": "water_recycled_volume",
    "总排水量（m³）": "water_discharge",
    "COD 排放量（t）": "cod_emission",
    "氨氮排放量（t）": "ammonia_nitrogen_emission",
    "总氮排放量（t）": "total_nitrogen_emission",
    "总磷排放量（t）": "total_phosphorus_emission",
    "营业收入（百万）": "revenue",
}

# 提取时加载/要求模型覆盖的指标（阶段一全部 13 个；含不在表内的强度与定性项）
STAGE1_METRIC_IDS: list[str] = [
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

# 指标 -> 中文名（无 registry 时的兜底）
METRIC_NAMES: dict[str, str] = {
    "water_withdrawal": "总取水量",
    "water_consumption": "总耗水量",
    "water_recycling_rate": "水循环利用率",
    "water_recycled_volume": "循环利用水量",
    "water_discharge": "总排水量",
    "water_intensity": "水资源消耗强度",
    "water_policy": "水资源政策",
    "water_risk": "水风险",
    "cod_emission": "COD 排放量",
    "ammonia_nitrogen_emission": "氨氮排放量",
    "total_nitrogen_emission": "总氮排放量",
    "total_phosphorus_emission": "总磷排放量",
    "revenue": "营业收入",
}
