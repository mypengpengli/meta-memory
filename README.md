# Meta Memory

一句话：**Meta Memory 是一套给 Codex / AI 智能体用的本地记忆工具。**

它要解决的问题很简单：你希望智能体真的“记得你”，但又不想每次把所有聊天记录、项目资料、旧文件全部塞进上下文。

它主要做两件事：

1. **回答前找出来**：你提问时，它只取出和当前问题有关的少量记忆，让智能体参考，不读取全部历史。
2. **回答后记下来**：把这轮对话事件保存下来；如果你明确说“记住”，再把稳定、有用的信息写成长期记忆。

普通说法就是：**该记的记住，该查的查出来，不该读的别塞进上下文。**

它适合你在这些情况下使用：

- 你长期让 Codex 帮你做同一个项目，不想每次重新解释项目背景。
- 你希望它记住你的偏好、写作风格、工作习惯、常用约束。
- 你希望它记住一个客户、一个人、一个产品、一个家庭事务或一段长期学习计划。
- 你希望它回答前能想起相关背景，但不要把所有旧内容都读进来。
- 你希望记忆存在本地 Markdown 和 SQLite 里，能看、能改、能追溯来源。
- 你不想只依赖 embedding，因为你需要更稳定、更可解释的召回。

英文简版在后面。前半部分先用中文把它讲清楚。

## 先说清楚：这是什么

Meta Memory 不是一个聊天记录备份器。

它也不是“把所有历史资料丢给 AI，然后让 AI 自己找”。

它是一套**本地脚本 + Markdown 记忆文件 + SQLite 检索索引**：

- 脚本负责读写记忆。
- Markdown 文件保存人能看懂、能修改的长期记忆。
- SQLite 索引负责快速查找，不用每次扫完整个目录。

每轮对话前后，智能体可以调用这些脚本，完成两件事：回答前读取相关记忆，回答后记录新事件。

最小工作方式是这样：

1. 你问问题。
2. 智能体先调用 `prepare-context`。
3. Meta Memory 根据你的问题，从本地记忆里找出相关内容。
4. 智能体只读取返回结果里的 `context_markdown`。
5. 智能体回答你。
6. 回答后调用 `finalize-turn`，保存这轮发生了什么。
7. 如果你明确说“记住这件事”，再调用 `remember` 写入长期记忆。

这就是它的核心：**不是让模型凭印象记，也不是把历史全塞给模型，而是让模型先从本地文件和索引里查到该看的那一小部分。**

需要说明清楚：Meta Memory 本身不是魔法开关，不会让所有 AI 自动拥有长期记忆。它提供的是一套本地记忆工具和 Codex skill 说明。智能体要按 `SKILL.md` 调用这些脚本；如果你手动使用，也可以直接运行下面的命令。

## 它到底能做什么

### 1. 让智能体下次不用从零开始

比如你告诉智能体：

> 我这个项目叫 meta-memory，目标是做一个 Codex 用的长期记忆 skill，不想依赖 embedding 作为默认召回。

以后你再说：

> 继续优化这个记忆项目的 README。

它应该能先找出和 `meta-memory`、`README`、`memory skill`、`embedding` 有关的记忆，再把这些摘要交给智能体，而不是让你重新解释一遍。

### 2. 把“原始记录”和“长期记忆”分开

这点很重要。

一次对话里可能有很多内容：问题、尝试、猜测、临时想法、错误判断、最终决定。它们不应该全部变成长期记忆。

Meta Memory 会先保存原始事件。真正稳定、重要、以后还会用到的信息，才适合写进长期记忆。

这样做的好处是：

- 原始记录还在，需要追溯时能查。
- 长期记忆保持干净，不会被普通闲聊污染。
- 以后发现旧记忆错了，可以替换、废弃、标记来源。

### 3. 回答前只读取相关记忆

它不会默认把 `memory-data/` 整个目录塞给智能体。

它会先按 `subject-id` 限定范围。比如：

- `person:me`: 你的个人记忆
- `project:meta-memory`: 这个项目的记忆
- `client:acme`: 某个客户的记忆

