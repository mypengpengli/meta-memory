# Runtime architecture

Meta Memory is a local, CLI-first runtime.  Its normal path is deliberately
small at the agent boundary and durable after that boundary:

```text
Agent
  → before (durable Turn + bounded context)
  → response
  → after (durable completion / local spool fallback)
  → dirty scope queue
  → incremental heartbeat
  → Session Card → memory intent gate → Claim / review inbox → projections
```

The public concepts are **user**, **project**, and **session**.  A user-level
Claim is stored in the profile-wide `global` scope; project Claims and completed
session summaries live in a workspace scope.  Agent IDs are provenance,
idempotency and continuity metadata rather than ordinary-memory partitions.

## Service boundaries

The public CLI is organized as small lifecycle services rather than exposing
SQLite tables directly:

- `runtime` owns `before`, `after`, explicit `remember`, and correction.
- `ux_memory`, `ux_inbox`, `ux_projects`, and `ux_history` own human-facing
  lifecycle actions over authoritative records.
- `turn_service` owns durable Turn leases; a long task can touch, reopen, or
  complete a late Turn without silently losing its evidence.
- `maintenance` owns only derived, dirty work: cards, review jobs, projections,
  Hot Memory, retention and optional compaction.
- `dream` owns source-linked, deferred synthesis.  Empty or unchanged sources
  produce `idle`, never placeholder prompt nodes.

Legacy `scripts/` remain the data-plane compatibility layer while public
services use explicit request values and a common `AppConfig` scope.  This lets
existing integrations keep their old command contracts while new lifecycle
features have stable, discoverable APIs.

## Retrieval and runtime cost

Retrieval is two-stage: FTS/chunk/entity/basic-memory sources produce a bounded
candidate set, then only those documents are final-ranked.  Session summaries
and imported resource chunks have their own optional FTS indexes with a LIKE
fallback for SQLite builds without FTS5.  Project root and remote identity are
resolved once per command and reused by audit/status code.

The database opens through a schema fast path after migrations are current.
Expensive migration/FTS reconciliation is reserved for a new schema or an
explicit repair path.  Heartbeats use dirty scopes and generation keys so one
batch of writes builds one Hot Memory snapshot per affected scope.

## Memory quality rules

Automatic extraction first classifies text as `ignore`, `session_only`, or
`long_term_candidate`.  Explicit `remember` always remains explicit.  Pending
or ambiguous changes are visible in `meta-memory inbox`; they do not become an
invisible backlog.  A continuation request may add a bounded set of completed
cross-Agent summaries, but other Agents' detailed transcript remains an
explicit history operation.
