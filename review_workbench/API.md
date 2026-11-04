# S4 接口与接入约定

## 统一流程接口

以下接口复用工作台的Host/Origin校验和写请求Token。浏览器只能传报告ID或上传ID，不能传本机路径、密钥或模块名。PDF上传采用`application/pdf`，最大64 MB；其他写接口使用JSON。状态和产物保存在当前工作台数据目录。

| 方法 / 路径 | 用途 |
|---|---|
| `GET /api/pipeline/reports` | 可选的已有报告及已有结果ID |
| `POST /api/pipeline/upload` | 上传PDF，返回不透明上传ID；`X-Filename`为URL编码文件名 |
| `POST /api/pipeline/jobs` | 创建完整流程；参数`source_id, mode, vision, allow_external, vision_calls, a_calls, b_calls` |
| `GET /api/pipeline/jobs` | 最近20个任务 |
| `GET /api/pipeline/jobs/{job_id}` | 阶段、页进度、新增调用/usage和最终复核运行ID |
| `POST /api/pipeline/jobs/{job_id}/cancel` | 空JSON，停止后续步骤；当前调用结束后生效 |
| `GET /api/pipeline/runs/{run_id}?tab=extract或classify&offset=0&limit=25` | 提取候选/A记录分页及覆盖概况；每页最多100条 |
| `GET /api/pipeline/runs/{run_id}/pages/{page_no}` | 页面内查看原页截图 |
| `GET /api/pipeline/runs/{run_id}/export` | A产物、验证与用量；内含保留B原文和人工意见的`review_workbench.zip` |

新流程复用现有提取、A、B引擎，默认`rules_only`，模型模式需明确外发授权及上限；同一时刻与B/旧审查共用一个执行槽。重启后未完成任务标记INTERRUPTED，不自动重试。主线程只做上传/调度，处理在单一后台执行器运行；PyMuPDF操作用锁串行。人工复核仍由下面的既有接口保存，不把PARTIAL当作已验收。

## 既有S4接口

版本：`s4-1.0`。默认 http://127.0.0.1:8765 ，只服务本机。此处 API 为独立工作台适配层，未来可包装为主设计 `/v1/audit:run` 等正式路由；业务核心不依赖路由命名。

## 本地 HTTP

| 方法 | 路径 | 请求/响应 |
|---|---|---|
| GET | `/api/bootstrap` | 进程令牌、是否配置模型、最近100次运行 |
| POST | `/api/import` | body 为 bundle；返回 `201 {bundle_id}` |
| POST | `/api/demo` | body `{}`；返回 `202 {run_id}`，只运行内置合成回放 |
| POST | `/api/runs` | `{bundle_id, mode: offline或http}`；返回 `202 {run_id}` |
| GET | `/api/runs/{run_id}` | 输入、状态、进度、审查结果、任务、历史与下游状态 |
| POST | `/api/runs/{run_id}/tasks/{task_id}/review` | 人工处置，见 review Schema |
| GET | `/api/runs/{run_id}/export` | 不可变结果与当前复核历史的 ZIP 快照 |
| GET | `/api/runs/{run_id}/pdf` | CLI 明确附加的 PDF；未附加返回404 |
| GET | `/api/schemas/{name}.schema.json` | 公共 Schema |
| GET | `/api/example` | 合成示例 bundle |

写请求使用 `Content-Type: application/json` 和 `X-Workbench-Token`。令牌从 bootstrap 获取，进程重启后刷新；不等同用户认证。拒绝非本机 Host、跨站 Origin。输入上限 8 MB。运行时单并发；忙时返回409。HTTP202后轮询详情，页面默认每900ms轮询一次。

错误统一为 `{error, message}`：400契约错误、403来源/令牌错误、404不存在、409并发/状态冲突、500内部错误。原始异常、密钥和供应商错误正文不返回浏览器。

## Python 对接

仅审查，不需要数据库或 HTTP：

```python
from review_workbench.auditor import audit
from review_workbench.contracts import validate_bundle
from review_workbench.providers import OfflineProvider

bundle = validate_bundle(your_bundle)
result = audit(bundle, OfflineProvider(), run_id="your_unique_run_id")
```

新增模型：实现 `name`、`model` 和 `review(context, max_tokens) -> Reply`。预期故障抛 `ModelFailure(code, retryable)`；密钥只保留在适配器对象内。不要把上游对象直接交给会原地修改输入的 SDK；引擎会检查输入指纹。

保存、复核及导出：复用 `Application` 与 `Store`。生产服务可以另建 Repository 适配器，仍保留事件追加和版本 CAS 语义。

