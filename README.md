# Meta Memory

`meta-memory` 是一个适合以 `SKILL` 方式分发的人物长期记忆系统。
它的目标不是把所有信息一次性塞进上下文，而是：

- 围绕一个具体的人建立长期记忆
- 回答时只取最相关的少量记忆
- 新信息先进入原始事件层，再逐步整理成稳定记忆

## Quick Start

默认按“每回合事件驱动”使用，不需要先手动初始化。
第一次调用运行时入口时，store 会自动创建。

回答前：

```text
python scripts/memory_runtime.py prepare-context --store D:\memory-data --subject-id me --subject-name 我 --session-id session-20260413 --query "我最近的睡眠状态有什么值得注意的吗？"
```

回答后：

```text
python scripts/memory_runtime.py finalize-turn --store D:\memory-data --subject-id me --subject-name 我 --session-id session-20260413 --reply "最近睡眠里最值得注意的是它和晚饭时间可能有关，但目前还只是候选观察。"
```

用户明确要求记住时：

```text
python scripts/memory_runtime.py remember --store D:\memory-data --subject-id me --subject-name 我 --title 回答风格偏好 --content "长期更喜欢先给结论，再给解释。" --use-underlying-kind
```

如果你的宿主只能在每回合前后各调用一次脚本，这个仓库就已经能工作。

## 它做什么

- 记住“这个人是谁”
- 记住“这个人现在怎样”
- 记住“发生过哪些关键事件”
- 记住“和谁有关、在推进什么、哪些信息还只是候选”

默认记忆层：

- `profile`
- `states`
- `events`
- `relationships`
- `goals`
- `domains`
- `sessions`
- `candidates`
- `archive`

## 它怎么工作

这套系统是双层存储：

- Markdown：存稳定、可复用、可人工阅读的正式记忆
- SQLite：存原始事件、索引、命中统计、处理状态、来源映射

核心机制是：

1. 新内容先进入 `raw_events`
2. `prepare-context` 在回答前整理旧的 `pending` 事件，并检索相关记忆
3. `finalize-turn` 在回答后记录助手回复，并在需要时继续整理
4. `remember` 在用户明确要求时直接写入结构化记忆，同时保留来源
5. 如果需要证据或时间线，再从 `raw_events` 下钻

## 主要入口

- `scripts/memory_runtime.py prepare-context`
  - 回答前取上下文
- `scripts/memory_runtime.py finalize-turn`
  - 回答后记录回复并顺手整理
- `scripts/memory_runtime.py remember`
  - 显式写入记忆
- `scripts/memory_runtime.py record-event`
  - 只记录原始事件，不立即整理

## 什么时候用哪一层

- 稳定人物特征和长期偏好：`profile`
- 当前阶段持续成立的状态：`states`
- 有明确时间点的关键经历：`events`
- 与重要人物相关的稳定模式：`relationships`
- 长期目标和项目：`goals`
- 工作/学习/健康等可复用经验：`domains`
- 当前回合临时推进信息：`sessions`
- 还不够稳定、仍需观察：`candidates`

拿不准时，先写 `candidate`。

## 对外使用建议

如果你想把它发给别的智能体或宿主，优先保留 `SKILL + scripts + references` 这套结构。
它比插件更容易移植，因为：

- 不依赖特定宿主的插件生命周期
- 不要求后台常驻
- 只要求宿主能在每回合前后调用脚本

## 详细资料

更详细的设计、加载规则和参考模板在：

- [SKILL.md](SKILL.md)
- [references/memory-system.md](references/memory-system.md)
- [references/loading-rules.md](references/loading-rules.md)
- [references/writeback-rules.md](references/writeback-rules.md)
- [references/memory/index.md](references/memory/index.md)
