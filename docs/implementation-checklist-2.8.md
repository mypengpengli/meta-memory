# 2.8 remote/shared-world implementation checklist

This is the engineering checklist for the 2.8 remote Agent, household sharing,
and spatial-memory work. Implementation boxes describe the intended completed
surface; the final verification section must be rerun after the code and docs
freeze and must not be marked green before the corresponding command succeeds.

## Remote lifecycle and identity

- [x] Package the HTTP service so an installed wheel works outside the source tree.
- [x] Keep the former `scripts/memory_api.py` entry as a compatibility wrapper.
- [x] Bind every token to one profile and one stable Agent identity.
- [x] Require explicitly allowed stable workspace IDs; never infer a remote project from server cwd.
- [x] Optionally bound a token to subject/person IDs and audience/channel IDs.
- [x] Preserve old agents-file permissions while adding explicit turns/status/shared/assets/maps/spatial permissions.
- [x] Implement remote before, after, touch, status, and recovery routes.
- [x] Validate Turn profile/workspace/subject/Agent/session before touch or completion.
- [x] Preserve idempotent exact-answer completion and late completion.
- [x] Keep ordinary retrieve/event/remember/feedback/proposal routes compatible.
- [x] Prevent bounded tokens from acting on another subject's proposal or Claim.
- [x] Prevent feedback against another Agent's private Claim.
- [x] Validate channel and subject authorization before a remote Turn is persisted.
- [x] Include `state_subject_id` in subject allow-list enforcement.
- [x] Prevent JSON payload files from overriding launcher-bound Agent/workspace/audience/channel identity.
- [x] Generate/extend an installed-package server agents file without copying repository extras.
- [x] Add a dedicated pure-server Overview that does not require a local Agent Skill.

## Remote client and generated Skill

- [x] Generate a remote Skill, non-secret config, and Windows/POSIX launchers.
- [x] Accept HTTPS origins and allow HTTP only for localhost development.
- [x] Reject HTTP redirects so Authorization is never forwarded to another origin.
- [x] Store only the token environment-variable name; never persist or print the token.
- [x] Pin the generated launcher to one explicit remote config instead of accepting ambient config discovery.
- [x] Require a stable session ID for the host conversation.
- [x] Give every concurrent Turn separate request/answer files, receipt, and Turn ID.
- [x] Hash the exact answer and reject changed text under the same Turn.
- [x] Queue network-ambiguous before/after operations atomically and replay them in order.
- [x] Queue network-ambiguous activity/state/observation/map JSON writes with stable idempotency keys.
- [x] Persist the complete operation payload before the first network attempt.
- [x] Bind every outbox row to installation origin and normalized identity; surface foreign/corrupt rows instead of replaying or deleting them.
- [x] Require operation-specific acknowledgements before deleting an outbox row.
- [x] Treat HTTP semantic failures as blocked errors instead of retryable delivery failures.
- [x] Preserve the original answer when offline and allow it to be sent only under the Skill's outbox rule.
- [x] Expose status and recovery commands suitable for host startup/connectivity restoration.
- [x] Add one-command remote installation with workspace, subject, audience, channel, and token-env arguments.
- [x] Explain real activation evidence; connectivity or file installation alone is not activation.
- [x] Expose remote shared feed/state/channel and spatial list/search/get reads.
- [x] Keep POSIX launcher bytes LF-only and Windows launcher bytes CRLF-only.
- [x] Document exact JSON statuses/exit codes, preallocated Turn IDs, and optional-channel behavior.

## Multi-Agent and household memory

