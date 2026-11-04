# ESG报告处理项目

当前主流程：**新版EsgAgent直读PDF、提取归类 → 原B审查 → 复核工作台 → 年度研究草稿 → 2025模板导出**。新版代码以`EsgAgent/EsgAgent/`为唯一上游实现，默认网页、API和CLI均已切换，不再自动回退旧提取器。已用新版随附的华润饮料、青岛啤酒、华润啤酒3份缓存跑通整条链路；草稿包含推定和推算，不能直接用于评分。

旧版独立提取、十份报告试跑、A数据单独运行B、二次LLM填表脚本和旧CLI分支已清理。日常只使用统一工作台；开发命令统一为`scripts/run_esg_pipeline.py`。历史结果、费用账本、原始交付数据及仍被使用的共用模块保留，用于现有记录回溯。

## 日常使用

1. **启动工作台**：双击`启动审查工作台.bat`，浏览器打开`http://127.0.0.1:8765`。
2. **选择或上传PDF**：可以选择新版随附的3份报告，也可以上传PDF。核对5位股票代码及报告财年。旧10份结果保留为历史，点击“查看已有结果”可回溯。
3. **运行完整流程**：默认“复用新版缓存”，不调用API；没有新版缓存时会明确停止。新PDF请选择模型模式、设置新版提取及B的调用上限并授权完整PDF外发。
4. **查看与复核**：切换“提取数据 / A归类结果 / B审查与人工复核”，在页面内查看原页截图，保存人工意见并导出完整流程包。
5. **年度草稿与填表**：处理结束自动显示当前报告草稿，可汇总新版结果并导出。保留原模板15列和规则表，另存结果；推定口径、推算公式、吨水近似和B状态另存台账。人工修订/排除请求会暂停相关值及依赖的推算值，待重审后再处理。
6. 新版已同时提取和归类，无需旧版“二次LLM筛选”步骤；该辅助入口只用于历史数据。提取模型为qwen3.8-flash，B沿用现有审查引擎；不把上游CLASSIFIED当成B通过。

根目录只保留三个BAT：`启动审查工作台.bat`（日常操作）、`安装全流程环境.bat`（首次安装）、`一键分类.bat`（按行业归档）。提取、A、B、批量队列和Excel导出均在工作台中完成。原[静态结果入口](data/esg_pipeline/全流程结果.html)仅用于旧结果回溯。

### 批量任务队列

工作台顶部“批量处理ESG报告”支持多选上传，或从已有报告中多选添加，每批1至100份。核对每份公司代码与财年，选择缓存模式或明确授权本批模型模式，点击“开始批量处理”。A、B上限按每份分别执行；同一PDF、公司及财年的重复项自动跳过。一份失败后继续下一份，逐份保留阶段、错误、请求次数、输入/输出token及费用估算。

