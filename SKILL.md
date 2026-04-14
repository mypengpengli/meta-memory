---
name: memory-orchestrator
description: 在对话中按“人物画像、当前状态、关系、事件、目标、领域切面、会话状态”加载需要的记忆，并在回合结束后把新信息写入会话记忆或候选池。适用于希望长期记住某个人的稳定特征、阶段变化、关系网络、项目目标和领域经验的场景。
---

# 记忆编排器

这个 Skill 的目标不是“把所有资料都读出来”，而是“围绕一个具体的人，按需读取少量高价值记忆，再把新信息写回合适层级”。

这套记忆系统默认采用四条主轴：

- 主对象：一个具体的人
- 时间维度：长期稳定 / 当前阶段 / 具体事件 / 已失效
- 关系网络：重要人物、事件、项目、来源
- 记忆层级：画像、状态、事件、关系、目标、领域、会话、候选、归档

## 默认读取顺序

每次需要记忆时，按这个顺序处理：

1. 先读人物画像：
   - [references/memory/person-profile.md](references/memory/person-profile.md)
2. 再读当前状态：
   - [references/memory/state-current.md](references/memory/state-current.md)
3. 如果问题和长期目标或项目相关，再读：
   - [references/memory/goals-projects.md](references/memory/goals-projects.md)
4. 如果问题和某个人际关系强相关，再读：
   - [references/memory/relationships-current.md](references/memory/relationships-current.md)
5. 如果问题明显涉及过去经历、变化原因、历史节点，再读：
   - [references/memory/timeline-index.md](references/memory/timeline-index.md)
6. 如果问题是某个专业领域或生活领域的切面，再读：
   - [references/memory/domains-index.md](references/memory/domains-index.md)
7. 如果当前任务是在继续上一段工作，再读会话记忆：
   - [references/memory/session-current.md](references/memory/session-current.md)
8. 如果刚形成一些未稳定的新结论，可参考候选池：
   - [references/memory/candidate-pool.md](references/memory/candidate-pool.md)
9. 如果摘要不够、必须找证据时，最后才看归档索引：
   - [references/memory/archive-index.md](references/memory/archive-index.md)

## 读取规则

- 不要一上来把所有记忆都读进来。
- 先判断当前问题是在问“这个人是谁”“这个人现在怎样”“这个人经历过什么”“这个人和谁有关”“这个人正在推进什么”。
- 先读当前有效的稳定信息，再读历史信息。
- 领域切面是补充层，不是主入口。
- 原始归档不直接进 prompt，只作为最后的证据层。

如果一条记忆同时涉及多个方向，不要强行换主目录。
优先保证：

- 有明确主对象
- 有时间边界
- 有可信度
- 有来源
- 有关联人物
- 有关联事件

## 写回规则

每轮对话后，不要直接污染长期记忆。

默认写回顺序：

1. 先更新 `session`
2. 再把可能长期有价值的信息写入 `candidate`
3. 只有足够稳定、来源清楚、时间边界明确时，才提升到：
   - `profile`
   - `state`
   - `event`
   - `relationship`
   - `goal`
   - `domain`

## 最小脚本闭环

如果要把这套 Skill 真正跑起来，默认可以这样配合脚本使用：

1. 回答前用 `scripts/memory_runtime.py prepare-context`
2. 回答后用 `scripts/memory_runtime.py finalize-turn`
3. 用户明确要求记住时，用 `scripts/memory_runtime.py remember`
4. 日常需要手动写入时，再用 `scripts/ingest_memory.py` 或 `scripts/write_memory.py`
5. 用 `scripts/review_candidates.py` 看哪些候选值得提升
6. 稳定候选用 `scripts/promote_candidates.py` 提升到 `profile` / `state` / `event` / `relationship` / `goal` / `domain`
7. 需要人工维护时，再用 `scripts/run_maintenance.py`

这套入口默认支持首次自动建库，不要求用户先手动跑初始化脚本。

## 后台心跳与增量整理

如果目标是“无感运行”，纯 Skill 更适合事件驱动，不要强依赖后台常驻。
正确顺序是：

1. 新内容先进入 `raw_events`
2. 回答前顺手检查旧的 `pending` 增量
3. 回合结束后再记录本轮回复，并视情况整理
4. 整理结果落到正式记忆层，来源关系留在数据库
5. 回答时先取正式记忆，不够再查原始事件

对应脚本：

