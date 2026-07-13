# Default architecture

Meta Memory defaults to a local shared runtime:

```text
Agent → meta-memory CLI → SQLite + Markdown → scheduled maintain / dream
```

The public model is only **user**, **project**, and **session**. Internally,
user memory maps to a profile-wide `global` scope and project memory maps to a
workspace scope. Agent IDs are retained for provenance and idempotency, never
used to isolate ordinary memory.

`after` appends raw user/assistant evidence and queues processing. `maintain`
uses durable SQLite leases to build session cards, atomic units, claims,
projections and hot memory. `dream` writes an inferred report with source Claim
IDs; it never overwrites a source claim or executes an external action.
