# ESG 水资源审查工作台

这是统一的 **PDF提取 + A过滤/归类 + B审查 + 人工复核** 工作台。主流程在一个页面完成，命令行和旧S4接口仍可独立使用；日常操作见[统一使用说明](../docs/使用说明.md)。原正式字典未交付，本地水领域适配不等于正式生产验收。合成演示仍可用于调试。

## 三步使用

1. 双击项目根目录的 **启动审查工作台.bat**。
2. 在页面顶部选择已有报告或上传PDF，点击 **运行完整流程**；已有10份结果可直接查看。
3. 在“提取数据 / A归类结果 / B审查与人工复核”中查看各阶段数据，打开原页截图并保存人工意见，使用 **导出完整流程包**。示例和旧JSON导入在高级工具中。

电脑需要 Python 3.10 或更高版本。本机已具备 Python 3.11。完整PDF流程需要`安装全流程环境.bat`安装PyMuPDF和jsonschema；本机环境已保留。**内置演示和原有人工复核仍可在不安装这些依赖、不配置API的情况下使用。** 不需要Node或数据库服务。请保持启动窗口打开；关闭程序用Ctrl+C，进行中的流程会在当前调用结束后停止。审查历史保存在本机`data/review_workbench/`，重启不会丢失。

浏览器没有自动打开时，访问 http://127.0.0.1:8765 。端口冲突时，在项目根目录运行：

```powershell
python -m review_workbench --port 8766 --open
```

启动脚本使用 Windows CRLF 换行和纯 ASCII 命令，避免中文批处理的编码解析问题。窗口结束前会停留显示信息；如果启动失败，请保留错误内容。文件格式由 `tests/test_windows_launcher.py` 回归检查，并实际通过 cmd.exe 启动临时服务验证网页可访问。

## 三种审查方式

| 方式 | 使用条件 | 结果含义 |
|---|---|---|
| 合成演示回放 | 点击“体验完整演示” | 预设响应展示典型问题，不是实时 AI，不代表准确率 |
| 离线审查 | 导入输入后默认执行 | 没有模型判断，记录转人工复核，不会自动确认 |
| 真实模型 | 配置密钥后选择“真实模型”并点击启动 | 证据片段和记录发往配置的服务，响应经过契约、证据引用和状态检查 |

内置例子包括：耗水错归取水、单位不符、年度错位、厂区冒充集团、数值无证据、回用指标漏提，以及一个可以保留的真实零值示例。所有企业与数字均为合成数据。

## 复核操作的意义

- **保留**：表示人工已经核对该任务；仍要交 S5 执行正式验证。
- **修订数值 / 变量**：只生成修订请求，状态为“修订待重审”。不修改原始值，不直接放行；需要 S3 或人工版本工具生成新记录，再导入审查。
- **申请排除评分**：保留记录与历史，输出排除请求；不把该记录标为通过。
- **暂缓**：保留待处理状态和备注。

复核姓名是本机自报信息，不是经过认证的身份。修改请求不能在原任务中直接改回“通过”，须进入新运行。每次保存追加事件；旧页面提交会因版本不一致返回提示，避免覆盖。

## 导入真实数据

页面右上角接受 **UTF-8 JSON bundle，不直接接受原始 PDF**。下载页面左下方的示例输入，查看顶层六部分：

```text
schema_version     s4-1.0
report_context     报告编号、公司名、报告年度，可附起止日期
catalogue          指标闭集版本、指标定义、允许的变量
review_policy      策略版本、水规则版本、阈值、调用预算
evidence           S1 原始证据
records            S3 归类记录
```

正式闭集需要从同事处获取；内置 DEMO-SUBSET 不能代替正式闭集。自建模拟数据请在 `report_context.extensions` 中注明 `"is_synthetic": true`，导出会持续保留模拟标记。

先检查输入：

```powershell
python -m review_workbench check "你的输入.bundle.json"
```

若同事交付的是 JSONL，准备元数据 JSON（仅含前四个顶层字段），再组装：

```powershell
python -m review_workbench pack --metadata "metadata.json" --evidence "evidence.jsonl" --records "records.jsonl" --out "report.bundle.json"
```

`pack` 是契约组装器，不会猜测未知字段。上游有额外字段时，请显式映射；需要留存的扩展信息放 `extensions` 或证据的 `source_ref`。原文件不会被修改；输出已存在时不会覆盖。

需要查看原始 PDF 页时，可在命令行显式附加 PDF：

```powershell
python -m review_workbench import "report.bundle.json" --pdf "报告.pdf"
```

这会复制附件至工作台数据目录，记录 SHA256，默认做离线审查。启动页面后可查看该运行及 PDF 原页。附件不自动匹配企业，需人工确认报告一致。当前为浏览器原生 PDF 查看，不含 PDF.js 区域高亮。

