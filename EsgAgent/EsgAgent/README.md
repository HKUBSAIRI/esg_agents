# ESgAgent — ESG 披露提取与归类 Agent（阶段一/二纵切）

> 2026-11-03：本目录已作为项目根工作台的新上游主体。日常请从外层`启动审查工作台.bat`启动；隔离worker执行本目录提取与选择器，随后接原B、年度数据库和Excel导出。原A/B历史保留但默认不再执行旧提取器。本目录自身尚未新增AuditAgent类，整合的B在外层模块。详见外层`docs/使用说明.md`当前默认流程。

面向港股公司 ESG/可持续发展报告，完成 **PDF 上传 → 单次模型调用直读整份 PDF → 候选数值 + 逐字证据 + 指标归类 → 阶段一归类结果 CSV** 的纵切。

- 模型：阿里云百炼 `qwen3.8-flash`（OpenAI 兼容 `chat.completions`，PDF 通过 `file_data` base64 或 `file_url` 传入）
- 提取前不解析PDF文本/版面、不做OCR；大PDF按页数及上传体积复制页面对象分块，证据页码回填为**原PDF物理页码（1起始）**。
- 已实现：入库、Skill/Prompt 管理、提取 + 归类、候选导出、CSV 导出、任务日志、单测与集成测试
- 外层统一工作台已接入现有B独立审查与复核页面；本子包不重复实现这些模块。正式数据库发布、评分和完整Summary分析仍未完成。

## 1. 环境准备

```bash
# 从整个项目的根目录安装（本目录不再保留单独的requirements）
python -m venv .venv-b-audit
.venv-b-audit/Scripts/python.exe -m pip install -r requirements.txt
```

配置 `config/.env`（已被 .gitignore 忽略，不会提交）：

```dotenv
DASHSCOPE_API_KEY=sk-xxxx
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
QWEN_MODEL=qwen3.8-flash
QWEN_VL_MODEL=qwen3-vl-plus
# 可选：QWEN_TIMEOUT_S=600 / QWEN_MAX_OUTPUT_TOKENS=32768 / MODEL_MAX_RETRIES=3
```

## 2. 快速开始

```bash
# Phase-1 冒烟：配置 / 13 个指标 Skill / 提取 Prompt / 模型连通性
.venv/bin/python -m app.cli smoke

# 单份报告：入库 → 提取归类 → 导出 CSV
.venv/bin/python -m app.cli extract \
  --pdf "HKEX ESG Reports/华润饮料 2025.pdf" --year 2025

# 大 PDF（>80 页自动分块）可指定分块并发数（1-4，默认 3）
.venv/bin/python -m app.cli extract \
  --pdf "HKEX ESG Reports/华润啤酒 2025.pdf" --company 00291.HK --year 2025 \
  --force --concurrency 3

# 目录批量（按文件名自动识别公司；也可 --force 忽略缓存）
.venv/bin/python -m app.cli batch --dir "HKEX ESG Reports"

# 与标准答案逐格对比（标准答案在 tests/fixtures/gold_stage1.json，只打印结果不生成文件）
PYTHONPATH=. .venv/bin/python scripts/compare_with_gold.py

# 复用已有提取批次重新导出
.venv/bin/python -m app.cli export --report-id rp_xxx
```

> 本地没有公网可访问的签名 URL，PDF 走 `file_data` base64（体积约 +33%）；70 页 PDF 首 token 需要几十秒到数分钟，请勿改成非流式调用。
>
> 平台单次 PDF 解析有页数上限（实测 70 页可、90 页起报 `file format illegal`）：超过
> `EXTRACT_CHUNK_THRESHOLD`（默认 80）页会自动按 `EXTRACT_CHUNK_PAGES`（默认 63）分块提取，
> 页码回填原报告物理页，`coverage.notes` 记录分块信息。分块默认**并发 3**执行
> （`--concurrency` / `EXTRACT_CHUNK_CONCURRENCY`，1-4）；思考模式保持开启，不做任何关闭或减弱。
> 189 页报告实测：串行 20.8 分钟 → 并发 3 时 **8.2 分钟**（2.5×）；48/70 页单调用报告仍约 4–5 分钟。

## 3. 输出

写入 `outputs/`：

| 文件 | 说明 |
|---|---|
| `阶段一_归类结果_汇总.csv` | 15 列归类结果表（股票代码…总磷排放量），表头逐字对齐用户模板 |
| `{代码}_{公司}_{年}_归类结果.csv` | 单公司版本，同上 |
| `{代码}_{公司}_{年}_候选人明细.csv` | 每个候选一行：metric_id、状态、原值/单位、物理页码、逐字证据、表题/行列头、归类理由、备选解释、置信度、归一化值 |
| `{代码}_{公司}_{年}_extraction.json` | 完整 `ExtractionBatch`（候选 + 证据 + 覆盖页码），正式提取产物 |

