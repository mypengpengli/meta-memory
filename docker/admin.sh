#!/bin/sh
set -eu

# Run administrative CLI commands through the image entrypoint so the process
# uses the same non-root uid/gid as the API and worker. `docker compose exec`
# defaults to the image's root user and can otherwise leave unreadable SQLite
# sidecars, Claim files, or backups in bind-mounted directories.
if [ -n "${MSYSTEM:-}" ]; then
    export MSYS_NO_PATHCONV=1
fi

[ "$#" -gt 0 ] || {
    printf '%s\n' \
        "Usage: sh docker/admin.sh <meta-memory arguments>" \
        "Example: sh docker/admin.sh --json overview --server --agents-file /config/agents.json" >&2
    exit 2
}

exec docker compose run --rm --no-deps meta-memory \
    meta-memory --config /config/config.toml "$@"
