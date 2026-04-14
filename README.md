# Meta Memory

`meta-memory` 现在面向的目标，不再只是“给对话代理补一点上下文”。
它的核心目标是：为一个具体的人建立可长期维护、可按需召回、可逐步修正的记忆系统。

这套系统不追求把“一个人的所有东西”一次性塞进 prompt。
它追求的是：

- 先把人的长期信息分层存下来
- 再根据当前问题只加载少量最相关的部分
- 新信息先进入会话层和候选层
- 经过验证后再升级成稳定长期记忆

## Quick Start

如果你是第一次打开这个仓库，先记住这一点：

- 这是一个更适合以 `SKILL` 方式分发的人物长期记忆系统
- 默认走“每回合事件驱动”，不是强依赖后台插件或后台服务
- 不需要先手动初始化，首次调用运行时入口会自动建库

最小接入方式只有 3 个动作：

1. 回答前调用 `prepare-context`
2. 回答后调用 `finalize-turn`
3. 用户明确要求记住时调用 `remember`

```text
python scripts/memory_runtime.py prepare-context --store D:\memory-data --subject-id me --subject-name 我 --session-id session-20260413 --query "我最近的睡眠状态有什么值得注意的吗？"
python scripts/memory_runtime.py finalize-turn --store D:\memory-data --subject-id me --subject-name 我 --session-id session-20260413 --reply "最近睡眠里最值得注意的是它和晚饭时间可能有关，但目前还只是候选观察。"
python scripts/memory_runtime.py remember --store D:\memory-data --subject-id me --subject-name 我 --title 回答风格偏好 --content "长期更喜欢先给结论，再给解释。" --use-underlying-kind
```

如果你的宿主只能在每回合前后各调用一次脚本，这个项目就已经能用。

## 现在的核心模型

### 1. 记忆对象是“人”，不是“领域”

旧版本更像“工作 / 学习 / 健康”的领域切片。
新版本把“人”作为主对象，领域只作为这个人的一个切面。

优先级更高的是：

- 这个人是谁
- 这个人现在处于什么阶段
- 发生过哪些关键事件
- 和哪些人有重要关系
- 正在推进哪些长期目标或项目
- 在不同领域里有哪些稳定偏好与经验

### 2. 记忆分层

推荐至少分成这几层：

- `profile`
  - 最稳定的人物画像
  - 身份、角色、长期偏好、长期约束、核心习惯
- `states`
  - 当前阶段状态
  - 工作状态、健康状态、家庭状态、情绪状态
- `events`
  - 时间线上发生过的重要事情
  - 转折点、关键经历、变化节点
- `relationships`
  - 重要人物与关系
  - 关系角色、边界、相处方式、敏感点
- `goals`
  - 长期目标、项目、计划、阶段事项
- `domains`
  - 工作、学习、健康、财务等领域切面
- `sessions`
  - 当前会话正在推进的内容
- `candidates`
  - 可能有长期价值，但还没确认稳定的内容
- `archive`
  - 原始日志、聊天记录、证据材料

### 3. 不只要分类，还要表达“变化”

想记住一个人，光有目录不够。
记忆系统还必须能表达：

- 这是长期稳定事实，还是阶段状态
- 这是以前成立，还是现在仍然成立
- 这条记忆的可信度多高
- 这条记忆来自哪里
- 它是否已经被新的记忆替代

所以每条记忆建议带这些字段：

- `subject_id`
- `subject_name`
- `memory_kind`
- `domain`
- `topic`
- `tags`
- `start_at`
- `end_at`
- `confidence`
- `status`
- `source`
- `related_people`
- `related_events`
- `supersedes`
- `replaced_by`

## 默认读取顺序

当代理需要回忆“这个人”的信息时，推荐按这个顺序读：

1. 人物画像
2. 当前状态
3. 长期目标与项目
4. 当前问题相关的人际关系
5. 当前问题相关的事件或时间线
6. 当前问题相关的领域切面
7. 当前会话记忆
8. 候选池
9. 原始归档

读取时遵守：

- 先画像，再状态
- 先摘要，再正文
- 先当前有效信息，再历史信息
- 先主对象，再关系网络
- 只有必须追证据时才看原始归档

## 默认写回顺序

每轮对话后，不要直接污染长期记忆。

推荐顺序：

1. 先更新 `sessions`
2. 再把可能重要的新信息放进 `candidates`
3. 只有稳定、可复用、时间边界清楚、来源可信时，才提升到：
   - `profile`
   - `states`
   - `events`
   - `relationships`
   - `goals`
   - `domains`