- [x] Preserve existing global/workspace/Agent Claim and Turn isolation.
- [x] Add explicit user, household, person, project, device, Agent, session, and event audiences.
- [x] Add audience memberships and independently addressable channels.
- [x] Add a curated cross-workspace activity feed without copying raw transcripts.
- [x] Add idempotent activity publication, validity windows, and supersession history.
- [x] Add current temporal state keyed by channel + subject + state key.
- [x] Prevent late/out-of-order device delivery from replacing newer state.
- [x] Add confidence, observation time, source reference, validity, expiry, and metadata.
- [x] Materialize state/activity/spatial expiry in Heartbeat and on demand.
- [x] Keep future-valid state scheduled until its validity window begins.
- [x] Prevent an expired newer value from resurrecting an older superseded value.
- [x] Return only relevant, bounded shared context during remote before.
- [x] Apply subject/channel visibility before result limits instead of filtering a limited result afterward.
- [x] Honor Agent and explicitly allowed subject audience memberships.
- [x] Keep another Agent's unfinished Turn and detailed transcript out of shared context.

## Images, maps, and spatial memory

- [x] Keep raw binary bytes outside SQLite and ordinary prompt memory.
- [x] Stream assets to content-addressed object storage with SHA-256 deduplication.
- [x] Verify asset size/hash and prevent paths derived from untrusted filenames.
- [x] Include the asset object directory in normal store backup/restore.
- [x] Add raw HTTP upload/download instead of forcing media through 2 MB JSON.
- [x] Add resumable indexed upload parts, per-part hashes, whole-file verification, and idempotent completion receipts.
- [x] Keep a client-side non-secret upload receipt so interrupted uploads resume safely.
- [x] Preserve per-scope media type/metadata and visibility when identical bytes deduplicate.
- [x] Add stable map IDs, increasing immutable versions, coordinate frames, capture time, and predecessor links.
- [x] Prevent one stable map ID from moving between channels.
- [x] Link maps to raw occupancy-grid/point-cloud/image assets without placing bytes in SQLite.
- [x] Add semantic observations with caption, OCR, objects, location, source, confidence, time, visibility, map, and asset link.
- [x] Add current/history filtering, supersession, expiry, audience membership, workspace, and Agent-private visibility.
- [x] Search spatial captions, OCR, recognized objects, and location text.
- [x] Return asset URIs in semantic results and download bytes only on demand.
- [x] Stream asset downloads to disk and use an RFC 5987-compatible download filename.
- [x] Enforce the configured asset maximum exactly, including limits below 8 MiB.
- [x] Snapshot and verify active asset objects during backup instead of copying a changing object tree.

## Product usability and operations

- [x] Add `serve` and `install-remote-agent` to the public CLI.
- [x] Add task-oriented `shared`, `asset`, `map`, and `spatial` CLI families with examples.
- [x] Add native TLS options and document the recommended reverse-proxy HTTPS deployment.
- [x] Keep server-side Heartbeat/Dream as the single background organizer.
- [x] Show shared activity, current state, assets, maps, and observations in Overview/status counts.
- [x] Document remote setup, tokens, stable identities, recovery, household semantics, and spatial operations.
- [x] Update README, architecture, data model, Agent integration, operations, troubleshooting, upgrade guide, and changelog.
- [x] Bump the package and integration documentation to 2.8.0.
- [x] Add migrations 024-026 and a narrowly scoped repair for the known legacy migration-022 partial state.
- [x] State explicitly that perception/OCR/SLAM/navigation are upstream capabilities, not Meta Memory features.

## Final acceptance (run after freeze)

- [x] Existing local lifecycle and migration tests pass on the final tree.
- [x] Remote identity/lifecycle and subject/channel authorization tests pass.
- [x] Remote outbox, exact-answer, concurrent-Turn, durable world-write, and token non-persistence tests pass.
- [x] Shared audience/activity/state scheduling, supersession, expiry, and pre-limit filtering tests pass.
- [x] Asset/map/spatial storage, per-scope visibility/metadata, streaming, search, and backup tests pass.
- [x] Hosted end-to-end test covers two Agents, multiple subjects/workspaces, shared reads, chunk resume, map, observation, search, and download.
- [x] Source and generated remote Skills pass the Skill validator.
- [x] Installed-wheel smoke runs outside the checkout with packaged migrations/templates and server-config generation.
- [x] A fresh Agent blind-reads the final generated Skill and follows its protocol without repository context.
- [x] Verify full cross-platform CI passes after the final `main` push.
