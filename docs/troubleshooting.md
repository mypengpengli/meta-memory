# Troubleshooting

Start with one command instead of guessing which subsystem is involved:

```bash
meta-memory overview
```

It reports `ready`, `needs_action`, or `degraded` and includes a `next_action`.
Use JSON when an Agent or script is reading the result:

```bash
meta-memory --json overview
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

Only an `after` result with `status: spooled` is a deferred, replayable
completion. A missing/wrong Turn, wrong Agent, or changed answer is a semantic
lifecycle error, not a spool condition; preserve the answer and resolve it
before sending. See [Agent integration](agent-integration.md).

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

## A remote Agent cannot connect or stays inactive

Run the generated remote launcher's `status` command. Check, in order:

1. the URL is HTTPS (plain HTTP is accepted only for localhost);
2. the environment variable named by `token_env` exists in the Agent process;
3. the server process has the corresponding token variable and agents-file row;
4. `profile_id` was copied from `shared init`, and `workspace_id`, `subject_id`,
   `audience_id`, and optional `channel_id` match the row;
5. the Agent actually loaded the generated `meta-memory-remote/SKILL.md`.

`status` proves connectivity only. Complete a normal Turn and require
`lifecycle_state: active` with recent `last_before` and `last_after` (older
compatible output may call them `last_before_at` and `last_after_at`).

If `local_outbox_pending` is non-zero, keep the original request/answer or asset
file and run `<remote-launcher> recovery`. A blocked 4xx item is an identity or
protocol error; do not discard it, create a replacement Turn, or change the
answer under the same Turn ID.

`local_outbox_corrupt` must also be zero. A foreign-origin, identity-mismatched,
or unreadable row is deliberately kept visible; do not edit its receipt to
force delivery. Preserve the original files and correct the installation or
binding with an operator. A launcher import/startup failure or any non-JSON
output is not a degraded Turn and must be repaired before drafting.

Do not judge recovery from exit code alone. Read the JSON status: `deferred`
means connectivity is still unavailable; `needs_action` means a semantic 4xx
item is blocked and its binding/identity must be corrected.

## A pure server Overview says a local Agent needs setup

Ordinary `meta-memory overview` evaluates a local workstation and its installed
Agent Skills. A dedicated HTTP server may intentionally have none. Evaluate
the hosted service instead:

```bash
meta-memory overview --server --agents-file "$HOME/.meta-memory/agents.json"
```

This checks the server store and binding file without requiring a local Codex,
Claude Code, or OpenClaw lifecycle.

## Shared activity/state/spatial context is empty

Confirm the Agent is a member of the channel audience and that both the
agents-file and remote config use the real IDs returned by `shared init`:

```bash
meta-memory shared channels
meta-memory shared feed --channel-id <id> --include-history
meta-memory shared states --channel-id <id> --include-history
meta-memory spatial list --channel-id <id> --include-history
```

Current context intentionally excludes expired/superseded data, raw binaries,
other Agents' unfinished Turns, and records outside the subject/channel. Use
`asset show` or remote `asset get` for metadata and download bytes only when
needed.

If the remote config intentionally has no channel, an empty `shared_context` is
expected. Ordinary Turn and workspace memory still work; activity/state/map/
spatial writes require the administrator to create a real channel and reinstall
the generated integration. Never substitute a guessed ID.

## A remote asset upload was interrupted

Keep the source file at the same path and do not edit or rename it. Run the
same command again:

```text
<remote-launcher> asset upload --file <same-file> --media-type <same-media-type>
```

The client reuses its upload receipt and verified parts. Remote `recovery`
replays Turn and JSON writes; retrying the unchanged upload command resumes the
binary upload. If the source changed intentionally, treat it as a new asset.

## A map or observation contains no recognition result

Meta Memory does not run visual recognition, OCR, object detection, SLAM, map
fusion, localization, or route planning. Confirm that the robot/upstream model
produced semantic output first, then store its caption/OCR/objects/map metadata,
source, observation time, confidence, and optional asset link.