## 目录结构

初始化一个外部记忆库时，默认目录会是：

```text
memory-data/
  profile/
  states/
  events/
  relationships/
  goals/
  domains/
  sessions/
  candidates/
  archive/
    raw/
    imports/
  db/
    memory_index.sqlite
```

## 作为 Skill 使用

把仓库内容放到本地 skills 目录，例如：

```text
C:\Users\<你的用户名>\.codex\skills\memory-orchestrator
```

入口文件是：

```text
SKILL.md
```

适合的使用方式包括：

- “读取这个人的稳定画像和当前状态，再回答问题”
- “判断这条新信息应该写入状态、事件、关系，还是先放候选池”
- “用这套结构为某个人设计长期记忆库”

## 最小可用闭环

现在这套仓库已经不只是文档和索引。
它有一个最小可用闭环：

1. 首次使用时，运行时入口会自动创建外部记忆库
2. 用 `memory_runtime.py prepare-context` 在回答前取上下文
3. 用 `memory_runtime.py finalize-turn` 在回合结束后记录回复并顺手整理
4. 用户明确要求记住时，用 `memory_runtime.py remember`
5. 需要人工整理时，用 `review_candidates.py` 和 `promote_candidates.py`
6. 需要手动维护时，再运行 `run_maintenance.py`

`init_memory_store.py` 仍然保留，但现在只是可选的手动预初始化工具，不再是首次使用的前置条件。

## 无感心跳与增量整理

如果真正目标是“平时无感运行，回答时自动调取”，纯 SKILL 更适合走事件驱动，而不是强依赖后台常驻定时器。
正确做法是只处理增量：

1. 新对话、新日志、新笔记，先进入数据库里的 `raw_events`
2. 每次回答前顺手检查旧的 `pending` 事件
3. 每次回合结束后再顺手记录本轮回复并视情况整理
4. 稳定结果写入 Markdown 记忆层，来源映射和处理状态写回数据库
5. 回答问题时先查长期记忆，不够再下钻到原始事件

当前仓库已经补上了这个骨架：

- `scripts/ingest_raw_event.py`
  - 只负责把新内容写进 `raw_events`
  - 默认用 `content_hash + subject_id + session_id + source_ref` 去重
- `scripts/run_heartbeat.py`
  - 单次执行心跳与增量整理
  - 只处理还没整理过的 `pending` 事件
  - 会更新 `maintenance_cursor`，避免旧事件被重复整理
- `scripts/heartbeat_service.py`
  - 按固定间隔重复执行心跳
  - 适合挂到后台、计划任务或宿主服务里
- `scripts/memory_runtime.py`
  - 面向宿主的桥接入口
  - 把“回答前取上下文”“回合后记录回复并整理”“显式记忆写入”“任意原始事件记录”收口成统一脚本

推荐节奏不是“固定 30 分钟硬整理”，而是两级：

- 轻量心跳：每 `5-10` 分钟检查一次
- 重整理：满足任一条件再触发
  - 距离上次整理已过 `30-60` 分钟
  - 未处理事件达到阈值，例如 `3-10`
  - 用户主动要求“记一下”或“整理一下”

这样才能控制 token 消耗，因为旧内容不会被反复分类和整理。

### 原始事件写入

```text
python scripts/ingest_raw_event.py --store D:\memory-data --subject-id me --subject-name 我 --session-id session-20260413 --source-type conversation --topic-hint sleep-observation --domain-hint health --content "最近两周睡眠可能和晚饭时间有关，但还需要继续观察。"
```

### 单次心跳整理

```text
python scripts/run_heartbeat.py --store D:\memory-data --subject-id me --policy balanced --interval-minutes 30 --min-pending 3
```

### 后台心跳服务

```text
python scripts/heartbeat_service.py --store D:\memory-data --subject-id me --check-every-minutes 10 --organize-interval-minutes 30 --min-pending 3 --policy balanced
```

`balanced` 适合作为默认策略：

- 很稳定的内容直接进长期层
- 不确定、需继续观察的内容留在 `candidate`
- 明显属于本轮临时上下文的内容留在 `session`

更适合纯 SKILL 的默认节奏是：

1. 回答前调用一次 `prepare-context`
2. 回答后调用一次 `finalize-turn`
3. 明确要记住的内容再调用 `remember`

