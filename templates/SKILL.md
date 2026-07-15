---
name: meta-memory
description: Repository compatibility mirror; use the installed canonical Meta Memory Skill contract for every user turn.
---

# Meta Memory compatibility mirror

The only installation source of truth is
`meta_memory/templates/skill.md.template`. Regenerate a host integration with:

```bash
meta-memory agent sync --all
```

The contract is: call `before` before drafting, retain its `turn_id`, call
`after --turn <turn_id>` with the exact final answer before sending it, and use
`overview`/`recovery` for operational state. See the root `SKILL.md` for the
repository-readable version of the same workflow.
