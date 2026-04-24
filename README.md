# Meta Memory

**中文** | **English**

Meta Memory is a portable Codex skill and local runtime for long-term memory around a specific person or subject.

它不是把所有历史一次性塞进上下文的 RAG 文件夹，而是一个“原始事件层 + 编译后的 Markdown 记忆层 + 运行时规则”的记忆系统。设计灵感来自 Andrej Karpathy 的 [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)：知识应该持续沉淀成可维护的结构，而不是每次提问都从原始材料重新拼接。

It follows the same core idea: raw sources stay as evidence, the agent maintains a compact compiled wiki, and each query reads only the pages needed for the current answer.

## 中文说明

### 它解决什么

- 每轮对话前自动准备相关记忆上下文。
- 每轮对话后记录原始用户/助手事件。
- 用户明确要求“记住”时，把信息写入结构化长期记忆。
- 默认保守写回，避免普通闲聊或未验证猜测污染长期层。
- 通过脚本检索少量正相关记忆，避免全量读取 `memory-data/`。

### 工作方式

1. `raw_events` 保存原始证据和对话事件。
2. Markdown 记忆页保存稳定、可复用、可人工审计的事实。
3. SQLite 索引保存检索、命中、来源映射和处理状态。
4. `prepare-context` 在回答前整理旧事件并返回 `context_markdown`。
5. `finalize-turn` 在回答后记录助手回复并保守整理。
6. `remember` 只在用户明确要求时写入长期记忆。

关键设计取舍：

- `subject-id` 是隔离记忆的 scope/container，可以是 `person:lp`、`project:meta-memory`、`client:acme`。
- `profile` 存稳定身份和偏好，`states` 存近期状态，`sessions` 存本轮任务状态。
- 原始事件尽量 append-only；稳定 Markdown 记忆可以通过追加、替换、`supersedes` / `replaced_by` 表达更新。
- 显式记忆可以带 `related_people`、`related_events`、`related_topics`、`related_sources`，检索会利用这些链接信号。
- 默认不依赖 embedding；主路径是字段权重、SQLite FTS/BM25、关联扩展和生命周期排序。

默认记忆层：

- `profile`: 身份、长期偏好、稳定风格
- `states`: 当前状态、近期阶段变化
- `events`: 关键事件和时间线
- `relationships`: 重要人物、关系模式、边界
- `goals`: 长期目标、项目、约束
- `domains`: 工作、学习、健康、财务、日常等领域经验
- `sessions`: 当前会话和短期任务状态
- `candidates`: 未验证、待观察、可能冲突的信息
- `archive`: 原始来源和导入材料

### 快速开始

第一次运行脚本会自动创建默认数据目录：

- Markdown: `memory-data/`
- SQLite: `memory-data/db/memory_index.sqlite`

回答前准备上下文：

```bash
python scripts/memory_runtime.py prepare-context \
  --subject-id me \
  --subject-name 我 \
  --session-id session-20260424 \
  --query-file query.txt
```

使用返回 JSON 里的 `context_markdown`，不要把完整 JSON 诊断、整个 `memory-data/` 或 `references/` 全量塞入上下文。

如果想扩大内部联想但不增加最终上下文，可以调大候选池或关联跳数：

```bash
python scripts/memory_runtime.py prepare-context \
  --subject-id me \
  --subject-name 我 \
  --session-id session-20260424 \
  --query-file query.txt \
  --candidate-pool 32 \
  --expand-hops 2
```

回答后记录回复：

```bash
python scripts/memory_runtime.py finalize-turn \
  --subject-id me \
  --subject-name 我 \
  --session-id session-20260424 \
  --reply-file reply.txt
```

用户明确要求记住时：

```bash
python scripts/memory_runtime.py remember \
  --subject-id me \
  --subject-name 我 \
  --title-file title.txt \
  --content-file memory.txt \
  --related-topic answer-style \
  --use-underlying-kind
```

中文、多行文本、引号较多的内容，优先使用 `--query-file`、`--reply-file`、`--title-file`、`--content-file` 或 `--payload-file`，避免 shell 编码和转义问题。

### 作为 Codex Skill 使用

把仓库作为一个 skill 放到 Codex 可发现的 skills 目录后，代理会先看到 `SKILL.md` 的 `name` 和 `description`。触发后默认只需要读取 `SKILL.md`，正常工作时运行 `scripts/memory_runtime.py` 即可。

自动加载边界：

- 会加载：`SKILL.md`
- 会执行：`scripts/memory_runtime.py`
- 默认使用：`prepare-context` 返回的 `context_markdown`
- 不默认读取：`README.md`、`references/`、`memory-data/`、单个记忆 Markdown 文件

