---
name: memory-orchestrator
description: 在对话中按领域、主题、项目和会话状态加载需要的记忆，并在回合结束后把新信息写入会话记忆或候选记忆。适用于需要调用长期偏好、工作经验、学习积累、家庭与子女教育、日常生活、健康、财务消费、人际关系等长期记忆的时候；也适用于判断一条新信息应该写到固定记忆、领域记忆、主题记忆、项目记忆、会话记忆还是候选池。
---

# 记忆编排器

这个 Skill 不是单纯“管理记忆”的说明书。
它的主要作用是：在对话中按需要加载记忆，并在对话后把值得保留的信息写回到合适的层级。

这套记忆系统不只使用“树状分类”。
它默认采用三轴结构：

- 领域：工作、学习、家庭与子女教育、日常生活、健康、财务消费、人际关系
- 记忆性质：固定、主题、项目、会话、候选、归档
- 主题网络：标签、关联主题、关联项目、关联来源

也就是说，文件组织是树，真正的召回是树加主题网络混合。

为了方便移植，这个 Skill 默认把记忆文档放在固定目录 `references/memory/` 下面。
如果以后记忆规模很大，可以再把原始归档迁出去，但第一版先以可移植和可维护为主。

## 默认读取顺序

每次需要记忆时，按这个顺序处理：

1. 先读固定记忆：
   - [references/memory/fixed-memory.md](references/memory/fixed-memory.md)
2. 再看总索引，判断当前问题落在哪个领域：
   - [references/memory/index.md](references/memory/index.md)
3. 只展开最相关的领域索引或领域主题：
   - [references/memory/work-index.md](references/memory/work-index.md)
   - [references/memory/learning-index.md](references/memory/learning-index.md)
   - [references/memory/family-education-index.md](references/memory/family-education-index.md)
   - [references/memory/daily-life-index.md](references/memory/daily-life-index.md)
   - [references/memory/health-index.md](references/memory/health-index.md)
   - [references/memory/finance-index.md](references/memory/finance-index.md)
   - [references/memory/relationships-index.md](references/memory/relationships-index.md)
4. 如果某条记忆的关联主题、标签或关联项目明显命中，再额外展开 1 到 2 份具体主题记忆。
5. 如果当前任务明显属于这个项目，再读项目记忆：
   - [references/memory/project-current.md](references/memory/project-current.md)
6. 如果当前任务是在继续上一段工作，再读会话记忆：
   - [references/memory/session-current.md](references/memory/session-current.md)
7. 如果摘要不够、必须找证据时，最后才看归档索引：
   - [references/memory/archive-index.md](references/memory/archive-index.md)

## 读取规则

- 不要一上来把所有记忆都读进来。
- 先判断当前问题属于什么领域和主题，再挑最相关的记忆。
- 固定记忆每次都可以考虑，但内容必须保持很小。
- 领域记忆和主题记忆一次最多展开少量文档，不要全读。
- 项目记忆只在“当前项目强相关”时加载。
- 会话记忆只在“明显承接上一轮工作”时加载。
- 原始归档不直接进 prompt，只作为最后的证据层。

如果一条记忆横跨多个主题，不要强行在目录上二选一。
优先保证：

- 有一个主归属领域
- 有明确标签
- 有关联主题
- 有关联项目
- 有关联来源

完整规则见：

- [references/memory-system.md](references/memory-system.md)
- [references/loading-rules.md](references/loading-rules.md)
- [references/classification-draft.md](references/classification-draft.md)
- [references/network-memory-model.md](references/network-memory-model.md)
- [references/slotting-guide.md](references/slotting-guide.md)
- [references/topic-catalog.md](references/topic-catalog.md)
- [references/work-topic-tree.md](references/work-topic-tree.md)
- [references/learning-topic-tree.md](references/learning-topic-tree.md)

## 写回规则

每轮对话后，不要直接污染长期记忆。

默认写回顺序：

1. 先更新会话记忆
2. 如果发现了可能长期有用的信息，先写入候选池
3. 只有足够稳定、可复用、已验证，才提升到固定/主题/项目记忆

完整规则见：

- [references/writeback-rules.md](references/writeback-rules.md)

## 何时把信息写到哪一层

### 固定记忆

适合：

- 长期偏好
- 明确长期约束
- 反复出现的稳定习惯

### 主题记忆

适合：

- 某个主题下可复用的经验
- 某类事情经常踩到的坑
- 某个主题下稳定成立的方法论

### 项目记忆

适合：

- 当前项目专属的状态、决策、约束
- 当前项目里重复出现的问题

### 会话记忆

适合：

- 当前正在推进的内容
- 最近得出的临时结论
- 下一步行动

### 候选池

适合：

- 可能有长期价值，但还不够稳定的内容
- 需要去重、归类、确认的内容

## 使用这个 Skill 时的硬规则

- 记忆加载的目标是“帮助当前回答”，不是“展示所有记忆”。
- 长期记忆必须尽量短、稳、可复用。
- 会话记忆可以频繁更新，但不应直接等于长期记忆。
- 如果拿不准，就先写入候选池。
- 如果没有明显领域和主题，就只用固定记忆和当前会话记忆，不要强行展开主题记忆。
