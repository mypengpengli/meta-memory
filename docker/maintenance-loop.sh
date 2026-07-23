#!/bin/sh
set -eu

umask 077

config=${META_MEMORY_CONFIG:-/config/config.toml}
state_dir=${META_MEMORY_WORKER_STATE_DIR:-${META_MEMORY_CONTAINER_STATE_DIR:-/data/.container-runtime}/worker}
heartbeat_interval=${META_MEMORY_HEARTBEAT_INTERVAL_SECONDS:-600}
dream_time=${META_MEMORY_DREAM_TIME:-23:30}
dream_retry=${META_MEMORY_DREAM_RETRY_SECONDS:-3600}
backup_interval=${META_MEMORY_BACKUP_INTERVAL_SECONDS:-86400}
backup_retry=${META_MEMORY_BACKUP_RETRY_SECONDS:-3600}
poll_interval=${META_MEMORY_WORKER_POLL_SECONDS:-30}

die() {
    printf '%s\n' "meta-memory worker: $*" >&2
    exit 2
}

positive_integer() {
    case "$1" in
        ''|*[!0-9]*|0) return 1 ;;
        *) return 0 ;;
    esac
}

for value_name in heartbeat_interval dream_retry backup_interval backup_retry poll_interval; do
    eval "value=\$$value_name"
    positive_integer "$value" || die "$value_name must be a positive integer"
done

case "$dream_time" in
    [0-2][0-9]:[0-5][0-9]) ;;
    *) die "META_MEMORY_DREAM_TIME must be HH:MM" ;;
esac
dream_hour=${dream_time%:*}
dream_minute=${dream_time#*:}
[ "$dream_hour" -le 23 ] || die "META_MEMORY_DREAM_TIME hour must be 00 through 23"

mkdir -p "$state_dir"
# Hold one cross-process lock for the lifetime of this loop. Heartbeat, Dream,
# and backup then run serially even if somebody accidentally scales `worker`.
exec 9>"$state_dir/worker.lock"
flock -n 9 || die "another maintenance worker already owns $state_dir"

next_heartbeat=0
next_backup_attempt=0
next_dream_attempt=0
sleep_pid=
stopping=0

stop() {
    stopping=1
    if [ -n "$sleep_pid" ]; then
        kill "$sleep_pid" 2>/dev/null || true
    fi
}
trap stop INT TERM

read_number() {
    file=$1
    fallback=$2
    if [ -f "$file" ]; then
        value=$(sed -n '1p' "$file")
        case "$value" in
            ''|*[!0-9]*) printf '%s\n' "$fallback" ;;
            *) printf '%s\n' "$value" ;;
        esac
    else
        printf '%s\n' "$fallback"
    fi
}

write_state() {
    destination=$1
    value=$2
    temporary="$destination.tmp.$$"
    printf '%s\n' "$value" > "$temporary"
    mv -f "$temporary" "$destination"
}

run_heartbeat() {
    printf '{"event":"heartbeat_started","at":"%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if meta-memory --json --config "$config" dream heartbeat; then
        printf '{"event":"heartbeat_completed","status":"ok","at":"%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    else
        code=$?
        printf '{"event":"heartbeat_completed","status":"error","exit_code":%s,"at":"%s"}\n' "$code" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
        return "$code"
    fi
}

run_deep() {
    today=$1
    printf '{"event":"dream_started","date":"%s","at":"%s"}\n' "$today" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if meta-memory --json --config "$config" dream deep; then
        write_state "$state_dir/dream.last-success-date" "$today"
        printf '{"event":"dream_completed","status":"ok","date":"%s","at":"%s"}\n' "$today" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        return 0
    else
        code=$?
        printf '{"event":"dream_completed","status":"error","exit_code":%s,"date":"%s","at":"%s"}\n' "$code" "$today" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
        return "$code"
    fi
}

run_backup() {
    printf '{"event":"backup_started","at":"%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if meta-memory-backup; then
        now=$(date +%s)
        write_state "$state_dir/backup.last-success-epoch" "$now"
        printf '{"event":"backup_completed","status":"ok","at":"%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        return 0
    else
        code=$?
        printf '{"event":"backup_completed","status":"error","exit_code":%s,"at":"%s"}\n' "$code" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
        return "$code"
    fi
}

last_backup=$(read_number "$state_dir/backup.last-success-epoch" 0)

printf '{"event":"worker_started","heartbeat_interval_seconds":%s,"dream_time":"%s","backup_interval_seconds":%s,"at":"%s"}\n' \
    "$heartbeat_interval" "$dream_time" "$backup_interval" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

while [ "$stopping" -eq 0 ]; do
    now=$(date +%s)

    if [ "$now" -ge "$next_heartbeat" ]; then
        run_heartbeat || true
        now=$(date +%s)
        next_heartbeat=$((now + heartbeat_interval))
    fi

    today=$(date +%Y-%m-%d)
    now_hour=$(date +%H)
    now_minute=$(date +%M)
    last_dream=
    if [ -f "$state_dir/dream.last-success-date" ]; then
        last_dream=$(sed -n '1p' "$state_dir/dream.last-success-date")
    fi
    if { [ "$now_hour" -gt "$dream_hour" ] || { [ "$now_hour" -eq "$dream_hour" ] && [ "$now_minute" -ge "$dream_minute" ]; }; } \
        && [ "$last_dream" != "$today" ] \
        && [ "$now" -ge "$next_dream_attempt" ]; then
        if run_deep "$today"; then
            next_dream_attempt=$((now + 86400))
        else
            next_dream_attempt=$((now + dream_retry))
        fi
    fi

    last_backup=$(read_number "$state_dir/backup.last-success-epoch" "$last_backup")
    if [ "$now" -ge "$next_backup_attempt" ] && [ "$now" -ge $((last_backup + backup_interval)) ]; then
        if run_backup; then
            last_backup=$(read_number "$state_dir/backup.last-success-epoch" "$now")
            next_backup_attempt=$((last_backup + backup_interval))
        else
            next_backup_attempt=$((now + backup_retry))
        fi
    fi

    if [ "$stopping" -eq 0 ]; then
        sleep "$poll_interval" &
        sleep_pid=$!
        wait "$sleep_pid" 2>/dev/null || true
        sleep_pid=
    fi
done

printf '{"event":"worker_stopped","at":"%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
