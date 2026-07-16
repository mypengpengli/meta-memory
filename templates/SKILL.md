---
name: meta-memory
description: Repository compatibility mirror for the Meta Memory local Agent Skill. Use the installed Agent-specific launcher to run before, draft, after, then send on every user turn.
---

# Meta Memory compatibility mirror

The installer source of truth is `meta_memory/templates/skill.md.template`.
Regenerate installed Skills and launchers after an upgrade or move:

```bash
meta-memory agent sync --all
```

Use the generated Skill's launcher form that matches the host's actual shell.
Its strict order is:

```text
before → draft exact answer → after with the same turn_id → send
```

Send only after `ok` or `spooled`. A `spooled` completion is a temporary
runtime/storage retry; do not create a second Turn. A semantic error such as a
wrong Turn, Agent, or changed answer is not spoolable: preserve the answer and
resolve the lifecycle problem before sending. See the root `SKILL.md` and
`docs/agent-integration.md` for the complete contract.

After one real host turn, confirm activation with `meta-memory agent status
--all --verbose`; launcher verification alone is not proof that the host loaded
the Skill.
