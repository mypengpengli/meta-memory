# Data model

- **Raw Event**: append-only user, assistant, feedback or resource evidence.
- **Session Archive**: searchable original messages grouped by session.
- **Memory Unit**: an atomic candidate extracted from one raw event.
- **Claim**: sourced, temporal, correctable long-term memory.
- **Hot Memory**: bounded, frozen per-session projection of eligible claims.
- **Proposal**: a reviewable correction, replacement or other high-risk change.

SQLite holds authoritative evidence, claims, queues and indexes. Markdown files
are readable projections, not a reason to bypass the SQLite safety path.
