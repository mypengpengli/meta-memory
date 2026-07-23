#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repository_dir=$(dirname "$script_dir")
cd "$repository_dir"

env_file=.env
template=.env.example
rotate=false

case "${1:-}" in
    '') ;;
    --rotate-token) rotate=true ;;
    -h|--help)
        printf '%s\n' \
            "Usage: sh docker/bootstrap.sh [--rotate-token]" \
            "Creates .env and runtime directories without starting services." \
            "Existing tokens are preserved unless --rotate-token is explicit."
        exit 0
        ;;
    *)
        printf '%s\n' "Unknown option: $1" >&2
        exit 2
        ;;
esac

[ -f "$template" ] || {
    printf '%s\n' "Missing $repository_dir/$template" >&2
    exit 2
}

if [ ! -f "$env_file" ]; then
    cp "$template" "$env_file"
fi
chmod 0600 "$env_file" 2>/dev/null || true

set_env() {
    key=$1
    value=$2
    temporary="$env_file.tmp.$$"
    awk -v key="$key" -v value="$value" '
        BEGIN { replaced = 0 }
        index($0, key "=") == 1 {
            if (!replaced) print key "=" value
            replaced = 1
            next
        }
        { print }
        END { if (!replaced) print key "=" value }
    ' "$env_file" > "$temporary"
    mv -f "$temporary" "$env_file"
    chmod 0600 "$env_file" 2>/dev/null || true
}

get_env() {
    key=$1
    fallback=$2
    value=$(awk -v key="$key" 'index($0, key "=") == 1 {sub("^[^=]*=", ""); found=$0} END {print found}' "$env_file")
    if [ -n "$value" ]; then
        printf '%s\n' "$value"
    else
        printf '%s\n' "$fallback"
    fi
}

current_token=$(get_env META_MEMORY_TOKEN '')
if [ -z "$current_token" ] || [ "$rotate" = "true" ]; then
    token=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
    [ "${#token}" -eq 64 ] || {
        printf '%s\n' "Could not generate a 256-bit token." >&2
        exit 1
    }
    set_env META_MEMORY_TOKEN "$token"
fi

case "$(uname -s 2>/dev/null || printf unknown)" in
    Linux)
        host_uid=$(id -u)
        host_gid=$(id -g)
        # The container intentionally never runs as uid 0. A root operator can
        # still read files owned by the documented container uid/gid.
        if [ "$host_uid" -gt 0 ] && [ "$host_gid" -gt 0 ]; then
            set_env META_MEMORY_UID "$host_uid"
            set_env META_MEMORY_GID "$host_gid"
        fi
        ;;
esac

make_runtime_dir() {
    configured=$1
    case "$configured" in
        /*|[A-Za-z]:[\\/]*) directory=$configured ;;
        *) directory="$repository_dir/$configured" ;;
    esac
    mkdir -p "$directory"
    chmod 0700 "$directory" 2>/dev/null || true
}

make_runtime_dir "$(get_env META_MEMORY_DATA_DIR './runtime/data')"
make_runtime_dir "$(get_env META_MEMORY_CONFIG_DIR './runtime/config')"
make_runtime_dir "$(get_env META_MEMORY_BACKUP_DIR_HOST './runtime/backups')"

META_MEMORY_TOKEN=$(get_env META_MEMORY_TOKEN '') docker compose config --quiet

printf '%s\n' \
    "Docker files are ready." \
    "  Environment: $repository_dir/$env_file" \
    "  Token: generated and kept private (not printed)" \
    "Next: docker compose up -d --build" \
    "HTTPS: set MEMORY_DOMAIN, then docker compose --profile https up -d"
if [ "$rotate" = "true" ]; then
    printf '%s\n' \
        "Token rotated. Recreate the API and update the remote Agent before its next request:" \
        "  docker compose up -d --force-recreate meta-memory"
fi
