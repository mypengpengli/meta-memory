#!/bin/sh
set -eu

umask 077

config=${META_MEMORY_CONFIG:-/config/config.toml}
store=${META_MEMORY_STORE:-/data/store}
agents_file=${META_MEMORY_AGENTS_FILE:-/config/agents.json}
backup_dir=${META_MEMORY_BACKUP_DIR:-/backups}
state_dir=${META_MEMORY_CONTAINER_STATE_DIR:-/data/.container-runtime}
store_parent=$(dirname "$store")

die() {
    printf '%s\n' "meta-memory container: $*" >&2
    exit 2
}

positive_integer() {
    case "$1" in
        ''|*[!0-9]*|0) return 1 ;;
        *) return 0 ;;
    esac
}

# Bind mounts can be owned by a different host user. Repair them once as root,
# then run both the API and worker without root privileges.
if [ "$(id -u)" = "0" ] && [ "${META_MEMORY_PRIVILEGES_DROPPED:-0}" != "1" ]; then
    runtime_uid=${META_MEMORY_UID:-10001}
    runtime_gid=${META_MEMORY_GID:-10001}
    positive_integer "$runtime_uid" || die "META_MEMORY_UID must be a positive integer"
    positive_integer "$runtime_gid" || die "META_MEMORY_GID must be a positive integer"
    install -d -m 0750 "$store_parent" "$store" "$(dirname "$config")" "$backup_dir" "$state_dir"
    # `install -d` can create the leaf store as root inside an otherwise
    # writable host-owned parent.  Check both parent and leaf so a fresh Linux
    # bind mount never leaves /data/store inaccessible after privilege drop.
    for directory in "$store_parent" "$store" "$(dirname "$config")" "$backup_dir" "$state_dir"; do
        if ! setpriv --reuid "$runtime_uid" --regid "$runtime_gid" --clear-groups test -w "$directory"; then
            chown -R "$runtime_uid:$runtime_gid" "$directory"
        fi
    done
    export META_MEMORY_PRIVILEGES_DROPPED=1
    export HOME=/home/meta-memory
    chown "$runtime_uid:$runtime_gid" /home/meta-memory
    exec setpriv --reuid "$runtime_uid" --regid "$runtime_gid" --clear-groups "$0" "$@"
fi

mkdir -p "$store" "$(dirname "$config")" "$backup_dir" "$state_dir"
[ -w "$store_parent" ] || die "$store_parent is not writable by uid $(id -u)"
[ -w "$store" ] || die "$store is not writable by uid $(id -u)"
[ -w "$state_dir" ] || die "$state_dir is not writable by uid $(id -u)"
[ -w "$(dirname "$config")" ] || die "$(dirname "$config") is not writable by uid $(id -u)"
[ -w "$backup_dir" ] || die "$backup_dir is not writable by uid $(id -u)"

if [ ! -f "$config" ]; then
    meta-memory --config "$config" setup \
        --name "${META_MEMORY_PROFILE_NAME:-User}" \
        --store "$store" \
        --maintenance yes \
        --dream yes \
        --agents \
        --no-schedule \
        --non-interactive
fi

# A fresh deployment can bootstrap its first Agent entirely from environment
# variables. The secret itself stays in the environment; agents.json records
# only its variable name.
if [ ! -f "$agents_file" ]; then
    agent_id=${META_MEMORY_BOOTSTRAP_AGENT_ID:-local-codex}
    workspace_id=${META_MEMORY_BOOTSTRAP_WORKSPACE_ID:-personal-workspace}
    subject_id=${META_MEMORY_BOOTSTRAP_SUBJECT_ID:-person:user}
    token_env=${META_MEMORY_TOKEN_ENV:-META_MEMORY_TOKEN}
    (
        set -f
        set -- meta-memory --config "$config" init-agents-file \
            --output "$agents_file" \
            --agent-id "$agent_id" \
            --workspace-id "$workspace_id" \
            --subject-id "$subject_id" \
            --token-env "$token_env"
        old_ifs=$IFS
        IFS=,
        for audience_id in ${META_MEMORY_BOOTSTRAP_AUDIENCE_IDS:-}; do
            audience_id=$(printf '%s' "$audience_id" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
            if [ -n "$audience_id" ]; then
                set -- "$@" --audience-id "$audience_id"
            fi
        done
        IFS=$old_ifs
        exec "$@"
    )
fi

# The Compose contract has one API replica. This OS lock also prevents a
# second Compose project from opening another authoritative HTTP writer on the
# same data volume by mistake. The descriptor remains held across exec.
for argument in "$@"; do
    if [ "$argument" = "serve" ]; then
        exec 8>"$state_dir/api.lock"
        flock -n 8 || die "another meta-memory serve process already owns $store"
        break
    fi
done

exec "$@"