这样不需要后台常驻，也不需要先让用户做初始化，首次调用时会自动建库。

## 宿主接入入口

如果你不是人工敲命令，而是要接到聊天宿主、桌面端、服务端或者自动代理里，优先调用：

- `scripts/memory_runtime.py prepare-context`
  - 回答前使用
  - 会先整理旧的 pending 事件，再检索相关记忆，最后把当前问题记录进 raw inbox
  - 输出 `context_markdown`，可以直接拼到回答前的上下文里
- `scripts/memory_runtime.py finalize-turn`
  - 回合结束后使用
  - 会先记录助手回复，再按阈值或间隔触发一次增量整理
- `scripts/memory_runtime.py remember`
  - 用户明确说“记住这个”时使用
  - 会先记录 raw event，再直接写入结构化记忆，并回写来源映射
- `scripts/memory_runtime.py record-event`
  - 宿主想把任意用户消息、助手消息、日志、外部事件先落到 raw inbox 时使用

### 回答前准备上下文

```text
python scripts/memory_runtime.py prepare-context --store D:\memory-data --subject-id me --subject-name 我 --session-id session-20260413 --query "我最近的睡眠状态有什么值得注意的吗？"
```

### 回合结束后记录回复

```text
python scripts/memory_runtime.py finalize-turn --store D:\memory-data --subject-id me --subject-name 我 --session-id session-20260413 --reply "最近睡眠里最值得注意的是它和晚饭时间可能有关，但目前还只是候选观察。"
```

### 显式写入记忆

```text
python scripts/memory_runtime.py remember --store D:\memory-data --subject-id me --subject-name 我 --title 回答风格偏好 --content "长期更喜欢先给结论，再给解释。" --use-underlying-kind
```

### 仅记录原始事件

```text
python scripts/memory_runtime.py record-event --store D:\memory-data --subject-id me --subject-name 我 --session-id session-20260413 --source-type note --topic-hint sleep-observation --domain-hint health --content "最近两周睡眠可能和晚饭时间有关，但还需要继续观察。"
```

## Markdown 和数据库如何分工

这套系统现在是双层存储：

- Markdown 负责高价值、稳定、常用、可人工阅读的记忆
  - `profile`
  - `states`
  - `events`
  - `relationships`
  - `goals`
  - `domains`
  - `sessions`
  - `candidates`
- SQLite 负责原始事件、处理状态、索引、命中统计、来源映射
  - `raw_events`
  - `maintenance_cursor`
  - `memory_sources`
  - `documents`
  - `scores`
  - `retrieval_log`

换句话说：

- 常用规则和稳定画像写进 Markdown
- 原始材料先入数据库
- 需要证据、追溯、时间过滤、主题过滤时，再查数据库

## 按时间和主题下钻原始层

回答问题时，默认还是先用 `retrieve_memories.py` 取长期记忆。
如果需要回看“某段时间里、某个主题下到底发生过什么”，再查 `raw_events`：

```text
python scripts/search_raw_events.py --store D:\memory-data --subject-id me --domain health --topic sleep --since 2026-04-01 --until 2026-04-30 --limit 10
```

也可以直接带 query 做交叉筛选：

```text
python scripts/search_raw_events.py --store D:\memory-data --subject-id me --query "睡眠 晚饭 时间" --processed-state pending --limit 5
```

这个脚本会返回：

- 原始事件 id
- 时间
- topic/domain hint
- 当前处理状态
- 是否已经整理到正式记忆
- 摘要片段

### 初始化

```text
python scripts/init_memory_store.py --store D:\memory-data
```

这一步现在是可选的。
如果你直接调用 `memory_runtime.py`、`write_memory.py`、`ingest_raw_event.py` 或检索脚本，store 也会在首次使用时自动创建。

### 写入一条候选记忆

```text
python scripts/write_memory.py --store D:\memory-data --kind candidate --subject-id me --subject-name 我 --title 最近压力偏高 --content "最近两周工作压力偏高，晚上容易继续想项目。"
```

### 一步式分类并写入

```text
python scripts/ingest_memory.py --store D:\memory-data --title 最近压力偏高 --content "最近两周工作压力偏高，晚上容易继续想项目。"
```

这个脚本会：

- 先调用 `classify_memory.py` 的规则判断应落哪层
- 生成最终写入 payload
- 自动调用写入逻辑

如果你想让“像状态但目前不确定”的内容直接先写候选，再以后人工提升，就用默认行为。
如果你明确要直接写入它的长期层，可以加：

