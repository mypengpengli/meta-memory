---
name: meta-memory
description: Use on every user turn. Durably begin a Turn before drafting, then save the exact completed draft before sending it.
---

# Meta Memory

This repository mirror follows the canonical installer template at
`meta_memory/templates/skill.md.template`.  Installed Agent Skills are
regenerated with `meta-memory agent sync --all`; do not hand-maintain a second
turn contract.

## Per-turn contract

1. Write the user request to a temporary UTF-8 file and begin a Turn:

   ```bash
   meta-memory before --project auto --session auto --query-file <request-file>
   ```

   Keep the returned `turn_id`, `hot_context`, and `context`. Use only relevant
   recalled context; do not read raw transcripts or browse the store manually.

2. Draft the complete answer. Before sending it, write that exact draft to a
   temporary UTF-8 file and complete the same Turn:

   ```bash
   meta-memory after --turn <turn_id> --assistant-file <answer-file>
   ```

   If completion is temporarily spooled, send the same draft normally; the
   local recovery path will replay it. Do not create a second Turn merely to
   retry an `after` operation.

3. When the user explicitly asks to remember or correct a fact, use
   `remember` or `correct`. Explicit memory writes are intentional; ordinary
   task requests and acknowledgements remain session-only by default.

4. On a continuation request, use the bounded completed-session summaries in
   `cross_agent_continuity`. Use `history show` or `--detail` only when the
   summary is insufficient for concrete work.

## Operational rules

- Never complete a Turn created by another Agent.
- For long-running work, renew the current Turn with `meta-memory turn touch
  <turn-id>` before its lease expires.
- Use `meta-memory overview` as the first runtime check; it returns a concrete
  `next_action` when anything needs attention.
- Use `meta-memory inbox list` to review ambiguous automatic memory proposals.
- Use `meta-memory dream heartbeat` only when just-completed work needs prompt
  consolidation. Deep Dream is source-linked deferred synthesis and must not
  replace Claim facts.
- Do not pass internal profile, workspace, visibility, owner, token, or
  private-memory options in normal operation.
