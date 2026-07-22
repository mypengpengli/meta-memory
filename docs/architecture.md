# Runtime architecture

Meta Memory is CLI-first and can run locally or behind one central HTTP/HTTPS
service. Its normal path is deliberately small at the Agent boundary and
durable after that boundary:

```text
Agent
  → before (durable Turn + bounded context)
  → response
  → after (durable completion / local spool fallback)
  → dirty scope queue
  → incremental heartbeat
  → Session Card → memory intent gate → Claim / review inbox → projections
```

Remote Agents use the same lifecycle through a generated dependency-free
client. The request supplies a stable workspace and subject; the server never
uses its cwd to identify a remote project. The client keeps exact-answer Turn
receipts and an outbox so an ambiguous network acknowledgement does not lose or
rewrite the response. Activity, state, spatial observation, and map writes use
the same durable JSON outbox pattern; binary upload uses a separate resumable
receipt tied to the unchanged source file.

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
- `http_api` authenticates and binds remote identity, then delegates to the
  same runtime services instead of reimplementing memory semantics.
- `shared_memory` owns audiences, curated cross-workspace activity, current
  time-bounded state, and bounded shared context.
- `spatial` owns content-addressed binary assets, immutable map versions, and
  searchable semantic observations. SQLite stores metadata; raw bytes live
  below the store's `assets/objects` directory. Scope bindings remain distinct
  even when identical bytes deduplicate to one physical object.

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

## Shared-world flow

```text
robot/device observation
  → raw asset (optional, SHA-256 object storage)
  → immutable map version (optional)
  → timestamped semantic observation / temporal state / curated activity
  → audience channel
  → bounded shared_context during a relevant remote before
  → raw asset download only on demand
```

This path is adjacent to Claims: it does not widen existing Claim visibility or
turn sensor telemetry into permanent user facts. Expiring state such as
`last_seen` is excluded after its validity window; newer state supersedes older
state, and out-of-order delivery cannot replace newer truth.

Perception and navigation are upstream responsibilities. Meta Memory stores the
caption, OCR, recognized-object array, map metadata, provenance, time,
confidence, and asset links supplied by a robot/model; it does not run vision,
SLAM, map fusion, localization, or route planning itself.
