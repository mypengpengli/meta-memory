#!/bin/sh
set -eu

# Linux-only acceptance for the actual Compose services and host operations.
# Every path and project name is isolated so this cannot touch a developer's
# normal .env or runtime directories.
suffix="$$"
test_root="./runtime/compose-stack-e2e-$suffix"
export COMPOSE_PROJECT_NAME="meta-memory-compose-e2e-$suffix"
export META_MEMORY_IMAGE=${META_MEMORY_IMAGE:-meta-memory:container-e2e}
export META_MEMORY_DATA_DIR="$test_root/data"
export META_MEMORY_CONFIG_DIR="$test_root/config"
export META_MEMORY_BACKUP_DIR_HOST="$test_root/backups"
export META_MEMORY_UID=$(id -u)
export META_MEMORY_GID=$(id -g)
export META_MEMORY_TOKEN=${META_MEMORY_TOKEN:-$(python -c 'import secrets; print(secrets.token_urlsafe(48))')}
export META_MEMORY_PORT=$(python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
export META_MEMORY_UPGRADE_PULL_BASE=false

before_json=$(mktemp)
after_json=$(mktemp)
cleanup() {
    docker compose down --volumes --remove-orphans >/dev/null 2>&1 || true
    rm -f -- "$before_json" "$after_json"
    rm -rf -- "$test_root"
}
trap cleanup EXIT HUP INT TERM

mkdir -p "$META_MEMORY_DATA_DIR" "$META_MEMORY_CONFIG_DIR" "$META_MEMORY_BACKUP_DIR_HOST"
docker compose config --quiet
docker compose up -d meta-memory worker

attempt=0
until python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$META_MEMORY_PORT/readyz', timeout=2).read()" >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 60 ] || {
        docker compose logs --tail 100 >&2
        printf '%s\n' "Compose API did not become ready." >&2
        exit 1
    }
    sleep 1
done

attempt=0
until find "$META_MEMORY_BACKUP_DIR_HOST" -maxdepth 1 -type f -name 'meta-memory-*.manifest.json' | grep -q .; do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 60 ] || {
        docker compose logs --tail 100 worker >&2
        printf '%s\n' "Worker did not create its initial backup bundle." >&2
        exit 1
    }
    sleep 1
done

first_archive=$(find "$META_MEMORY_BACKUP_DIR_HOST" -maxdepth 1 -type f -name 'meta-memory-*.zip' | sort | head -n 1)
[ -n "$first_archive" ]
first_base=${first_archive%.zip}
test -f "$first_base.agents.json"
test -f "$first_base.manifest.json"

sh docker/admin.sh --json remember --project restore-probe \
    --content post-backup-marker-should-disappear >/dev/null
sh docker/admin.sh --json search --project restore-probe \
    post-backup-marker-should-disappear > "$before_json"
python -c 'import json,sys; assert len(json.load(open(sys.argv[1], encoding="utf-8"))["results"]) >= 1' "$before_json"

sh docker/restore.sh "$(basename "$first_archive")"
sh docker/admin.sh --json search --project restore-probe \
    post-backup-marker-should-disappear > "$after_json"
python -c 'import json,sys; assert len(json.load(open(sys.argv[1], encoding="utf-8"))["results"]) == 0' "$after_json"

# Exercise the cached-base success path. The default refresh-base path has the
# same state transition but additionally contacts the image registry.
sh docker/upgrade.sh
python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$META_MEMORY_PORT/readyz', timeout=3).read()"
test "$(docker compose ps --status running -q meta-memory | wc -l)" -eq 1
test "$(docker compose ps --status running -q worker | wc -l)" -eq 1

printf '%s\n' '{"status":"ok","checks":["compose","worker_backup","admin_uid","sidecars","restore","upgrade"]}'
