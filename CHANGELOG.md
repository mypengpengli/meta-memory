# Changelog

## 2.7.0 — 2026-07-16

### Portable Agent integrations

- Made every installed CLI entry bootstrap packaged legacy modules before command dispatch, so commands work outside the source checkout without `PYTHONPATH`.
- Added explicit integration lifecycle observability that separates generated files, verified launchers, and a host-observed `before`/`after` turn.
- Strengthened the generated Skill contract for any compatible local CLI Agent with an exact `before → draft → after → send` protocol and precise failure handling.
- Added a practical custom-Agent integration guide and corrected OpenAI Skill metadata to invoke `$meta-memory`.

### Fresh-install confidence

- Added an installed-package smoke test that runs the console command from an unrelated temporary directory and verifies packaged migrations, templates, launchers, lifecycle status, Doctor, and Overview.
- Packaged the optional LLM prompts and legacy defaults as runtime resources instead of relying on source-checkout paths.
- Made setup surface recoverable scheduler and integration problems as concrete next actions instead of presenting a partial installation as ready.

## 2.6.0 — 2026-07-15

### Daily-use closure

- Added a public lifecycle for memories, project bindings, session history, imported resources, review proposals, recovery, and human-readable readiness status.
- Added an approval inbox and feedback loop so automatically proposed memories never become an invisible backlog.
- Added Agent integration synchronization and upgrade visibility using a versioned Skill contract.
- Added practical operations documentation and a first-day usage path.

### Memory quality and continuity

- Added intent-aware memory intake that separates ignored text, session-only work, explicit writes, and long-term candidates.
- Added automatic retrieval of bounded completed cross-Agent summaries for continuation requests.
- Added structured session state and an auditable, source-aware Dream lifecycle with preview, list, show, and archive support.
- Added long-running Turn lease/touch and late-completion recovery semantics.

### Performance and maintenance

- Made retrieval bounded before final Python ranking, with indexed retrieval for memories, resources, and session summaries.
- Made maintenance and Hot Memory work scope/generation driven instead of repeatedly scanning completed history.
- Added database startup fast paths, runtime retention/compaction, scheduler observability/log rotation, and project-identity reuse.

### Compatibility

- Existing top-level commands remain supported as compatibility aliases where a lifecycle subcommand now provides the clearer form.
- Existing stores migrate forward transactionally through numbered SQLite migrations.

## 2.5.0

- Multi-Agent runtime isolation, shared completed-session summaries, Dream heartbeat/deep synthesis, Agent status/verify, runtime audit, and unfinished-Turn recovery.
