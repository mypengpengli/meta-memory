---
name: memory-orchestrator
description: 为一个具体人物或主体准备记忆上下文、记录原始对话事件，并把信息保守写回到 session/candidate 或显式沉淀到长期层。适合需要在多轮对话中通过 `scripts/memory_runtime.py prepare-context`、`finalize-turn`、`remember` 管理 profile/state/goal/relationship/event/domain 记忆，同时避免全量加载记忆文件或误污染长期层的场景。
---

# Memory Orchestrator

把这个 Skill 当作“宿主前后置运行时”。
默认先跑运行时，不先手工展开很多记忆文件。

按下面顺序使用：

1. 回答前运行 `scripts/memory_runtime.py prepare-context`
   - 读取返回的 `context_markdown`
   - 只把相关上下文注入当前回答
2. 回答后运行 `scripts/memory_runtime.py finalize-turn`
   - 记录原始事件
   - 让系统保守整理
3. 用户明确要求“记住这个”时运行 `scripts/memory_runtime.py remember`
4. 只有当一次回答本身值得沉淀时，才在 `finalize-turn` 上加 `--capture-artifact`

默认数据目录是 Skill 根目录下的 `memory-data/`。
默认数据库文件是 `memory-data/db/memory_index.sqlite`。
多行文本、中文引号或宿主传入内容优先走 `--query-file`、`--reply-file`、`--title-file`、`--content-file`、`--payload-file`。

默认写回规则：

- 先写 `raw_events`
- 自动整理默认只进 `session` / `candidate`
- 长期层优先通过显式 `remember`
- 用户问句不要直接进长期层

不要把 `references/` 当成默认上下文。
只有在下面情况才补读参考文件：

- 运行时返回的上下文不够
- 需要人工审计读取顺序或写回策略
- 需要排查分类、canonical 页或来源映射问题

补读时，一次只读最相关的 1 份；不够再继续。
建议顺序：

1. `references/reference-map.md`
2. `references/loading-rules.md`
3. `references/writeback-rules.md`
4. `references/memory/index.md`
5. `references/memory-system.md`

维护时使用：

- `scripts/run_maintenance.py`
  - 重建索引、评分、视图并跑 lint
- `scripts/lint_memory.py`
  - 检查错误提升、缺来源、重复 canonical 页

系统会持续生成：

- `memory-data/index.md`
  - 正式记忆导航页
- `memory-data/log.md`
  - 原始事件时间线
- `memory-data/sources.md`
  - 来源层与正式记忆层分工