然后再查标题、主题、标签、摘要、正文、相关人物、相关事件、相关来源。

如果找到一条相关记忆，它还可以顺着显式关联继续找一两步。比如：

- 这个项目关联到 `README`
- `README` 关联到 `Codex skill`
- `Codex skill` 关联到 `trigger rules`

这样就能做到比较接近“顺着主题想起来”，但又不是把所有东西都读出来。

### 4. 不把 embedding 当成默认答案

embedding 可以有用，但它不是这个项目的默认主路径。

原因很实际：很多时候你要找的是一条具体记忆，不是“语义上差不多”的一堆内容。只靠 embedding，可能会漏掉关键词明确但语义分数不高的内容，也可能召回看起来相关但实际没用的内容。

所以 Meta Memory 默认先用更可解释的方法：

- 字段匹配：标题、标签、主题、人物、事件、来源
- 全文检索：SQLite FTS/BM25
- 显式关联：`related_topics`、`related_people`、`related_events`、`related_sources`
- 重要性排序：重要记忆优先
- 生命周期：过期、替换、废弃的记忆降权或不返回

## 记忆怎么分层

长期记忆不是一个大杂烩。默认按用途分层：

- `profile`: 稳定身份、长期偏好、固定风格
- `states`: 当前状态、近期阶段变化
- `events`: 关键事件、时间线、转折点
- `relationships`: 重要人物、关系模式、沟通边界
- `goals`: 长期目标、项目、约束
- `domains`: 工作、学习、健康、财务、日常等领域经验
- `sessions`: 当前会话和短期任务状态
- `candidates`: 未验证、待观察、可能冲突的信息
- `archive`: 原始来源和导入材料

这套分层的目的不是复杂，而是避免所有记忆混在一起。稳定身份和临时任务不应该放在同一个地方；已验证事实和待确认猜测也不应该放在同一个地方。

## 快速开始

第一次运行会自动创建：

- `memory-data/`: 默认本地记忆目录
- `memory-data/db/memory_index.sqlite`: 检索、来源、状态索引

最常用的流程只有三步：回答前读取、回答后记录、明确要求时写入长期记忆。

第一步，回答前读取相关记忆：

```bash
python scripts/memory_runtime.py prepare-context \
  --subject-id person:me \
  --subject-name 我 \
  --session-id session-20260424 \
  --query-file query.txt
```

只使用返回 JSON 里的 `context_markdown`。不要把完整 JSON、`memory-data/`、`references/` 或单个记忆文件全量塞进模型上下文。

第二步，回答后记录这轮回复：

```bash
python scripts/memory_runtime.py finalize-turn \
  --subject-id person:me \
  --subject-name 我 \
  --session-id session-20260424 \
  --reply-file reply.txt
```

第三步，用户明确要求“记住”时，再写入长期记忆：

```bash
python scripts/memory_runtime.py remember \
  --subject-id person:me \
  --subject-name 我 \
  --title-file title.txt \
  --content-file memory.txt \
  --related-topic answer-style \
  --importance 0.9 \
  --use-underlying-kind
```

中文、多行文本、引号较多的内容，优先使用 `--query-file`、`--reply-file`、`--title-file`、`--content-file` 或 `--payload-file`，避免 shell 编码和转义问题。

## Recall Model

Meta Memory currently uses an explainable, non-embedding default recall path:

1. **Scope filter**: `--subject-id` isolates memory by person, project, client, or other container.
2. **Direct field match**: title, topic, tags, summary, people, events, topics, sources.
3. **Full-text recall**: SQLite FTS/BM25 indexes title, summary, body text, and relation fields.
4. **Associative expansion**: after a direct hit, retrieval expands through shared `related_people`, `related_events`, `related_topics`, and `related_sources`.
5. **Lifecycle ranking**: active memories rise; ended, superseded, or replaced memories fall or disappear.
6. **Importance ranking**: each memory has `importance`; durable high-impact facts outrank ordinary notes.

You can widen internal recall without increasing final context:

```bash
python scripts/memory_runtime.py prepare-context \
  --subject-id person:me \
  --subject-name 我 \
  --session-id session-20260424 \
  --query-file query.txt \
  --candidate-pool 32 \
  --expand-hops 2
```

