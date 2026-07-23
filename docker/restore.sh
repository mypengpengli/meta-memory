#!/bin/sh
set -eu

# Git Bash rewrites Linux container paths before invoking docker.exe unless
# path conversion is disabled. Native Linux shells are unaffected.
if [ -n "${MSYSTEM:-}" ]; then
    export MSYS_NO_PATHCONV=1
fi

archive=${1:-}
[ -n "$archive" ] || {
    printf '%s\n' "Usage: docker/restore.sh <backup-filename.zip>" >&2
    exit 2
}
case "$archive" in
    */*|*\\*)
        printf '%s\n' "Use a filename from the configured backup directory, not a path." >&2
        exit 2
        ;;
    *.zip) ;;
    *)
        printf '%s\n' "Backup filename must end in .zip." >&2
        exit 2
        ;;
esac

base=${archive%.zip}
agents_name="$base.agents.json"
manifest_name="$base.manifest.json"

compose() {
    docker compose "$@"
}

restart_services() {
    compose up -d meta-memory worker >/dev/null
}
trap restart_services EXIT HUP INT TERM

# Stop the only scheduler first so it cannot prune the selected archive while
# it is being verified. Verification runs inside the image, so it also works
# when old bind-mount files are not readable by the host account.
compose stop worker
bundle_status=$(compose run --rm --no-deps --entrypoint python meta-memory -c '
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

archive, agents, manifest = (Path(value) for value in sys.argv[1:4])
if not archive.is_file():
    raise SystemExit(f"Backup not found: {archive}")
sidecars = (agents.is_file(), manifest.is_file())
if sidecars == (False, False):
    print("legacy")
    raise SystemExit(0)
if sidecars != (True, True):
    raise SystemExit("Backup binding sidecars are incomplete; refusing a partial disaster restore.")
try:
    document = json.loads(manifest.read_text(encoding="utf-8"))
    bindings = json.loads(agents.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"Backup binding sidecar is unreadable: {type(exc).__name__}")
if document.get("format_version") != 1:
    raise SystemExit("Unsupported Docker backup manifest version.")
if document.get("archive") != archive.name or document.get("agents_file") != agents.name:
    raise SystemExit("Docker backup manifest filenames do not match the selected backup.")
def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()
if not hmac.compare_digest(digest(archive), str(document.get("archive_sha256") or "")):
    raise SystemExit("Backup ZIP checksum does not match its Docker manifest.")
if not hmac.compare_digest(digest(agents), str(document.get("agents_sha256") or "")):
    raise SystemExit("agents.json checksum does not match its Docker manifest.")
items = bindings.get("agents") if isinstance(bindings, dict) else None
if not isinstance(items, dict) or not items:
    raise SystemExit("Backed-up agents.json has no Agent bindings.")
invalid = sorted(
    str(agent_id)
    for agent_id, item in items.items()
    if not isinstance(item, dict)
    or not isinstance(item.get("token_env"), str)
    or not item["token_env"].strip()
)
if invalid:
    raise SystemExit(
        "Backed-up agents.json has invalid token_env bindings for: "
        + ", ".join(invalid)
    )
missing = sorted({
    item["token_env"].strip()
    for item in items.values()
    if not os.environ.get(item["token_env"].strip())
})
if missing:
    raise SystemExit("Set the restored Agent token variables in .env before restore: " + ", ".join(missing))
print("bundle")
' "/backups/$archive" "/backups/$agents_name" "/backups/$manifest_name")
case "$bundle_status" in
    bundle) ;;
    legacy)
        printf '%s\n' "Warning: legacy ZIP has no agents.json sidecar; current Agent bindings will be retained." >&2
        ;;
    *)
        printf '%s\n' "Unexpected backup verification result: $bundle_status" >&2
        exit 1
        ;;
esac

# The API can remain online while a one-off worker creates a consistent,
# non-pruning pre-restore snapshot of the current state.
compose run --rm --no-deps -e META_MEMORY_BACKUP_PRUNE=false worker meta-memory-backup
compose stop meta-memory
compose run --rm --no-deps meta-memory \
    meta-memory --config /config/config.toml restore "/backups/$archive" --destination /data/store --force
if [ "$bundle_status" = "bundle" ]; then
    compose run --rm --no-deps --entrypoint python meta-memory -c '
import os
import shutil
import sys
import uuid
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
destination.parent.mkdir(parents=True, exist_ok=True)
temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
try:
    shutil.copyfile(source, temporary)
    os.chmod(temporary, 0o600)
    if hasattr(os, "chown"):
        os.chown(temporary, int(os.environ["META_MEMORY_UID"]), int(os.environ["META_MEMORY_GID"]))
    os.replace(temporary, destination)
finally:
    temporary.unlink(missing_ok=True)
' "/backups/$agents_name" /config/agents.json
fi
# The worker state is intentionally outside the restored store. Clear only its
# success markers so the restored data receives a fresh Dream and backup; lock
# files remain harmless and are reacquired by the restarted processes.
compose run --rm --no-deps --entrypoint /bin/sh worker -c \
    'rm -f /data/.container-runtime/worker/dream.last-success-date /data/.container-runtime/worker/backup.last-success-epoch'

restart_services
trap - EXIT HUP INT TERM

attempt=0
until compose exec -T meta-memory python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/readyz', timeout=2).read()" >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 30 ] || {
        printf '%s\n' "Restore completed, but the API did not become ready within 60 seconds." >&2
        exit 1
    }
    sleep 2
done

compose run --rm --no-deps meta-memory \
    meta-memory --json --config /config/config.toml doctor
printf '%s\n' "Restore completed from $archive"