## 旧版独立审查接口配置（可选）

当前新版全流程直接使用根目录的`阿里api key`，无需下面的旧版独立接口配置。根目录的示例JSON已清理。若开发者需要单独接入旧接口，可手动创建`review_workbench.local.json`：

```json
{"endpoint":"","api_key":"","model":""}
```

1. 在上述文件填写接口的`endpoint`、`api_key`、`model`。
2. 重启工作台。
3. 选择“真实模型”，点击“启动审查”，确认发送。

`endpoint` 必须是支持 `messages` / `choices` 聊天协议的完整 HTTPS 地址，包含 `/chat/completions` 路径。不同供应商模型参数可能不同；本实现使用 `model/messages/temperature/max_tokens/stream`，未启用工具调用或联网检索。不支持此协议的服务应新增 Provider 适配器，而不是修改审查核心。

阿里云接口配置请参照[百炼官方兼容接口说明](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)，使用你账户所在地域的地址、密钥与模型，不在代码中固定某个模型名。

也可用环境变量 `ESG_AUDIT_ENDPOINT`、`ESG_AUDIT_API_KEY`、`ESG_AUDIT_MODEL`；环境变量优先于本机文件。配置文件已被 `.gitignore` 排除，请勿分享。浏览器、导出文件不包含后端 API 密钥。真实调用可能产生费用；没有配置时不会发送请求。

模型网络超时、429/5xx 最多重试两次，重试计入 `max_model_calls`。无效 JSON、遗漏结论、非法代码、悬空证据等都会使该批降级，不能当作通过。截断或拆批导致覆盖不完整的记录也转人工；这是最小版有意采用的保守策略。

## 导出与上下游交接

导出 ZIP 包含：

| 文件 | 用途 |
|---|---|
| `input.bundle.json` | 完整输入语义快照，数值和证据不变 |
| `audit_findings.jsonl` / `.csv` | 逐条审查发现 |
| `audit_report.json` | 逐记录结论、分组结果、版本和统计 |
| `model_calls.jsonl` | 请求片段、响应、调用状态及预算回执 |
| `review_tasks.json` / `review_events.jsonl` | 人工任务、修订请求和历史 |
| `s5_handoff.json` | 待复核、待重审、排除请求、可送 S5 验证的状态 |
| `manifest.json` | 输入及导出文件 SHA256、运行编号、模拟标记、附件指纹 |
| `schemas/` | 版本化公共契约 |

输入指纹基于规范化 JSON（UTF-8、字段排序、紧凑序列化），不等同原 JSON 文件字节指纹。PDF 指纹单独记录。导出不包含 PDF 附件，避免重复大文件。

**S5 必须拒绝模拟数据，并检查所有未解决的组级任务。`READY_FOR_S5` 仅表示送验证，不代表已通过验证、可入库或可评分。**

## 维护与测试

```text
contracts.py       契约、校验与稳定指纹；schemas/ 是其可下载版本
auditor.py         纯审查流程，不依赖网页、数据库
providers.py       离线 / 回放 / HTTP 模型适配器
store.py           SQLite、不可变结果、复核历史及并发版本检查
service.py         导入、审查、复核、导出用例，供未来 API/Planner 复用
server.py          本地 HTTP 适配器
__main__.py        命令行与启动入口
web/               原生页面，只呈现状态、提交操作
examples/          合成输入和预设响应
API.md             接口与上下游对接说明
```

开发验证请在项目根目录运行（旧的独立检查BAT已清理）：

```powershell
python -m unittest discover -s tests -p "test*.py"
```

页面回归是可选开发检查，另外需要 Selenium、Chrome 和匹配的 ChromeDriver；运行程序本身不需要这些。操作方法见 `tests/browser_check.py` 和 [Selenium 官方说明](https://www.selenium.dev/documentation/webdriver/browsers/chrome/)。

```powershell
python -m unittest discover -s tests -p browser_check.py -v
```

此工作台是本地独立版。完整生产应用的登录、角色权限、多人服务、SSE、Planner 自动局部重跑和正式入库不在本次范围。新运行会重新审查，不自动继承旧运行的人工确认；原确认历史仍保留。接入时通过显式记录版本、证据和策略指纹决定复用，避免把旧确认错误应用到新数据。
# B独立审查接入（2026-10-30补充）

启动同一工作台后，使用“B独立审查”区域运行合成演示或导入四类结果，可复核、附加PDF并导出原始产物和人工历史。B核心与原型引擎独立，正式运行需A侧契约与客户端绑定。操作及限制见[统一使用说明](../docs/使用说明.md)。以下原型功能和接口说明继续适用。