The final prompt remains controlled by `--top-k` and `context_markdown`.

## Writeback Policy

Default behavior is conservative:

- Raw events are preserved first in `raw_events`.
- Automatic organization writes to `session` or `candidate` by default.
- Long-term layers should come from explicit `remember`, validated promotion, or intentional artifact capture.
- Use `supersedes` / `replaced_by` or `status: superseded` for conflicts and replacements.
- Do not promote normal user questions, one-off chat, unverified guesses, or long transcripts directly into long-term memory.

## As A Codex Skill

`SKILL.md` is the agent-facing runtime contract. This `README.md` is for humans.

When the skill triggers, the agent should:

- Read `SKILL.md`.
- Run `scripts/memory_runtime.py prepare-context` before answering.
- Use only `context_markdown` as memory context.
- Run `scripts/memory_runtime.py finalize-turn` after answering.
- Run `scripts/memory_runtime.py remember` only for explicit durable memory.

The agent should not load these by default:

- `README.md`
- `references/`
- `memory-data/`
- individual memory Markdown files
- full runtime JSON diagnostics

## Maintenance

Run the standard maintenance sequence:

```bash
python scripts/run_maintenance.py
```

This rebuilds indexes, scores, generated views, and lint checks.

Run lint only:

```bash
python scripts/lint_memory.py
```

Compile scripts:

```bash
python -m compileall scripts
```

Validate the skill folder:

```bash
python <path-to-skill-creator>/scripts/quick_validate.py .
```

## Retrieval Evaluation

To stop guessing whether recall is good, create retrieval cases:

```json
[
  {
    "name": "answer style",
    "query": "how should answers be structured",
    "subject_id": "person:me",
    "must_include": ["answer style preference"],
    "must_not_include": ["obsolete"]
  }
]
```

Run:

```bash
python scripts/evaluate_retrieval.py --cases-file retrieval-cases.json --strict
```

The evaluator reports `recall_at_k`, selected titles, missing expectations, and unexpected matches. This is the recommended way to improve recall over time.

## Repository Layout

```text
SKILL.md                  Agent-facing runtime contract
agents/openai.yaml        UI metadata
scripts/                  Runtime, indexing, retrieval, writeback, lint, evaluation
references/               On-demand design and policy references
assets/templates/         Memory note templates
memory-data/              Default local memory store, git-ignored
```

Generated views inside `memory-data/`:

- `index.md`: compiled memory navigation
- `log.md`: raw event timeline
- `sources.md`: source layer and memory-source mapping

## English Quick Reference

### What It Is

Meta Memory is a local memory runtime for agents. It records raw events, writes durable memories into structured Markdown, and retrieves a small relevant context before each answer.

### Core Commands

```bash
python scripts/memory_runtime.py prepare-context --subject-id person:me --subject-name "Me" --session-id session-1 --query-file query.txt
python scripts/memory_runtime.py finalize-turn --subject-id person:me --subject-name "Me" --session-id session-1 --reply-file reply.txt
python scripts/memory_runtime.py remember --subject-id person:me --subject-name "Me" --title-file title.txt --content-file memory.txt --use-underlying-kind
```

### Design Choices

- `subject-id` is the memory scope/container.
- `profile` stores stable identity and preferences.
- `states` stores recent dynamic state.
- `sessions` stores short-lived task state.
- Raw events are append-only evidence.
- Compiled memories are updated through append, replace, `supersedes`, or `replaced_by`.
- Retrieval is deterministic by default: weighted fields, SQLite FTS/BM25, association expansion, lifecycle ranking, and importance ranking.
- Embeddings are optional future fallback, not the primary recall path.

## Design Influences

- Andrej Karpathy's [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f): compile durable knowledge instead of repeatedly rereading raw material.
- [supermemory](https://github.com/supermemoryai/supermemory): scoped memory and tool-first retrieval.
- [mem0](https://github.com/mem0ai/mem0): User/Session/Agent memory split and retrieve-generate-store loop.

Meta Memory keeps those ideas local-first, dependency-light, and inspectable.
