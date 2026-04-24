---
name: memory-orchestrator
description: Prepare and persist scoped per-person, per-project, or per-subject memory for Codex conversations. Use when an agent must load only relevant profile/state/goal/relationship/event/domain/session memories before answering, record user and assistant turns after answering, explicitly remember user-requested facts, preserve raw evidence, or maintain a compiled Markdown memory/wiki without bulk-loading memory files. 为具体人物、项目或主体准备记忆上下文、记录对话事件、保守写回 session/candidate，并在用户明确要求时沉淀长期记忆；适合需要自动加载相关记忆、自动记录回合、按 scope 隔离记忆、避免全量读取和误污染长期层的场景。
---

# Memory Orchestrator

Treat this skill as a per-turn memory runtime, not as a folder to browse manually. / 把它当作每回合前后置运行时，不要默认展开记忆目录。

## Runtime Contract

1. Before answering, run `scripts/memory_runtime.py prepare-context`.
   - Pass `--subject-id`, `--subject-name`, `--session-id`, and the current user request.
   - Prefer `--query-file` for Chinese, multiline text, quotes, or host-provided content.
   - Use only the returned `context_markdown` as memory context.
   - Do not inject full `retrieved`, `raw_evidence`, `memory-data/`, or `references/` into the reply context unless debugging.
   - Retrieval is deterministic by default: weighted fields + SQLite FTS/BM25 + 1-hop association expansion. Embeddings are not required.
2. Answer the user with current facts first, using retrieved memories only when relevant.
3. After answering, run `scripts/memory_runtime.py finalize-turn`.
   - Record the assistant reply with `--reply-file` when possible.
   - Let the runtime organize conservatively.
4. When the user explicitly says to remember/save a fact, run `scripts/memory_runtime.py remember`.
   - Use `--title-file`, `--content-file`, or `--payload-file` for nontrivial text.
   - Add `--use-underlying-kind` when accepting the classifier's long-term kind.
5. Use `finalize-turn --capture-artifact` only when the assistant reply itself is durable knowledge worth filing.

Default store: `memory-data/` under this skill directory. Default index: `memory-data/db/memory_index.sqlite`.

## Scope Model

- Treat `--subject-id` as the memory scope/container. Use a stable id such as `person:lp`, `project:meta-memory`, or `client:acme`.
- Treat `--session-id` as short-lived conversation or task state, not as long-term identity.
- Put stable identity and preferences in `profile`; put current or time-bounded state in `states`; put task progress in `sessions`.
- Link graph-like clues with `--related-person`, `--related-event`, `--related-topic`, and `--related-source` when writing explicit memories.

## Recall Model

- Direct recall: title, topic, tags, summary, relationship fields, and indexed body text.
- Associative recall: after a direct hit, expand through shared people, events, topics, and sources for up to `--expand-hops` hops.
- Lifecycle ranking: active and recently useful memories rise; ended, superseded, or replaced memories fall or disappear.
- Importance ranking: `importance` is stored per memory and participates in ranking; use higher values only for durable, high-impact facts.
- Keep `--top-k` small for prompt context; use `--candidate-pool` only to widen internal ranking.
- Do not add embedding retrieval as the primary path unless the user explicitly chooses it; if added later, treat it as an optional fallback.

## Writeback Guardrails

- Always preserve raw evidence first in `raw_events`.
- Automatic organization defaults to `session` or `candidate`.
- Long-term layers (`profile`, `states`, `events`, `relationships`, `goals`, `domains`) should come from explicit `remember`, validated promotion, or intentional artifact capture.
- Keep raw evidence append-only; update compiled memories by appending, replacing, or using `supersedes` / `replaced_by`.
- Never promote a normal user question, one-off chat, unverified guess, or long raw transcript directly into long-term memory.
- If unsure, keep it as `candidate` until later evidence confirms it.

## Context Budget Rules

- The skill trigger loads this `SKILL.md`; it should be enough for normal use.
- Runtime scripts may be executed without reading their source.
- `prepare-context` is the default retrieval surface; it returns positive-match memories in `context_markdown`.
- Do not read generated `memory-data/index.md`, `memory-data/log.md`, `memory-data/sources.md`, or individual memory files unless `context_markdown` is insufficient.
- Do not read `README.md` during normal skill use; it is for humans on GitHub.

## Optional References

Read at most one reference file at a time, only when the runtime result is insufficient or you are auditing behavior.

- `references/reference-map.md`: choose which reference is relevant.
- `references/loading-rules.md`: decide which memory layer to inspect manually.
- `references/writeback-rules.md`: decide where new information belongs.
- `references/memory/index.md`: inspect top-level memory page conventions.
- `references/memory-system.md`: inspect the raw-source plus compiled-memory architecture.

Stop reading references as soon as you know the next action.

## Maintenance

- Run `scripts/run_maintenance.py` to rebuild indexes, scores, generated views, and lint checks.
- Run `scripts/lint_memory.py` when auditing for missing sources, accidental long-term promotion, duplicate canonical pages, or stale structure.
- Run `scripts/evaluate_retrieval.py --cases-file <cases.json>` when auditing whether important queries still recall expected memories.
- Generated views are navigation aids only:
  - `memory-data/index.md`
  - `memory-data/log.md`
  - `memory-data/sources.md`
