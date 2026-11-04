# 01 Qwen 文档理解与提取 Agent

## 官方能力与接入

模型 ID 为 `qwen3.8-flash`。部署地址由 `DASHSCOPE_BASE_URL` 配置，不推断工作空间 URL。官方 PDF 理解文档确认：北京及新加坡地域可用，OpenAI 兼容 `chat.completions` 中 `messages[].content` 使用 `{"type":"file","file":{"file_url":"..."}}`，或 `file_data` 为 `data:application/pdf;base64,...` 且提供 `filename`。单 PDF 上限 150 MB、256 页；解析首 token 最长可达 300 秒，建议流式调用。**不要通过 Responses API 传 PDF**，该路径可能返回 200 却未将文件交给模型。官方资料：[PDF 理解](https://help.aliyun.com/en/model-studio/pdf-understanding)、[模型规格](https://help.aliyun.com/en/model-studio/qwen3-8-flash)。

模型页面给出 1,000,000 token 上下文和结构化输出能力；但 PDF 页数/文件大小上限仍单独生效。调用限流、JSON Schema 参数组合、特定账号地域能力需通过部署环境冒烟测试确认，不能将模型页面规格当作服务可用性保证。PDF API 未承诺返回每条事实的可靠 bbox；首版只要求模型给出 PDF 物理页码、逐字证据及表格上下文。bbox 可空，不开发本地 PDF 文本/版面解析器。

```python
from openai import OpenAI
import os
client = OpenAI(api_key=os.environ['DASHSCOPE_API_KEY'],
  base_url=os.environ['DASHSCOPE_BASE_URL'])
stream = client.chat.completions.create(
  model='qwen3.8-flash', stream=True,
  messages=[{'role':'user','content':[
    {'type':'file','file':{'file_url': signed_pdf_url}},
    {'type':'text','text': extraction_prompt}
  ]}] )
# 聚合增量文本；验证 JSON；持久化 request_id、usage 与原始响应。
```

用户在 Web/API 上传 PDF 后，后端只做文件类型、大小、页数元数据和哈希检查（页数可由轻量元数据读取获得，不提取页面内容），将原件存入对象存储并生成可供阿里云服务读取的短时签名 URL。`file_url` 是首选传输方式；若无法提供可下载 URL，可把上传文件直接编码为 `file_data` Base64（必须提供 `filename`）。两种方式均由平台解析 PDF，后端不提取文本、不渲染页面、不做 OCR。超过 150 MB 或 256 页的 PDF 返回 `PDF_LIMIT_EXCEEDED` 并提示用户提供符合限制的报告；首版不自动拆页。页码以原件物理页码 1 起始。

## 处理流程

`ingest_pdf(path, company_id, report_year)->ReportRef`：检查文件并保存原件，不解析内容。`extract_report(report_id, task_id)->list[ExtractionCandidate]`：从对象存储取得签名 URL，加载 common、行业及相关指标 Skill，将 Skill 正文与提取 Prompt 合并为文本，和 PDF 一起发送给 `qwen3.8-flash`。平台一次读取整份 PDF 并返回候选、原文证据、表格标题/行列头/单位、年份、统计边界、归类理由和歧义。Python 只做 JSON/字段/页码范围校验及存储。证据定位由模型给出页码和原文；无法确定时标记 `NEEDS_REVIEW`。标准化由 Python 单位库在审核通过后执行，不覆盖原值。

## Prompt 模板

```text
你是 ESG 披露候选事实提取器。只使用所附 PDF 和本轮 Skill 片段。对每条候选同时填写指标归类、归类理由和备选解释。
逐页识别水资源数量、比例和政策叙述；保留表题、行列头、脚注、年份与边界。
输出符合 ExtractionBatch v1 的 JSON。每个候选必须给出逐字证据和 PDF 物理页码。
看不清、单位不明或口径不明时设置 ambiguity_flags，不猜测。
给出候选 metric_id、归类原因、可选解释和歧义状态；不进行推算，不补全缺失值。
任务参数：{report_id, skill_digest, schema_version}。
```

## 输出与错误

`ExtractionCandidate` 完整字段见 07；定性项 `raw_value` 可为 null，`raw_text` 必填。失败码：`PDF_INVALID`、`PDF_LIMIT_EXCEEDED`、`MODEL_TIMEOUT`、`MODEL_RATE_LIMIT`、`MODEL_BAD_JSON`、`EVIDENCE_NOT_LOCATED`。429/5xx 指数退避最多 3 次；坏 JSON 一次修复调用，仍失败保存原响应并转人工。幂等键为 `report_sha256 + page_range + prompt_version + skill_digest`。


## 提取与归类一体化细则

本文件规定 `ExtractionAgent` 的归类子流程；归类是提取 Agent 内的一项职责，数据与提取候选保存在同一记录。一次 PDF 理解调用可同时返回候选数值、证据和指标归类。Python 校验结构并保存候选，随后由独立审核 Agent 核实。

## 指标体系与 Skill

初始指标包括 `water_withdrawal`、`water_consumption`、`water_discharge`、`water_recycled_volume`、`water_recycling_rate`、`water_intensity`、`water_policy`、`water_risk`。定义、识别规则、排除规则、标准单位和版本存于 `metric_definitions` 与对应 Skill。新指标只需增加登记和 Skill。提取时加载 common、企业行业和本任务相关指标 Skill，不加载全部规则。

关键区别：取水是从外部水源获取；耗水是未回到可用水体的水；排水是向外排放；回用量是内部再次利用；比率不是体积。具体可执行定义以版本化 Skill 为准。报告只写“总用水量”且无口径时，`classification_status=UNKNOWN`，`metric_id=null`，不得硬归类。

## 同一次调用的输出协议

`extract_report(report_id, task_id)->list[ExtractionCandidate]`。每条候选同时包含 `raw_value`、`unit`、`data_year`、`reporting_boundary`、`evidence_ids`、`metric_id`、`metric_version`、`classification_status`、`classification_reason`、`alternatives`、`confidence` 和 `skill_versions`。统一字段及约束见 07。

```json
{"candidate_id":"ca_01","report_id":"rp_01","company_id":"co_01","report_year":2025,"data_year":2025,"raw_value":"100,000","raw_text":null,"unit":"立方米","statistical_period":"2025-01-01/2025-12-31","reporting_boundary":{"level":"GROUP","description":"集团"},"evidence_ids":["ev_01"],"ambiguity_flags":[],"metric_id":"water_withdrawal","metric_version":"1.0.0","classification_status":"CLASSIFIED","classification_reason":"表格行名为总取水量，单位为立方米，2025 年集团口径；并非排水或回用量","alternatives":[],"confidence":0.91,"skill_versions":{"water_withdrawal":"1.0.0"},"model_call_id":"mc_01","supersedes_id":null}
```

若证据不足，返回 `UNKNOWN`、`NEEDS_REVIEW` 或 `INSUFFICIENT_EVIDENCE`；其中 `UNKNOWN` 的 `metric_id` 必须为 null。候选可保存供审查，但不得自动通过审核或进入正式表。

## 提取 Prompt 补充

```text
在提取每条水资源披露时，同步根据所给指标 Skill 填写候选 metric_id、版本、归类状态、归类原因和其他可能解释。
归类原因必须引用该条 PDF 证据的表题、行列头、原文及统计口径。不能从单个模糊词强制判断指标。
看不清或定义不匹配时使用 UNKNOWN/NEEDS_REVIEW/INSUFFICIENT_EVIDENCE；不要编造数值、年份、单位、页码或坐标。
只输出 ExtractionBatch v1 JSON。
```

模型 JSON 无效时至多一次修复调用；仍失败保留原始响应并转人工。未登记的指标 ID 返回 `METRIC_VERSION_UNKNOWN`。审核阶段必须重新读取原 PDF 独立判断归类，不将此处的 `classification_reason` 当作事实。


## 首个开发纵切与输出容量

优先完成“上传一份真实 PDF→完整文件单次模型调用提取兼归类→结构化候选表/CSV→证据回看”纵切，再接审核。这里的“单次”指每次提取尝试只提交一份完整 PDF，禁止 Agent 切页；模型输出截断、超限或 JSON 无法修复时标记失败或人工处理，不把不完整候选当作全集。CSV 是候选导出视图，包含 `candidate_id`、`metric_id`、原值/单位、页码、证据和状态；原始 JSON 与 PDF 始终保留。
