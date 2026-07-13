# Troubleshooting

Run `meta-memory doctor` first. It checks schema migrations, FTS availability,
blocked claims, unsourced active claims and queued work.

Run `meta-memory maintain` to recover expired leases, process queued turns and
refresh projections. Do not run permanent workers unless using an advanced
deployment.

For a portable copy use `meta-memory backup`; do not copy an active SQLite file
or its WAL file directly. Use `meta-memory restore <archive>` only into an empty
destination, or explicitly pass `--force` after confirming replacement is safe.
