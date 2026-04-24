# Meta Memory

面向 Codex / 智能体的本地优先、可解释、低上下文长期记忆系统。

Local-first, deterministic memory for Codex agents.

Meta Memory 解决的是一个很实际的问题：智能体如果没有长期记忆，每次对话都像第一次见你；但如果把所有历史都塞进上下文，又会慢、贵、混乱，还容易把无关旧信息带进当前回答。

所以这个项目不是一个“历史记录文件夹”，也不是简单 RAG。它更像一个给智能体用的记忆系统：先保存原始经历，再把稳定、重要、可复用的信息整理成结构化记忆，回答问题时只联想到当前最相关的一小部分。

In short: Meta Memory is a local runtime that records raw events, compiles durable memories into Markdown, and retrieves only the relevant context for each turn.

适合这些场景：

- 你希望智能体长期记住一个人、一个项目、一个客户或一个持续任务。
- 你希望它每次回答前能自动想起相关背景，但不要读取全部历史。
- 你希望记忆能追溯来源、能被人工审计、能被修正和替换。
- 你不想把 embedding 当成唯一召回方式，希望召回过程更稳定、更可解释。

## Why

普通聊天记录、RAG、embedding 搜索都能“找东西”，但个人长期记忆需要更严格的规则。原因很简单：

- **不能什么都记成长期记忆**：一次闲聊、猜测、临时想法，不应该污染长期层。
- **不能每次读全部历史**：记忆越多，上下文越大，回答越慢，也越容易跑偏。
- **不能只靠模糊语义搜索**：embedding 有用，但它不总是稳定，尤其是你想精确找回某条关键记忆时。
- **必须保留来源**：长期记忆应该能追溯到原始对话或事件，方便以后修正。
- **必须能解释召回原因**：系统应该能告诉你为什么这条记忆被找出来，是标题命中、全文命中，还是通过某个主题联想到的。

Meta Memory 采用的是“确定性优先”的路线：字段权重、SQLite FTS/BM25、显式关联、多跳联想、生命周期、重要性排序和召回评测。embedding 以后可以作为可选兜底，但不是默认主路径。

## 中文概览

### 一句话理解

你可以把它想成三层：

1. **原始记录层**：先把对话、事件、回复原封不动记下来，相当于“经历”。
2. **整理记忆层**：把真正稳定、有价值的信息整理成 Markdown，相当于“长期记忆”。
3. **联想检索层**：回答前根据当前问题，只找出相关的少量记忆，相当于“想起来”。

这和人脑比较像：人不会每次回答问题都回放一生经历，而是先想到几个相关主题，再顺着人物、事件、时间、目标继续联想。

### 它具体做什么

- 回答前运行 `prepare-context`，只返回相关的 `context_markdown`。
- 回答后运行 `finalize-turn`，记录用户/助手原始事件。
- 用户明确要求“记住”时运行 `remember`，写入结构化长期记忆。
- 默认保守写回，普通对话先进入 `session` 或 `candidate`。
- 不默认依赖 embedding，主召回路径是字段权重、SQLite FTS/BM25、关联扩展、生命周期和重要性排序。

### 它怎么“联想”

当你问一个问题时，它不会全量读取 `memory-data/`，而是按下面顺序找：

1. 先按 `subject-id` 限定范围，比如 `person:me`、`project:meta-memory`。
2. 再查标题、主题、标签、摘要、正文、人物、事件、来源。
3. 如果命中了一条记忆，再沿着 `related_topics`、`related_people`、`related_events`、`related_sources` 扩展 1-2 跳。
4. 最后按重要性、状态、是否过期、是否被替代、最近命中情况排序。

这样做的目标是：既尽量不漏掉相关记忆，又不把无关历史塞进上下文。

### 记忆层

- `profile`: 稳定身份、长期偏好、固定风格
- `states`: 当前状态、近期阶段变化
- `events`: 关键事件、时间线、转折点
- `relationships`: 重要人物、关系模式、沟通边界
- `goals`: 长期目标、项目、约束
- `domains`: 工作、学习、健康、财务、日常等领域经验
- `sessions`: 当前会话和短期任务状态
- `candidates`: 未验证、待观察、可能冲突的信息
- `archive`: 原始来源和导入材料

### 快速开始

第一次运行会自动创建：

- `memory-data/`: 默认本地记忆目录
- `memory-data/db/memory_index.sqlite`: 检索、来源、状态索引

最常用的流程只有三步。

第一步，回答前准备记忆上下文：


```bash
python scripts/memory_runtime.py prepare-context \
  --subject-id person:me \
  --subject-name 我 \
  --session-id session-20260424 \
  --query-file query.txt
```

只使用返回 JSON 里的 `context_markdown`。不要把完整 JSON、`memory-data/`、`references/` 或单个记忆文件全量塞进模型上下文。

第二步，回答后记录助手回复：

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
