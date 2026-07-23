#!/bin/sh
set -eu

umask 077

config=${META_MEMORY_CONFIG:-/config/config.toml}
backup_dir=${META_MEMORY_BACKUP_DIR:-/backups}
agents_source=${META_MEMORY_AGENTS_FILE:-/config/agents.json}
retention_days=${META_MEMORY_BACKUP_RETENTION_DAYS:-30}
retention_count=${META_MEMORY_BACKUP_RETENTION_COUNT:-14}
lock_wait=${META_MEMORY_BACKUP_LOCK_WAIT_SECONDS:-60}
prune=${META_MEMORY_BACKUP_PRUNE:-true}

positive_integer() {
    case "$1" in
        ''|*[!0-9]*|0) return 1 ;;
        *) return 0 ;;
    esac
}

positive_integer "$retention_days" || {
    printf '%s\n' "META_MEMORY_BACKUP_RETENTION_DAYS must be a positive integer" >&2
    exit 2
}
positive_integer "$retention_count" || {
    printf '%s\n' "META_MEMORY_BACKUP_RETENTION_COUNT must be a positive integer" >&2
    exit 2
}
positive_integer "$lock_wait" || {
    printf '%s\n' "META_MEMORY_BACKUP_LOCK_WAIT_SECONDS must be a positive integer" >&2
    exit 2
}
case "$prune" in
    true|false) ;;
    *)
        printf '%s\n' "META_MEMORY_BACKUP_PRUNE must be true or false" >&2
        exit 2
        ;;
esac

mkdir -p "$backup_dir"
[ -f "$agents_source" ] || {
    printf '%s\n' "Agent binding file does not exist: $agents_source" >&2
    exit 2
}
state_dir=${META_MEMORY_WORKER_STATE_DIR:-${META_MEMORY_CONTAINER_STATE_DIR:-/data/.container-runtime}/worker}
mkdir -p "$state_dir"
exec 8>"$state_dir/backup.lock"
flock -w "$lock_wait" 8 || {
    printf '%s\n' "Another backup is still running after ${lock_wait}s." >&2
    exit 1
}

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
final="$backup_dir/meta-memory-$timestamp.zip"
if [ -e "$final" ]; then
    final="$backup_dir/meta-memory-$timestamp-$$.zip"
fi
partial="$backup_dir/.meta-memory-$timestamp-$$.partial.zip"
base=${final%.zip}
agents_final="$base.agents.json"
manifest_final="$base.manifest.json"
agents_partial="$backup_dir/.meta-memory-$timestamp-$$.partial.agents.json"
manifest_partial="$backup_dir/.meta-memory-$timestamp-$$.partial.manifest.json"
committed=false

cleanup() {
    rm -f -- "$partial" "$agents_partial" "$manifest_partial"
    if [ "$committed" != "true" ]; then
        rm -f -- "$final" "$agents_final" "$manifest_final"
    fi
}
trap cleanup EXIT HUP INT TERM

meta-memory --json --config "$config" backup --output "$partial"
cp -- "$agents_source" "$agents_partial"
chmod 0600 "$agents_partial"
archive_sha256=$(sha256sum "$partial" | awk '{print $1}')
agents_sha256=$(sha256sum "$agents_partial" | awk '{print $1}')
created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
archive_name=$(basename "$final")
agents_name=$(basename "$agents_final")
printf '%s\n' \
    '{' \
    '  "format_version": 1,' \
    "  \"created_at\": \"$created_at\"," \
    "  \"archive\": \"$archive_name\"," \
    "  \"archive_sha256\": \"$archive_sha256\"," \
    "  \"agents_file\": \"$agents_name\"," \
    "  \"agents_sha256\": \"$agents_sha256\"" \
    '}' > "$manifest_partial"
chmod 0600 "$manifest_partial"
mv "$partial" "$final"
mv "$agents_partial" "$agents_final"
mv "$manifest_partial" "$manifest_final"
committed=true

if [ "$prune" = "true" ]; then
    # Remove abandoned partial archives and completed archives beyond both limits.
    find "$backup_dir" -maxdepth 1 -type f -name '.meta-memory-*.partial.*' -mtime +1 -exec rm -f -- {} +
    find "$backup_dir" -maxdepth 1 -type f -name 'meta-memory-*.zip' -mtime "+$retention_days" \
        | while IFS= read -r old_backup; do
            old_base=${old_backup%.zip}
            rm -f -- "$old_backup" "$old_base.agents.json" "$old_base.manifest.json"
        done
    find "$backup_dir" -maxdepth 1 -type f -name 'meta-memory-*.zip' \
        -printf '%T@\t%p\n' \
        | sort -nr \
        | awk -F '\t' -v keep="$retention_count" 'NR > keep {sub(/^[^\t]*\t/, ""); print}' \
        | while IFS= read -r old_backup; do
            if [ -n "$old_backup" ]; then
                old_base=${old_backup%.zip}
                rm -f -- "$old_backup" "$old_base.agents.json" "$old_base.manifest.json"
            fi
        done
    # A kill between the three atomic renames can leave an incomplete current
    # bundle. Remove that whole partial bundle while preserving true legacy
    # ZIP-only backups, which have neither sidecar.
    find "$backup_dir" -maxdepth 1 -type f -name 'meta-memory-*.agents.json' \
        | while IFS= read -r old_agents; do
            old_base=${old_agents%.agents.json}
            if [ ! -f "$old_base.zip" ] || [ ! -f "$old_base.manifest.json" ]; then
                rm -f -- "$old_base.zip" "$old_agents" "$old_base.manifest.json"
            fi
        done
    find "$backup_dir" -maxdepth 1 -type f -name 'meta-memory-*.manifest.json' \
        | while IFS= read -r old_manifest; do
            old_base=${old_manifest%.manifest.json}
            if [ ! -f "$old_base.zip" ] || [ ! -f "$old_base.agents.json" ]; then
                rm -f -- "$old_base.zip" "$old_manifest" "$old_base.agents.json"
            fi
        done
fi

trap - EXIT HUP INT TERM
printf '{"status":"ok","backup":"%s","agents":"%s","manifest":"%s","retention_days":%s,"retention_count":%s,"pruned":%s}\n' \
    "$final" "$agents_final" "$manifest_final" "$retention_days" "$retention_count" "$prune"
