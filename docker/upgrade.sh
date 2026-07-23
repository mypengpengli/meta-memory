#!/bin/sh
set -eu

# Keep /config, /data, and /backups as container paths under Windows Git Bash.
if [ -n "${MSYSTEM:-}" ]; then
    export MSYS_NO_PATHCONV=1
fi

mode=${META_MEMORY_UPGRADE_MODE:-build}
pull_base=${META_MEMORY_UPGRADE_PULL_BASE:-true}
case "$pull_base" in
    true|false) ;;
    *)
        printf '%s\n' "META_MEMORY_UPGRADE_PULL_BASE must be true or false" >&2
        exit 2
        ;;
esac
caddy_running=false
if [ -n "$(docker compose --profile https ps --status running -q caddy 2>/dev/null)" ]; then
    caddy_running=true
fi

# Take the mandatory snapshot before resolving or building a new image. A
# one-off container goes through the entrypoint and therefore uses the same
# non-root uid/gid as scheduled backups; `compose exec` would default to root.
docker compose run --rm --no-deps worker meta-memory-backup

case "$mode" in
    build)
        if [ "$pull_base" = "true" ]; then
            docker compose build --pull meta-memory
        else
            docker compose build meta-memory
        fi
        ;;
    pull)
        docker compose pull meta-memory
        ;;
    *)
        printf '%s\n' "META_MEMORY_UPGRADE_MODE must be build or pull" >&2
        exit 2
        ;;
esac
if [ "$caddy_running" = "true" ]; then
    # Pull the pinned proxy image only when HTTPS was already enabled. A user
    # running the API behind another gateway must not suddenly need a domain.
    docker compose --profile https pull caddy
fi

# The backup lock serialized the snapshot with any scheduled backup. Only now
# stop the scheduler and recreate the single API instance.
docker compose stop worker
restart_worker() {
    docker compose up -d worker >/dev/null
}
trap restart_worker EXIT HUP INT TERM

docker compose up -d meta-memory worker
trap - EXIT HUP INT TERM

attempt=0
until docker compose exec -T meta-memory python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/readyz', timeout=2).read()" >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 30 ] || {
        printf '%s\n' "Upgrade was deployed, but the API did not become ready within 60 seconds." >&2
        exit 1
    }
    sleep 2
done

docker compose run --rm --no-deps meta-memory \
    meta-memory --json --config /config/config.toml doctor
if [ "$caddy_running" = "true" ]; then
    docker compose --profile https up -d --force-recreate caddy
    attempt=0
    until [ -n "$(docker compose --profile https ps --status running -q caddy 2>/dev/null)" ]; do
        attempt=$((attempt + 1))
        [ "$attempt" -lt 15 ] || {
            printf '%s\n' "The API upgraded, but Caddy did not remain running. Check: docker compose logs caddy" >&2
            exit 1
        }
        sleep 2
    done
fi
printf '%s\n' "Upgrade completed; the pre-upgrade archive and Agent-binding sidecars are in the configured backup directory."
