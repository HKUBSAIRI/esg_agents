# 02 数据审核 Agent

## 独立核验

`audit_candidate(report_id, candidate_id)->AuditDecision`。审核 Agent 从对象存储重新取得原 PDF，再次向 `qwen3.8-flash` 传入同一原 PDF，并要求围绕候选页码及上下文核对文本、表格和图表，不仅阅读上游 JSON。审核提示词仅把上游结论标为“待验证主张”，同时传入指标 Skill；审核 Agent 独立给出数值、单位、年份、边界、归类、证据位置六项结果。

```text
你是独立审核员。原 PDF 为唯一企业披露事实来源。待验证主张不构成证据。
逐项核对原值、单位、年份、统计边界、指标定义和页码；引用你重新读取的 PDF 内容。
所有核对项通过且证据完整才 PASS；明确错误 REJECTED；歧义或无法读取 NEEDS_MANUAL_REVIEW。
只输出 AuditDecision v1 JSON；给出问题、建议和理由。
```

## 状态与流转

- `PASS`：六项检查均 `PASS`，证据页/原文一致，经 Python 校验后事务提交正式数据。
- `REJECTED`：明确误读、错年、错单位或错分类。创建新候选版本回退提取填表，最多一次自动重试；再失败转人工。
- `NEEDS_MANUAL_REVIEW`：模糊、证据不足、模型无法判定，写 `review_cases`。Web 页面显示 原 PDF 内嵌预览及页码跳转、候选及其归类、审核检查、原文与表格上下文；人工可修正并批准/驳回，需账号、时间、理由。批准前不进正式表。

```json
{"audit_id":"au_01","candidate_id":"ca_01","status":"NEEDS_MANUAL_REVIEW","checks":{"value":"PASS","metric":"UNCERTAIN","unit":"PASS","year":"PASS","boundary":"UNCERTAIN","evidence":"PASS"},"pdf_evidence_ids":["ev_a01"],"issues":["集团边界不明确"],"suggested_fix":"核查附注","reason":"表格未定义覆盖范围","reviewer_type":"MODEL","model_call_id":"mc_02"}
```

## Web 接口

`GET /api/review-cases?status=OPEN`、`GET /api/review-cases/{id}`、`GET /api/reports/{id}/pages/{page}`、`POST /api/review-cases/{id}/decision`。决策体：`{"decision":"APPROVE|REJECT","edited_candidate":null,"reason":"...","expected_version":3}`。服务端校验 RBAC、版本号、证据、分类和单位；编辑会派生新候选/人工审核记录。审批与正式数据写入同一数据库事务；并发冲突返回 409。不得由前端直接写正式表。

## 异常

PDF 哈希不符 `SOURCE_CHANGED`；定位失败 `EVIDENCE_NOT_LOCATED`；审核模型超时最多三次，仍失败转人工。审计日志不可清理；模型审核与人工覆盖均保留历史。