- `scripts/ingest_raw_event.py`
  - 把新对话、新日志、新笔记写进 `raw_events`
  - 默认去重，避免同一条原始内容被反复整理
- `scripts/run_heartbeat.py`
  - 单次执行心跳与增量整理
  - 只处理 `processed_state = pending` 的事件
- `scripts/heartbeat_service.py`
  - 按固定间隔重复执行心跳
  - 适合后台常驻或外部计划任务
- `scripts/search_raw_events.py`
  - 按时间、主题、处理状态、query 下钻原始层
- `scripts/memory_runtime.py`
  - 宿主桥接入口
  - 统一提供 `prepare-context` / `finalize-turn` / `remember` / `record-event`

推荐时间策略：

- 轻量心跳：`5-10` 分钟
- 重整理：`30-60` 分钟
- 若未处理事件数先达到阈值，例如 `3-10`，可提前整理

推荐默认策略：

- `policy=balanced`
  - 稳定内容可直接进长期层
  - 不确定内容优先留在 `candidate`
  - 当轮临时信息保留在 `session`

如果你要查“某个时间范围、某个主题里到底发生过什么”，优先用：

- `scripts/search_raw_events.py --subject-id ... --since ... --until ... --topic ...`

如果你要真正把这套 Skill 接到外部宿主，优先用：

- `scripts/memory_runtime.py prepare-context`
  - 回答前取上下文
- `scripts/memory_runtime.py finalize-turn`
  - 回答后记录助手回复并顺手整理
- `scripts/memory_runtime.py remember`
  - 用户明确要求记住时写入结构化记忆
- `scripts/memory_runtime.py record-event`
  - 只记录原始事件，不立即整理

推荐习惯是：

- 拿不准时先写 `candidate`
- 当前阶段信息优先写 `state`
- 正在推进的临时工作写 `session`
- 真正稳定后再提升到 `profile` / `relationship` / `goal` / `domain`

`classify_memory.py` 的作用不是代替人判断，而是先给出一版合理建议：

- 它会判断更像画像、状态、事件、关系、目标、领域、会话还是候选
- 如果推荐的是 `session` 或 `candidate`，还会给出一个 `underlying_long_term_kind`
- 输出里带 `suggested_payload`，可以直接作为后续写入参考

`ingest_memory.py` 是更适合日常使用的入口：

- 它会先分类，再写入
- 默认尊重分类结果
- 如果你明确想跳过 `candidate/session`，可以改用 `--use-underlying-kind` 或 `--force-kind`

`review_candidates.py` 则适合定期整理：

- 它会列出候选池里最值得提升的内容
- 给出 `promote_now` / `review_after_verification` / `keep_candidate`
- 给出建议提升到哪一类长期记忆

提升时要尽量保证：

- 时间边界清楚
- 来源足够可信
- 不是一次性情绪或猜测
- 值得跨会话复用

如果在 Windows 命令行里直接传中文参数不稳定，优先使用：

- `scripts/write_memory.py --payload-file ...`
- `scripts/retrieve_memories.py --query-file ...`
- `scripts/ingest_raw_event.py --payload-file ...`
- `scripts/search_raw_events.py --query-file ...`

## 何时把信息写到哪一层

### 人物画像

适合：

- 长期稳定身份
- 长期偏好
- 明确长期约束
- 核心习惯

### 当前状态

适合：

- 某个阶段持续成立的状态
- 最近一段时间的工作、健康、家庭、情绪状况

### 事件记忆

适合：

- 有时间点的关键经历
- 导致状态变化的节点
- 重要转折

### 关系记忆

适合：

- 重要人物画像
- 相处方式
- 边界
- 敏感点

### 目标与项目

适合：

- 长期目标
- 阶段计划
- 正在推进的项目
- 项目约束与状态

### 领域切面

适合：

- 工作、学习、健康、财务等方面可复用的长期经验

### 会话记忆

适合：

- 当前做到哪
- 临时结论
- 下一步
- 尚未验证的问题

### 候选池

适合：

- 可能长期有价值，但还不够稳定
- 可能和旧记忆冲突
- 需要继续验证来源和边界

## 使用这个 Skill 的硬规则

- 目标是帮助当前回答，不是展示整个人生档案。
- 长期记忆必须尽量短、稳、可复用、时间边界清楚。
- 当前状态不等于长期画像。
- 事件不等于状态。
- 如果拿不准，就先写入候选池。