`outputs/` 放阶段一产物（归类结果 CSV、候选人明细 CSV、推算值 CSV、完整 `ExtractionBatch` JSON）；`data/` 保存不可变原件（`data/objects/pdf/<sha256>.pdf`）、报告引用（`data/reports/<report_id>.json`）与批次缓存（`data/batches/<report_id>.json|.raw.json`）；`logs/` 保存 JSONL 运行日志（不含密钥/Prompt 全文）。

## 4. 目录

```text
app/
  cli.py                      # 命令行：smoke / extract / batch / export / list-reports
  agents/extraction.py        # ExtractionAgent：草稿→规范对象、ID 重映射、页码校验、幂等缓存
  adapters/qwen.py            # QwenPDFClient：流式、json_object、重试、错误映射、调用日志
  adapters/object_store.py    # LocalObjectStore：本地不可变对象存储
  core/                       # settings / errors / ids(UUIDv7) / logging / catalog(15 列映射)
  models/contracts.py         # Evidence / ExtractionCandidate / ExtractionBatch / ReportRef / MetricDefinition
  models/model_output.py      # 模型输出草稿契约（Draft*）
  services/ingest.py          # PDF 校验、哈希、页数、入库
  services/skill_loader.py    # Skill front matter 解析、指标登记、按 token 预算拼装
  services/prompt_registry.py # Jinja2 Prompt 渲染 + digest
  services/normalizer.py      # 单位解析/换算（万吨→m³、亿元→百万元、%），仅用于展示
  services/export.py          # 候选选择（GROUP/年度/置信度）、冲突检测、CSV 导出
skills/
  common/{evidence,normalization}.md
  industries/{fb-nb,fb-ab}/recognition.md
  metrics/<metric_id>/1.0.0.md        # 13 个指标定义（水 8 个 + 排放 4 个 + 营业收入）
prompts/extraction/v1.j2
config/{.env,companies.json}
tests/                        # 单测 + 集成测试（全部离线）
```

## 5. 关键规则

- 每条候选至少 1 条证据；`source_evidence` 逐字、不可改写；`source_page` 为物理页码。
- 定量候选 `raw_value` 必填，定性候选 `raw_text` 必填；`normalized_value` 不由模型写入。
- `classification_status=UNKNOWN` 时 `metric_id` 必须为 `null`，禁止硬归类；报告只写“用水量”且无口径时不得归为取水/耗水。
- `water_withdrawal`（取水）/ `water_consumption`（耗水）/ `water_discharge`（排水）/ `water_recycled_volume`（回用量）/ `water_recycling_rate`（比率）/ `water_intensity`（强度）严格区分；COD/氨氮/总氮/总磷为 4 个独立质量指标，浓度与削减量不算排放量。
- 模型 JSON 非法时至多一次修复调用；仍失败保留原始响应转人工；输出达到 token 上限（截断）直接失败，不把不完整候选当全集。
- 提取结果本地缓存按 `report_id`；`--force` 强制重新调用。

## 6. 测试

```bash
.venv/bin/python -m pytest tests -q
```

覆盖：契约校验（UNKNOWN 禁止 metric_id）、单位换算、CSV 15 列、候选选择与冲突、草稿→规范对象（页码越界/未登记指标/坏 JSON 修复）、入库限制与幂等、Qwen adapter（fake client，不联网）、跨模块集成（PDF→提取→CSV）。

## 7. 阶段一验收状态与产物

- 实现说明、真实运行数据、逐项归类依据、待人工确认项：`docs/09_阶段一实现说明.md`
- 阶段一归类结果（15 列，三份测试报告）：`outputs/阶段一_归类结果_汇总.csv`
- 逐条证据/页码/归类理由：`outputs/*_候选人明细.csv`；完整批次 JSON：`outputs/*_extraction.json`
- 与标准答案逐格对比：`PYTHONPATH=. .venv/bin/python scripts/compare_with_gold.py`（只打印结果，不生成文件）

大文件传输：DashScope 对 base64 `file_data` 有约 28,000,000 字符上限（约 20MB PDF）。
超过时用 `scripts/oss_signed_url.py` 上传 OSS 取短时签名 URL，再 `--pdf-url` 传入（官方首选 `file_url`）：

```bash
URL=$(PYTHONPATH=. .venv/bin/python scripts/oss_signed_url.py --pdf "HKEX ESG Reports/华润饮料 2025.pdf")
.venv/bin/python -m app.cli extract --pdf "HKEX ESG Reports/华润饮料 2025.pdf" --pdf-url "$URL"
PYTHONPATH=. .venv/bin/python scripts/oss_signed_url.py --delete "<key>"   # 用后清理临时对象
```

> `config/.env`（真实 key）与 `config/companies.json` 均不提交；`config/.env.example` 为模板。