可选填写提取、B模型的输入/输出单价（元/百万token）；未填写单价或未返回用量的请求显示费用未知，估算不是阿里账单。队列结束后点击“导出本批2025表”，只汇总本批已完成报告；另可下载含全部成功、失败、重复和停止项的JSON运行记录。原15列表头和研究草稿标志保留。刷新页面可恢复查看；停止后当前API响应结束才生效，服务重启不自动付费重跑。详见[批量使用说明](docs/使用说明.md#批量任务队列)。

首次在新电脑运行：安装Python 3.10以上，双击`安装全流程环境.bat`。当前电脑已保留可用的`.venv-b-audit/`环境。窗口会保留运行信息，结束前不要关闭。

依赖统一维护在根目录`requirements.txt`，包括运行、测试及浏览器验证依赖；安装脚本和新版EsgAgent共用这一份，不再分别维护A/B、提取和开发依赖文件。命令行安装：`.venv-b-audit\Scripts\python.exe -m pip install -r requirements.txt`。

详细操作、API预算、缓存复用和文件接口见 **[使用说明](docs/使用说明.md)**。

2026-11-04检查及优化记录见[程序与流程检查报告](docs/程序与流程检查报告_20261104.md)：本次加强单位、范围与冲突判断，保留缓存重跑时的人工排除约束，并修复批量预检及报告登记问题。报告列明了正式评分前仍需完成的验证工作。

## 文件安排

| 位置 | 用途 |
|---|---|
| `EsgAgent/EsgAgent/` | 新版上游主体：提取、归类、13指标Skill、选择与换算 |
| `src/esg_pipeline/esgagent_*`、`scripts/esgagent_worker.py` | 隔离新核心、B契约投影、流程编排与年度接入 |
| `src/esg_extraction/`、旧A模块 | 历史兼容及共用文件工具；不作为默认提取路径 |
| `src/esg_semantic/` | 原B独立审查及历史A兼容 |
| `config/`、`skills/` | 版本化水领域配置、提示词和审查策略 |
| `review_workbench/` | 复核界面、API、数据库和Schema |
| `tests/` | 单元、契约、集成及浏览器检查 |
| `docs/` | 使用说明、技术总设计和B开发规范 |
| `data/esg_pipeline/` | 每份报告保留一套完整流程结果、模型缓存及用量账本 |
| `data/esgagent_pipeline/`、`data/review_workbench/esgagent_runtime/` | 新版CLI结果与CLI/网页共用提取缓存；旧`data/esgagent_runtime/`保留供回溯 |
| `data/review_workbench/pipeline_batches/` | 持久化批量队列、本批年度归集JSON与Excel；逐份任务仍在`pipeline_jobs/` |
| `data/ten_pdf_visual_20261102/` | 本次10份PDF的提取输入、证据、截图和视觉用量 |
| `data/review_workbench/` | 工作台数据库、原PDF附件及人工复核记录 |
| `data/water_collection/` | 命令行年度归集批次、SQLite与最新批次指针 |
| `outputs/water_collection_20261102/` | 10份报告的2025模板试跑导出 |
| `esg_agents/esg_agents/` | A同学的原始交付数据，供兼容与对照 |
| `ESG报告_按行业分类/` | 原报告库及行业归档 |
| `data/maintenance/cleanup_20261104.json` | 本次清理清单（删除路径、原因、大小和校验信息） |

模型缓存、证据和截图属于复用/回溯所需数据，不能当作普通临时文件直接删除。重新运行会生成新版本；中途停止不会再把旧的完整结果从入口中隐藏。重复试跑的用量账本集中保留在`data/esg_pipeline/usage_history/`，不重复计入缓存消耗。

当前新版登记13个水资源相关指标（其中10个数值指标映射15列），不是原86指标/179变量全量字典。B兼容视图保留所有候选，原始多证据关系保存在`upstream/extraction.json`。评分、正式生产发布及完整S5质量分档尚未实现。

网页上传、任务状态和新结果保存在`data/review_workbench/`中的`pipeline_uploads/`、`pipeline_jobs/`和`pipeline_results/`，与已有10份试验结果分开。页面刷新后可继续查看任务；服务意外退出会显示中断，不自动续费重试。

## 行业分类功能

将新PDF放进`document/五位公司代码/`，双击`一键分类.bat`；需要持续监测时，在项目目录运行`一键分类.bat --watch 10`。文件名以五位公司代码开头也可识别。分类优先使用`公司行业对照.xlsx`，缺少的代码由`data/补充公司行业.csv`补充；冲突和未知代码进入待核对，不根据正文猜测。

```powershell
python classify_esg.py --dry-run
python classify_esg.py --verify
```

原件不改写。`data/已知文件情况.json`记录原PDF异常，是业务元数据，应保留。`水资源.xlsx`是年度填表契约，修改表头或字段需同步升级程序契约。

## 开发与检查

```powershell
.venv-b-audit\Scripts\python.exe -m pytest -q
.venv-b-audit\Scripts\python.exe scripts\verify_esg_pipeline.py
.venv-b-audit\Scripts\python.exe scripts\run_esg_pipeline.py --plan
```

浏览器专项检查：`python -m pytest tests/browser_pipeline_check.py -q`，需要本机Selenium与ChromeDriver，不是日常运行依赖。

保留的B示例、专项检查、A旧交付兼容入口用于调试与回归；日常使用以“一键ESG全流程”和“启动审查工作台”为主。

- [技术总设计](docs/ESG_Water_MultiAgent_Technical_Design.md)
- [B开发规范](docs/B_开发文档.md)
- [工作台接口](review_workbench/API.md)

密钥仅从本机`阿里api key`读取，不上传到代码库，不放进文档。