## 与上游接入

- 单个 bundle 只有一个报告，所有 records/evidence 都归属于 report_context.pid。
- 每条 record 需要 indicator_id；若上游以指标组封装，适配器显式补入该字段。
- `record_uid`、`evidence_id` 保留上游值，跨运行由 run_id 分隔。不通过公司名猜测实体，不复用旧公司代码来覆盖主体。
- 年份为记录实际披露年度，报告年度与财年起止日期单独保存；不能按发布日期直接填记录年度。
- 缺失使用 null；0 为实际数值。范围 breakdown 必须有依据，不能缺省为 Group。
- 未知变量仍可按原字符串传入并保留归类状态，审查会转人工；未知 indicator_id 必须先提供该指标的闭集定义。
- `extensions` 保存额外字段，不传给模型。来源元数据放 source_ref；当前模型只接收质量提示的受限摘要。
- 不隐式读取当前目录所有 PDF，不对分类目录进行移动或修改。

## 审查结果

finding 使用 `review_policy_version`，outcome 使用 `policy_version`；这是对主文档不一致 Schema 的显式修正。公共 Schema 由 `contracts.py` 维护，导出命令：

```powershell
python -m review_workbench schemas --out review_workbench/schemas
```

每组最多20记录、40条证据，单证据模型片段最多1200字。完整证据仍保存在输入快照。分批/证据遗漏信息在 coverage 中；本版对分批记录保守转人工，暂不做跨批模型归并。模型自报 confidence 不是实测准确率。

## 人工处置示例

```json
{
  "action": "CORRECT_VALUE",
  "reviewer": "研究员姓名",
  "reason": "已核对第12页披露表，提出修订并重审",
  "expected_version": 0,
  "proposed_value": 0
}
```

任务 version 每次提交加一。相同旧版本再次提交返回409，不覆盖原事件。REMAP 需 proposed_variable 且在该指标闭集内；跨指标修改交回上游。组级遗漏任务没有原记录，不能在此直接改值或改映射。

## 交给 S5 / Planner

必须同时消费 `audit_findings.jsonl`、`audit_report.json`、`review_tasks.json`、`review_events.jsonl`、`s5_handoff.json` 和 manifest：

1. 核验 run_id、输入 SHA256、闭集、策略及水规则版本。
2. 拒绝 is_demo=true 的数据进入正式结果/评分。
3. REVIEW_REQUIRED、未解决组任务、审查中断或失败均不得误判为通过。
4. REAUDIT_REQUIRED 对应 `RECLASSIFY_AND_REAUDIT` 事件：生成新上游版本，再运行 S4，完成后重做 S5；本模块不自动执行这一流程。
5. EXCLUDED_FROM_SCORING 为排除请求，保留数据及依据，不表示通过其他验证。
6. READY_FOR_S5 仍需 S5 全部校验和入库门禁。没有任何字段表示“已通过 S5”。

生产对接应由已认证身份替换 reviewer 自报字段，并加入角色权限和正式幂等键。当前不把业务规则、质量分或评分公式写入前端。
# B独立审查扩展（2026-10-30）

旧接口保持兼容。B结果视图是工作台内部适配格式，不使用旧bundle契约重新定义A/B公共模型。

| 接口 | 请求与行为 |
|---|---|
| `POST /api/b/demo` | 空对象；后台运行B合成夹具，返回`run_id` |
| `POST /api/b/import` | `{"files":{"文件名":"UTF-8内容"}}`；要求四类B结果，允许附加输入和闭集；不调用模型 |
| `POST /api/b/run` | 同上，但只接受B输入文件；要求本机可信A工厂和正式契约；后台运行 |
| `POST /api/runs/<id>/pdf` | `application/pdf`原始字节，最大64 MB，仅B非运行中任务；附件不可覆盖 |

沿用`GET /api/runs/<id>`查询进度/详情、原有人工复核接口和导出接口。B导出将原始文件放入`b_original/`，网页侧人工意见放入`workbench/`。Bootstrap新增`b_audit`配置状态；该状态不等同于A接口验收成功。写操作继续要求`X-Workbench-Token`及合法Host/Origin。

文件导入仅进行本地展示一致性检查，来源标记为`import_unverified`；正式工厂实际绑定完成后标记`formal_bound`，合成运行标记`demo`。三者均不能直接视为生产入库或评分许可。

日常运行和当前适配限制见[统一使用说明](../docs/使用说明.md)，正式契约要求见[B开发规范](../docs/B_开发文档.md)。

