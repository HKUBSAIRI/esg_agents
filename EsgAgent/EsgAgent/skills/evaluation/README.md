# 评价 Skill（占位）

本目录用于存放**评价类 Skill**（`skills/evaluation/<rule_id>/<version>.md`），当前阶段仅
占位，未启用任何评价规则。

约定（见 `docs/05_Skills_Prompts_Tools管理.md`）：

- 每份评价 Skill 使用与其它 Skill 相同的 YAML 风格 front matter，`kind: evaluation`；
- 必须额外关联已登记的 `formula_id`，并声明 `applies_to`（如 `[extraction]`、`[audit]`）；
- 评价 Skill 只描述规则与证据要求，不运行代码；计算由 Python handler 执行；
- `SkillLoader.resolve()` 会在 common → industry → metric 之后按 `task` 加载 `applies_to`
  命中的评价 Skill；本目录暂无合法 Skill 时该阶段为空，不视为错误；
- 新增评价规则：先登记 formula_id 与 handler，再补 Skill 与测试，不改 Agent 核心循环。

本 README 不含 front matter，`SkillLoader` 会跳过它，不会登记为 Skill。
