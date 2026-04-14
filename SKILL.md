---
name: memory-orchestrator
description: 在对话里为一个具体的人加载长期记忆、记录新事件、并把稳定信息整理成可复用记忆。适合需要围绕人物画像、当前状态、关系、事件、目标和候选信息进行长期记忆编排的场景。
---

# Memory Orchestrator

这个 Skill 的目标不是“读出所有资料”，而是：

- 回答前只取最相关的少量记忆
- 回答后把新信息写回合适的层
- 拿不准时先进入候选层，不直接污染长期记忆

## Quick Start

默认按每回合事件驱动使用：

1. 回答前运行 `scripts/memory_runtime.py prepare-context`
2. 回答后运行 `scripts/memory_runtime.py finalize-turn`
3. 用户明确说“记住这个”时运行 `scripts/memory_runtime.py remember`

这套入口支持首次自动建库，不要求用户先手动运行初始化脚本。

## 运行机制

这套 Skill 依赖两层存储：

- Markdown：正式记忆
- SQLite：原始事件、索引、处理状态、来源映射

主流程：

1. 新内容先进入 `raw_events`
2. `prepare-context` 整理旧的 `pending` 事件，检索相关记忆，再记录当前用户问题
3. `finalize-turn` 记录助手回复，并在需要时继续整理
4. `remember` 直接写入结构化记忆，同时保留原始来源

## 默认读取顺序

先判断当前问题在问什么，再决定读哪层。

- 问“这个人是谁”、长期偏好、稳定习惯、回答风格时，先读 `person-profile.md`
- 问最近一段时间的状态、近况、困扰、阶段变化时，先读 `state-current.md`
- 问长期目标、计划、项目进度、明确约束时，先读 `goals-projects.md`
- 问某个重要人物、沟通方式、边界、关系模式时，先读 `relationships-current.md`
- 问发生过什么、为什么会变成这样、前后变化、时间线时，先读 `timeline-index.md`
- 问工作、学习、健康、财务、日常等具体领域时，先读 `domains-index.md`
- 问当前这轮任务做到哪、刚确认了什么、下一步是什么时，先读 `session-current.md`
- 问还没证实的观察、暂时判断、可能冲突的信息时，先读 `candidate-pool.md`
- 问证据、原话、长日志、原始记录时，先读 `archive-index.md`

如果问题同时涉及多类内容，再按这个顺序补充展开：

1. `person-profile.md`
2. `state-current.md`
3. `goals-projects.md`
4. `relationships-current.md`
5. `timeline-index.md`
6. `domains-index.md`
7. `session-current.md`
8. `candidate-pool.md`
9. `archive-index.md`

已经足够回答时就停止，不要把后面的层全部读完。

## 默认写回规则

- 长期稳定偏好和身份：`profile`
- 当前阶段持续状态：`state`
- 有明确时间点的经历：`event`
- 和重要人物相关的稳定模式：`relationship`
- 长期目标和项目：`goal`
- 工作/学习/健康等可复用经验：`domain`
- 当前任务临时上下文：`session`
- 还不够稳定、还需观察：`candidate`

拿不准时先写 `candidate`。

## 宿主入口

- `scripts/memory_runtime.py prepare-context`
  - 回答前取上下文
- `scripts/memory_runtime.py finalize-turn`
  - 回答后记录回复并顺手整理
- `scripts/memory_runtime.py remember`
  - 用户明确要求记住时写入正式记忆
- `scripts/memory_runtime.py record-event`
  - 只记录原始事件，不立即整理

## 规则

- 不要把所有记忆都读进上下文
- 优先读当前有效信息，再读历史信息
- 原始归档只作为证据层，不直接大量塞进 prompt
- 不确定信息先进入 `candidate`
- 长期记忆尽量短、稳、可复用

## 需要更多细节时再读

- `references/memory/index.md`
  - 想先判断“当前问题该展开哪份顶层记忆”时读
- `references/reference-map.md`
  - 想判断“下一份参考规则该读哪篇”时读
- `references/loading-rules.md`
  - 想看更细的加载停止条件和下钻条件时读
- `references/writeback-rules.md`
  - 想看新信息该写回哪层时读