只有在运行时返回的信息不够、需要人工审计读取顺序、或排查写回分类问题时，才按需读取 `references/` 中的一份文件。

### 维护

```bash
python scripts/run_maintenance.py
```

它会重建索引、评分和生成视图，并运行 lint。

也可以单独检查：

```bash
python scripts/lint_memory.py
```

生成视图：

- `memory-data/index.md`: 正式记忆导航
- `memory-data/log.md`: 原始事件时间线
- `memory-data/sources.md`: 来源层和正式记忆层的映射

## English

### What It Does

Meta Memory gives an agent a disciplined memory loop:

- Load only relevant memories before answering.
- Record raw user and assistant turns after answering.
- Save explicit user-requested facts into structured long-term memory.
- Keep automatic writeback conservative.
- Avoid bulk-loading memory folders into the model context.

### Architecture

The system has three layers:

- Raw events: immutable evidence from conversations and imports.
- Compiled Markdown memory: stable, reviewable pages the agent maintains over time.
- Runtime index: SQLite tables for search, scores, sources, and processing state.

Design choices:

- `subject-id` is the memory scope/container, for example `person:lp`, `project:meta-memory`, or `client:acme`.
- `profile` is static identity and preference memory; `states` is recent dynamic state; `sessions` is short-lived task state.
- Raw events are append-only evidence; compiled memories may be appended, replaced, or linked through `supersedes` / `replaced_by`.
- Explicit memories can include `related_people`, `related_events`, `related_topics`, and `related_sources`; retrieval uses these link signals.
- Embeddings are not the default path; recall uses weighted fields, SQLite FTS/BM25, association expansion, and lifecycle ranking.

The runtime flow is:

1. `prepare-context` records the user request, organizes pending events conservatively, retrieves relevant memories, and returns `context_markdown`.
2. The agent answers using only relevant memory context.
3. `finalize-turn` records the assistant reply and optionally organizes the finished turn.
4. `remember` writes durable facts when the user explicitly asks the agent to remember something.

### Quick Start

Prepare context before the answer:

```bash
python scripts/memory_runtime.py prepare-context \
  --subject-id me \
  --subject-name "Me" \
  --session-id session-20260424 \
  --query-file query.txt
```

Record the assistant reply after the answer:

```bash
python scripts/memory_runtime.py finalize-turn \
  --subject-id me \
  --subject-name "Me" \
  --session-id session-20260424 \
  --reply-file reply.txt
```

Explicitly remember a durable fact:

```bash
python scripts/memory_runtime.py remember \
  --subject-id me \
  --subject-name "Me" \
  --title-file title.txt \
  --content-file memory.txt \
  --related-topic answer-style \
  --use-underlying-kind
```

Use file-based arguments for multiline text, non-ASCII text, quotes, or host-generated payloads.

### Context Discipline

During normal skill use, the agent should rely on `context_markdown` from `prepare-context`.

Do not load these by default:

- `README.md`
- `references/`
- `memory-data/`
- individual memory Markdown files
- full JSON diagnostics from runtime output

Read references only when debugging, auditing, or manually deciding a loading/writeback policy.

Retrieval is intentionally explainable. Runtime results expose `query_score`, `fts_score`, `association_score`, `lifecycle_score`, and `reasons` so missed or surprising recalls can be debugged without inspecting every memory file.

### Repository Layout

```text
SKILL.md                  Codex skill entrypoint
agents/openai.yaml        UI metadata
scripts/                  Runtime, indexing, retrieval, writeback, lint
references/               On-demand design and policy references
assets/templates/         Memory page templates
memory-data/              Default local memory store, git-ignored
```

### Validation

Compile scripts:

```bash
python -m compileall scripts
```

Validate the skill with Codex's skill validator:

```bash
python <path-to-skill-creator>/scripts/quick_validate.py .
```

Run maintenance:

```bash
python scripts/run_maintenance.py
```

## Design Notes

Meta Memory intentionally combines compiled Markdown memory with lightweight retrieval. The compiled layer makes knowledge compound across sessions; retrieval and strict context rules prevent the compiled layer from becoming a new source of context bloat.

The default policy is conservative: raw evidence is always preserved, candidates can be reviewed later, and long-term memory requires explicit or validated promotion.

The recall path intentionally avoids making embeddings mandatory. Embeddings can be useful as a semantic fallback, but personal memory needs deterministic, inspectable signals first: exact fields, full-text search, graph-like links, recency/usefulness, and explicit lifecycle metadata.

The design is informed by the scoped memory and tool-first retrieval patterns in [supermemory](https://github.com/supermemoryai/supermemory) and the User/Session/Agent memory split and retrieve-generate-store loop in [mem0](https://github.com/mem0ai/mem0), but this repository stays local-first and dependency-light.
