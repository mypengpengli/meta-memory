# Troubleshooting

Start with one command instead of guessing which subsystem is involved:

```bash
meta-memory overview
```

It reports `ready`, `needs_action`, or `degraded` and includes a `next_action`.
Use JSON when an Agent or script is reading the result:

```bash
meta-memory overview --json
```

## It did not remember something

```bash
meta-memory recovery status
meta-memory turn list --unfinished
meta-memory inbox list
meta-memory memory recent --project auto
```

An interrupted response is normally recoverable through the local spool.  Run
`meta-memory recovery replay` or `meta-memory dream heartbeat` if `overview`
asks for it.  A long running Agent operation should use `turn touch <id>`;
finished work can use the explicit late-completion/reopen lifecycle rather than
creating a second, duplicate Turn.

## It recalled the wrong project or did not continue prior work

```bash
meta-memory project current
meta-memory project list
meta-memory history recent --project auto
meta-memory history search --project auto "keyword"
```

Bind the repository root with an understandable name using `project set`.
The normal cross-Agent path uses completed summaries only; use `history show`
when a bounded detailed transcript is actually required.

## Dream or scheduled maintenance looks wrong

```bash
meta-memory dream status
meta-memory dream deep --scan-days 7 --dry-run
meta-memory schedule status
```

An empty or unchanged Dream scope correctly returns `idle`.  `schedule status`
shows the installed action, most recent result, expected due time, and log
location.  Reinstall the local schedule after moving the repository or Python
environment:

```bash
meta-memory agent sync --all
meta-memory schedule install
```

## Diagnostics, backup, and repair

`meta-memory doctor` is a non-mutating health report.  `meta-memory maintain`
processes dirty work; it is not necessary to run a permanent worker for normal
local use.  Use `meta-memory backup` for a portable copy rather than copying an
active SQLite file or WAL directly.  Restore into an empty destination unless
you intentionally pass `--force`.
