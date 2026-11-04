# 04 Summary 分析 Agent

## 工作机制

接收用户问题和权限上下文，先解析实体/年份/指标，动态发现工具，根据中间结果决定追加查询、比较、计算或外部搜索。每轮最多 12 次工具调用、总时限 120 秒；工具结果以带来源的结构化对象回传。Agent 不直接访问未审核候选，也不能自行计算公式或等级。

## 工具契约

统一 `ToolSpec(name, version, input_schema, output_schema, permission, timeout_s, handler)`；调用结果 `ToolResult(status: OK|ERROR|UNAVAILABLE_FORMULA, data, error_code, provenance, call_id)`。

| 工具 | 输入 | 输出 |
|---|---|---|
| `get_company_metric_data` | company_id, metric_id, start_year, end_year, boundary_filter? | `FactView[]` |
| `get_multiple_companies_data` | company_ids[], metric_id, year | 各公司 `FactView[]` 与缺失列表 |
| `get_industry_metric_data` | industry_code, metric_id, year, boundary_filter? | 事实列表、样本量与行业成员版本 |
| `compare_metric_series` | fact_ids[], comparison_type | 口径检查与可比数据；不计算未经注册公式 |
| `calculate_metric` | formula_id, formula_version?, input_fact_ids[], parameters | 结果、单位、输入与公式版本；无实现返回 `UNAVAILABLE_FORMULA` |
| `search_external_context` | query, region?, date_range? | URL、标题、摘要、检索时间、来源类别 |

外部搜索优先 Tavily 适配器，密钥未配置返回 `TOOL_UNAVAILABLE`。搜索材料写独立 `external_sources`，不可进入 `official_facts`。工具输出上限与超时由注册器统一控制；数据库工具使用只读凭据和 tenant 过滤。模型 tool calling 仅传已授权工具 schema，Python 执行真实函数。

## Prompt

```text
你是 ESG 水资源分析员。先确认问题需要的实体、指标、期间与比较范围，按需调用已注册工具。
企业披露数据只能引用 FactView；计算只能引用 calculate_metric 的成功结果；外部资料单独标注来源。
缺公式时明确说明无法计算，不能自行编造公式、评分或等级。数据缺失或口径不一致时给出局限。
报告给出数据来源、计算过程、绩效观察、评价依据、针对性建议和局限；每个数量结论引用 fact_id/calc_id。
```

## 报告输出 `AnalysisReport v1`

`question`、`scope`、`facts[]`、`calculations[]`、`comparisons[]`、`external_sources[]`、`assessment`、`recommendations[]`、`limitations[]`、`unavailable_requests[]`、`tool_call_ids[]`。未配置评价 Skill 时 `assessment.grade=null`，只作有证据的描述性观察。用户提问“取水强度同行水平”时先查取水量及所需分母，确认公式已注册和边界可比，再查同行并调用计算工具；工具不存在即在 `unavailable_requests` 说明。
