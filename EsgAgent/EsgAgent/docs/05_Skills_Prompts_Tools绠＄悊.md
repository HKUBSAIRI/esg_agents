# 05 Skills、Prompts、Tools 管理

## 边界

Skill 是版本化知识与规则，不运行代码；Prompt 是 Agent 行为及输出契约；Tool 是带权限的确定性执行能力。官方 PDF 调用格式提供文件内容项与文本内容项，未定义本项目的 Skill 对象参数；`SkillLoader` 从文件/登记表读取所需片段，按 token 预算拼成提示词文本，与 PDF 文件内容项同次发送，保存 digest 与版本。不得把全部行业知识每次塞入上下文。

```text
skills/common/{evidence,normalization}.md
skills/industries/{industry_code}/recognition.md
skills/metrics/{metric_id}/{version}.md
skills/evaluation/{rule_id}/{version}.md
prompts/{extraction,audit,summary}/v1.j2
```

每份 Skill front matter：`id`、`version`、`kind`、`applies_to`、`depends_on`、`priority`、`token_estimate`、`metric_ids`。正文含定义、识别/排除规则、单位、边界、反例、证据要求；评价 Skill 还需关联已注册 formula_id。加载顺序：common→企业行业→任务候选指标→必要评价；依赖递归去重，版本锁定，超过预算按任务相关性裁剪并记录遗漏。缺必要 Skill 则失败，不静默继续。

```python
bundle = skill_loader.resolve(task='extraction', industry_code='manufacturing',
  metric_ids=['water_withdrawal','water_consumption'], max_tokens=12000)
prompt = prompt_registry.render('extraction', version='v1',
  skill_text=bundle.text, report_context=report_context)
registry.register(ToolSpec(name='calculate_metric', version='v1', handler=calculate_metric,
  input_schema=CalculateInput.model_json_schema(), output_schema=ToolResult.model_json_schema(),
  permission='facts:read', timeout_s=20))
```

工具发现 `registry.list_for(agent='summary', principal=ctx.user)`，调用 `registry.invoke(name, args, ctx)`；参数严格 JSON Schema 校验、权限校验、超时、幂等与调用日志统一由注册器处理。新增指标：新增定义表记录与 Skill；新增行业：新增行业 Skill；新增公式：新增 Python handler、公式版本登记、测试与 Skill 引用；不改 Agent 核心循环。Prompt 版本与 Skill digest 随每次调用存档。