```text
python scripts/ingest_memory.py --store D:\memory-data --title 睡眠观察 --content "最近睡眠可能和晚饭时间有关，但还需要继续观察。" --use-underlying-kind
```

### 先分类，再决定写入哪一层

```text
python scripts/classify_memory.py --title 最近压力偏高 --content "最近两周工作压力偏高，晚上容易继续想项目。"
```

这个脚本会输出：

- `recommended_kind`
- `underlying_long_term_kind`
- `recommended_domain`
- `recommended_status`
- `suggested_tags`
- `suggested_payload`

如果你想把分类结果保存成 JSON，再交给后续流程，可以用：

```text
python scripts/classify_memory.py --payload-file D:\raw-memory.json --out-file D:\classified-memory.json
```

### 写入一条稳定画像

```text
python scripts/write_memory.py --store D:\memory-data --kind profile --subject-id me --subject-name 我 --title 回答风格偏好 --content "长期更偏好先给结构化判断，再给解释。" --tag 偏好 --tag 沟通
```

### 按问题检索要加载的记忆

```text
python scripts/retrieve_memories.py --store D:\memory-data --subject-id me --query "我最近为什么总是停不下来，还在想项目？" --top-k 6
```

这个脚本会：

- 优先补进 `profile` 和 `state`
- 再按 query 从 `goal`、`relationship`、`event`、`domain`、`session` 中挑最相关的记忆
- 记录命中次数并更新轻量排序分数

### 把候选提升为正式长期记忆

```text
python scripts/promote_candidates.py --store D:\memory-data --candidate D:\memory-data\candidates\20260413-101500-sleep.md --target-kind state --title CurrentSleepState --domain health --topic sleep
```

这个脚本会：

- 读取候选记忆
- 写入目标长期层，例如 `profile` / `state` / `goal`
- 把原候选移到 `archive/imports/promoted/`
- 在正式记忆里保留 `related_sources` 追踪
- 自动重建索引和评分

### 批量检查候选池

```text
python scripts/review_candidates.py --store D:\memory-data --top-k 10
```

这个脚本会：

- 重新用分类规则评估每条候选
- 给出 `promote_now` / `review_after_verification` / `keep_candidate`
- 给出 `suggested_target_kind`
- 按 `promotion_score` 排序，方便你先看最值得整理的候选

### Windows 中文输入建议

如果命令行里直接传中文内容不稳定，优先改用文件输入：

```text
python scripts/classify_memory.py --payload-file D:\raw-memory.json --out-file D:\classified-memory.json
python scripts/ingest_memory.py --store D:\memory-data --payload-file D:\payload.json
python scripts/write_memory.py --store D:\memory-data --payload-file D:\payload.json
python scripts/retrieve_memories.py --store D:\memory-data --query-file D:\query.txt --subject-id me
python scripts/ingest_raw_event.py --store D:\memory-data --payload-file D:\raw-event.json
python scripts/search_raw_events.py --store D:\memory-data --subject-id me --query-file D:\query.txt
```

`payload.json` 里可以直接放：

```json
{
  "title": "睡眠观察",
  "kind": "candidate",
  "subject_id": "me",
  "subject_name": "我",
  "domain": "health",
  "topic": "sleep",
  "content": "最近两周入睡偏晚，连续三天凌晨一点后才睡。",
  "tags": ["健康", "作息"]
}
```

## 关键文件

优先阅读：

- `SKILL.md`
- `references/memory-system.md`
- `references/loading-rules.md`
- `references/writeback-rules.md`
- `references/classification-draft.md`
- `references/network-memory-model.md`
- `references/slotting-guide.md`
- `references/topic-catalog.md`

样板记忆入口在：

- `references/memory/index.md`

维护脚本在：

- `scripts/init_memory_store.py`
- `scripts/classify_memory.py`
- `scripts/ingest_memory.py`
- `scripts/write_memory.py`
- `scripts/retrieve_memories.py`
- `scripts/memory_runtime.py`
- `scripts/ingest_raw_event.py`
- `scripts/search_raw_events.py`
- `scripts/review_candidates.py`
- `scripts/promote_candidates.py`
- `scripts/reindex_memory.py`
- `scripts/normalize_candidates.py`
- `scripts/merge_duplicates.py`
- `scripts/score_memories.py`
- `scripts/run_maintenance.py`
- `scripts/run_heartbeat.py`
- `scripts/heartbeat_service.py`
