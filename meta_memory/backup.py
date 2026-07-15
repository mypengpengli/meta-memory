"""Portable, verified backup and guarded restore for a local Meta Memory store.

The public ``backup_store`` / ``restore_store`` helpers predate the public
configuration object and remain available for integrations.  New CLI calls
use ``backup_app`` / ``restore_app`` so the archive also carries the user
configuration needed to move a complete installation to another machine.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import AppConfig


FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
CHECKSUMS_NAME = "checksums.sha256"
STORE_PREFIX = "store"
CONFIG_NAME = "config.toml"
DATABASE_RELATIVE_PATH = Path("store") / "db" / "memory_index.sqlite"
MAX_ARCHIVE_MEMBERS = 50_000
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024  # two GiB; local backups should stay bounded
MAX_MEMBER_BYTES = 512 * 1024 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _app_version() -> str:
    try:
        from . import __version__

        return str(__version__)
    except Exception:  # pragma: no cover - defensive for partially installed packages
        return "unknown"


def _default_destination(root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return root.parent / f"meta-memory-backup-{stamp}.zip"


def _normal_relative(value: str | Path) -> str:
    """Return a canonical ZIP member path or reject an unsafe one."""
    raw = str(value)
    if not raw or "\x00" in raw:
        raise ValueError("Backup contains an empty or invalid path.")
    # ZIP names are POSIX paths.  Treat backslashes as unsafe rather than
    # letting a Windows extractor reinterpret them as separators later.
    if "\\" in raw:
        raise ValueError("Backup contains a path with an unsupported separator.")
    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise ValueError("Backup contains an absolute path.")
    parts = posix.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Backup contains an unsafe relative path.")
    if any(":" in part for part in parts):
        raise ValueError("Backup contains a path that is not portable to Windows.")
    normalized = "/".join(parts)
    if normalized != raw.rstrip("/"):
        raise ValueError("Backup contains a non-canonical path.")
    return normalized


def _safe_stage_path(stage: Path, member_name: str) -> Path:
    relative = _normal_relative(member_name)
    candidate = (stage / relative).resolve()
    try:
        candidate.relative_to(stage.resolve())
    except ValueError as exc:
        raise ValueError("Backup contains an unsafe path.") from exc
    return candidate


def _validate_zip_member(info: zipfile.ZipInfo) -> None:
    name = info.filename.rstrip("/")
    if not name:
        return
    _normal_relative(name)
    if info.file_size < 0 or info.file_size > MAX_MEMBER_BYTES:
        raise ValueError("Backup contains an oversized file.")
    # Unix symlinks are represented in the high 16 bits.  Never extract one:
    # a later copy/resolve could otherwise escape the staging directory.
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise ValueError("Backup contains a symbolic link, which is not supported.")
    file_type = stat.S_IFMT(mode)
    if mode and file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ValueError("Backup contains a non-regular file.")


def _archive_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise ValueError("Backup contains too many files.")
    total = 0
    seen: set[str] = set()
    files: list[zipfile.ZipInfo] = []
    for info in members:
        _validate_zip_member(info)
        name = info.filename.rstrip("/")
        if not name:
            continue
        normalized = _normal_relative(name)
        if normalized in seen:
            raise ValueError("Backup contains duplicate paths.")
        seen.add(normalized)
        if info.is_dir():
            continue
        total += info.file_size
        if total > MAX_ARCHIVE_BYTES:
            raise ValueError("Backup is too large to restore safely.")
        files.append(info)
    return files


def _extract_to_stage(archive: zipfile.ZipFile, stage: Path) -> set[str]:
    names: set[str] = set()
    for info in _archive_members(archive):
        relative = _normal_relative(info.filename)
        target = _safe_stage_path(stage, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info, "r") as source, target.open("xb") as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
        names.add(relative)
    return names


def _parse_manifest(stage: Path, *, allow_legacy: bool) -> tuple[dict[str, object] | None, bool]:
    manifest_path = stage / MANIFEST_NAME
    checksums_path = stage / CHECKSUMS_NAME
    if not manifest_path.exists() and not checksums_path.exists():
        if allow_legacy:
            return None, True
        raise ValueError("Backup is missing its manifest and checksums.")
    if not manifest_path.is_file() or not checksums_path.is_file():
        raise ValueError("Backup manifest is incomplete.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Backup manifest is not valid JSON.") from exc
    if not isinstance(manifest, dict) or manifest.get("format_version") != FORMAT_VERSION:
        raise ValueError("Backup format version is unsupported.")
    if manifest.get("store_relative_path", STORE_PREFIX) != STORE_PREFIX:
        raise ValueError("Backup manifest has an invalid store path.")
    database_path = manifest.get("database_relative_path")
    if database_path not in {None, str(DATABASE_RELATIVE_PATH).replace("\\", "/")}:
        raise ValueError("Backup manifest has an invalid database path.")
    config_path = manifest.get("config_relative_path")
    if config_path not in {None, CONFIG_NAME}:
        raise ValueError("Backup manifest has an invalid config path.")
    versions = manifest.get("schema_versions", [])
    if not isinstance(versions, list) or any(not isinstance(item, str) for item in versions):
        raise ValueError("Backup manifest has invalid migration metadata.")
    return manifest, False


def _parse_checksums(stage: Path) -> dict[str, str]:
    expected: dict[str, str] = {}
    try:
        lines = (stage / CHECKSUMS_NAME).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("Backup checksums cannot be read.") from exc
    for line in lines:
        if not line.strip():
            continue
        try:
            digest, raw_name = line.split("  ", 1)
        except ValueError as exc:
            raise ValueError("Backup checksums have an invalid line.") from exc
        name = _normal_relative(raw_name)
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("Backup checksums have an invalid digest.")
        if name in expected:
            raise ValueError("Backup checksums contain duplicate paths.")
        expected[name] = digest
    if not expected:
        raise ValueError("Backup checksums are empty.")
    return expected


def _validate_archive_payload(stage: Path, names: set[str], manifest: dict[str, object] | None, legacy: bool) -> None:
    source = stage / STORE_PREFIX
    if not source.is_dir():
        raise ValueError("Backup does not contain a Meta Memory store.")
    if legacy:
        return
    expected = _parse_checksums(stage)
    allowed_meta = {MANIFEST_NAME, CHECKSUMS_NAME}
    payload_names = names - allowed_meta
    if payload_names != set(expected):
        raise ValueError("Backup files do not match its checksum manifest.")
    for name, digest in expected.items():
        path = _safe_stage_path(stage, name)
        if not path.is_file() or _sha256_file(path) != digest:
            raise ValueError(f"Backup checksum verification failed for {name}.")
    assert manifest is not None
    database_rel = str(manifest.get("database_relative_path") or "")
    database_digest = str(manifest.get("database_sha256") or "")
    if database_rel:
        if expected.get(database_rel) != database_digest:
            raise ValueError("Backup database checksum does not match its manifest.")
    config_rel = manifest.get("config_relative_path")
    if config_rel and str(config_rel) not in expected:
        raise ValueError("Backup config is missing from its checksum manifest.")


def _integrity_check(database: Path, *, required: bool) -> None:
    if not database.exists():
        if required:
            raise ValueError("Backup is missing its SQLite database.")
        return
    try:
        # Backups contain a complete snapshot, so ``immutable=1`` is safe and
        # prevents SQLite from creating a transient -wal/-shm beside it while
        # merely validating it.
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            row = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ValueError("Backup SQLite database cannot be opened.") from exc
    if not row or str(row[0]).casefold() != "ok":
        raise ValueError("Backup SQLite integrity check failed.")


def _migration_versions(database: Path) -> list[str]:
    if not database.exists():
        return []
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if not exists:
                return []
            return [str(row[0]) for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RuntimeError("Could not read SQLite migration metadata for backup.") from exc


def _snapshot_database(database: Path, destination: Path) -> None:
    """Copy a SQLite database with SQLite's online backup API, never its WAL."""
    source: sqlite3.Connection | None = None
    copied: sqlite3.Connection | None = None
    try:
        source = sqlite3.connect(str(database), timeout=20)
        copied = sqlite3.connect(str(destination))
        source.backup(copied)
        copied.commit()
    except sqlite3.Error as exc:
        raise RuntimeError("Could not create a consistent SQLite backup snapshot.") from exc
    finally:
        if copied is not None:
            copied.close()
        if source is not None:
            source.close()
    _integrity_check(destination, required=True)


def _is_transient(relative: Path) -> bool:
    name = relative.name.casefold()
    return name.endswith(("-wal", "-shm", "-journal", ".tmp", ".temp", "~"))


def _stage_store(root: Path, stage: Path, *, target: Path) -> Path:
    staged_store = stage / STORE_PREFIX
    database = root / "db" / "memory_index.sqlite"
    snapshot = stage / "memory_index.sqlite"
    if database.exists():
        _snapshot_database(database, snapshot)
    for path in root.rglob("*"):
        if path.is_symlink():
            # Do not accidentally back up data outside the store.  Symlinks
            # are not portable across machines either.
            continue
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if _is_transient(relative) or path.resolve() == target:
            continue
        destination = staged_store / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if path == database and snapshot.exists():
            shutil.copy2(snapshot, destination)
        else:
            shutil.copy2(path, destination)
    for transient in (snapshot, Path(str(snapshot) + "-wal"), Path(str(snapshot) + "-shm"), Path(str(snapshot) + "-journal")):
        if transient.exists():
            transient.unlink()
    # Keep the layout usable even for an initialized but otherwise empty store.
    staged_store.mkdir(parents=True, exist_ok=True)
    return staged_store


def _remove_sqlite_sidecars(root: Path) -> None:
    """Remove transient files next to a complete SQLite backup snapshot."""
    database = root / "db" / "memory_index.sqlite"
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(database) + suffix)
        if sidecar.exists():
            sidecar.unlink()


def _stage_config(stage: Path, config: "AppConfig" | None) -> Path | None:
    if config is None:
        return None
    destination = stage / CONFIG_NAME
    source = Path(config.path).expanduser()
    if source.is_file():
        shutil.copy2(source, destination)
        return destination
    # An in-memory AppConfig can still be backed up without first mutating the
    # user's live config file.
    from .config import save_config

    snapshot_config = replace(config, path=destination)
    save_config(snapshot_config)
    return destination


def _write_checksums(stage: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for path in sorted(stage.rglob("*")):
        if not path.is_file() or path.name in {MANIFEST_NAME, CHECKSUMS_NAME}:
            continue
        relative = path.relative_to(stage).as_posix()
        entries[relative] = _sha256_file(path)
    lines = [f"{digest}  {name}" for name, digest in sorted(entries.items())]
    (stage / CHECKSUMS_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return entries


def _build_backup(
    root: Path,
    destination: str | Path | None,
    *,
    config: "AppConfig" | None,
) -> dict[str, object]:
    if not root.is_dir():
        raise FileNotFoundError(f"Memory store does not exist: {root}")
    target = Path(destination).expanduser().resolve() if destination else _default_destination(root)
    if target.exists() and target.is_dir():
        raise ValueError("Backup output must be a ZIP file path, not a directory.")
    target.parent.mkdir(parents=True, exist_ok=True)
    database = root / "db" / "memory_index.sqlite"
    with tempfile.TemporaryDirectory(prefix="meta-memory-backup-") as temporary:
        stage = Path(temporary)
        _stage_store(root, stage, target=target)
        config_path = _stage_config(stage, config)
        schema_versions = _migration_versions(stage / DATABASE_RELATIVE_PATH)
        _remove_sqlite_sidecars(stage / STORE_PREFIX)
        checksums = _write_checksums(stage)
        database_name = DATABASE_RELATIVE_PATH.as_posix()
        manifest: dict[str, object] = {
            "format_version": FORMAT_VERSION,
            "app_version": _app_version(),
            "created_at": _utc_now(),
            "user_id": config.user_id if config is not None else "unknown",
            "store_relative_path": STORE_PREFIX,
            "config_relative_path": CONFIG_NAME if config_path is not None else None,
            "database_relative_path": database_name if database.exists() else None,
            "database_sha256": checksums.get(database_name, ""),
            "schema_versions": schema_versions,
        }
        (stage / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        # Build the archive only after all payloads and checksums are final.
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(stage).as_posix())
    return {
        "status": "ok",
        "backup": str(target),
        "store": str(root),
        "format_version": FORMAT_VERSION,
        "manifest": manifest,
    }


def backup_store(store: str | Path, destination: str | Path | None = None) -> dict[str, object]:
    """Back up a store while preserving the original store-only API.

    Prefer :func:`backup_app` from the CLI: it adds ``config.toml`` to the
    same verified format.  Store-only archives are deliberately restorable by
    ``restore_store`` for older integrations.
    """
    return _build_backup(Path(store).expanduser().resolve(), destination, config=None)


def backup_app(config: "AppConfig", destination: str | Path | None = None) -> dict[str, object]:
    """Create a portable archive containing both configuration and store."""
    return _build_backup(Path(config.store).expanduser().resolve(), destination, config=config)


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _validate_restore_target(target: Path, archive_file: Path) -> None:
    if target == Path(target.anchor):
        raise ValueError("Refusing to restore into a filesystem root.")
    if target.exists() and target.is_symlink():
        raise ValueError("Restore destination may not be a symbolic link.")
    if target.exists() and not target.is_dir():
        raise ValueError("Restore destination must be a directory.")
    if _inside(archive_file, target):
        raise ValueError("Backup archive must not be stored inside the restore destination.")


def _prepare_store_for_restore(stage_store: Path) -> dict[str, object]:
    """Run migrations and Doctor against the sibling staging copy first."""
    _integrity_check(stage_store / "db" / "memory_index.sqlite", required=False)
    from .legacy import bootstrap

    bootstrap()
    from _common import ensure_store_ready
    from doctor import doctor

    ensure_store_ready(stage_store)
    health = doctor(stage_store)
    if health.get("active_claims_without_sources"):
        # The archive is not corrupt, but this should be visible to the user.
        health = {**health, "status": "warning"}
    return health


def _swap_staged_store(staged_store: Path, target: Path, *, force: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.exists()
    if existing and any(target.iterdir()) and not force:
        raise ValueError("Restore destination is not empty; pass --force only after confirming its contents can be replaced.")
    staging_parent = target.parent
    incoming = staging_parent / f".meta-memory-restore-{uuid.uuid4().hex}"
    rollback = staging_parent / f".meta-memory-pre-restore-{uuid.uuid4().hex}"
    try:
        shutil.copytree(staged_store, incoming)
        if existing:
            target.replace(rollback)
        try:
            incoming.replace(target)
        except Exception:
            if existing and rollback.exists() and not target.exists():
                rollback.replace(target)
            raise
        if rollback.exists():
            shutil.rmtree(rollback)
    finally:
        if incoming.exists():
            shutil.rmtree(incoming)
        # If a failed move left the rollback beside the target, preserve it
        # rather than silently deleting the only remaining prior store.


def _load_archived_config(config: "AppConfig", stage: Path, target_store: Path) -> "AppConfig" | None:
    archived = stage / CONFIG_NAME
    if not archived.is_file():
        return None
    from .config import load_config

    try:
        restored = load_config(archived)
    except (OSError, ValueError) as exc:
        raise ValueError("Backup configuration could not be loaded.") from exc
    restored.path = Path(config.path).expanduser()
    restored.store = target_store
    return restored


def _apply_restored_config(config: "AppConfig", restored: "AppConfig") -> str:
    from .config import save_config

    save_config(restored)
    # Keep the live object usable for callers that restore and then continue in
    # the same Python process (the CLI itself is a fresh process either way).
    config.user_name = restored.user_name
    config.user_id = restored.user_id
    config.store = restored.store
    config.memory_mode = restored.memory_mode
    config.default_project = restored.default_project
    config.search_depth = restored.search_depth
    config.maintenance_enabled = restored.maintenance_enabled
    config.maintenance_interval_minutes = restored.maintenance_interval_minutes
    config.dream_enabled = restored.dream_enabled
    config.dream_schedule = restored.dream_schedule
    config.dream_scan_days = restored.dream_scan_days
    config.dream_provider = restored.dream_provider
    config.dream_command = restored.dream_command
    config.dream_heartbeat_enabled = restored.dream_heartbeat_enabled
    config.dream_heartbeat_interval_minutes = restored.dream_heartbeat_interval_minutes
    config.dream_heartbeat_max_scopes = restored.dream_heartbeat_max_scopes
    config.dream_heartbeat_max_jobs = restored.dream_heartbeat_max_jobs
    config.dream_deep_enabled = restored.dream_deep_enabled
    config.dream_deep_schedule = restored.dream_deep_schedule
    config.dream_deep_scan_days = restored.dream_deep_scan_days
    config.history_scope = restored.history_scope
    config.history_allow_detail = restored.history_allow_detail
    config.history_detail_max_sessions = restored.history_detail_max_sessions
    config.history_detail_max_turns = restored.history_detail_max_turns
    config.history_detail_max_chars = restored.history_detail_max_chars
    config.history_tool_summary_max_chars = restored.history_tool_summary_max_chars
    config.turns_unfinished_warning_minutes = restored.turns_unfinished_warning_minutes
    config.turns_abandon_after_minutes = restored.turns_abandon_after_minutes
    config.session_auto_expire_hours = restored.session_auto_expire_hours
    config.top_k = restored.top_k
    config.embeddings = restored.embeddings
    config.http_api = restored.http_api
    config.agent_private_memory = restored.agent_private_memory
    config.projects = restored.projects
    return str(restored.path)


def _restore(
    archive_path: str | Path,
    destination: str | Path,
    *,
    force: bool,
    config: "AppConfig" | None,
) -> dict[str, object]:
    archive_file = Path(archive_path).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    if not archive_file.is_file():
        raise FileNotFoundError(f"Backup archive does not exist: {archive_file}")
    _validate_restore_target(target, archive_file)
    with tempfile.TemporaryDirectory(prefix="meta-memory-restore-") as temporary:
        stage = Path(temporary) / "archive"
        stage.mkdir(parents=True)
        try:
            with zipfile.ZipFile(archive_file) as archive:
                names = _extract_to_stage(archive, stage)
        except zipfile.BadZipFile as exc:
            raise ValueError("Backup archive is not a valid ZIP file.") from exc
        manifest, legacy = _parse_manifest(stage, allow_legacy=True)
        _validate_archive_payload(stage, names, manifest, legacy)
        source = stage / STORE_PREFIX
        required_database = bool(manifest and manifest.get("database_relative_path"))
        _integrity_check(source / "db" / "memory_index.sqlite", required=required_database)
        restored_config = _load_archived_config(config, stage, target) if config is not None else None
        # Migrations and Doctor run against a throwaway sibling copy before the
        # user's destination is touched.  This is the practical transaction
        # boundary around a filesystem restore.
        prepared = Path(temporary) / "prepared-store"
        shutil.copytree(source, prepared)
        health = _prepare_store_for_restore(prepared)
        _swap_staged_store(prepared, target, force=force)
        config_path = _apply_restored_config(config, restored_config) if config is not None and restored_config is not None else None
    warnings: list[str] = []
    if legacy:
        warnings.append("Legacy store-only backup restored; no configuration was included.")
    if config is not None and config_path is None:
        warnings.append("Backup did not contain config.toml; the current configuration was retained.")
    result: dict[str, object] = {
        "status": "ok",
        "restored_to": str(target),
        "backup": str(archive_file),
        "format_version": None if legacy else FORMAT_VERSION,
        "manifest": manifest,
        "health": health,
        "config": config_path,
        "scheduler": "Run `meta-memory schedule install` to install local tasks on this machine.",
    }
    if warnings:
        result["warnings"] = warnings
    return result


def restore_store(archive_path: str | Path, destination: str | Path, *, force: bool = False) -> dict[str, object]:
    """Restore only a store, preserving the established public signature."""
    return _restore(archive_path, destination, force=force, config=None)


def restore_app(
    config: "AppConfig",
    archive_path: str | Path,
    destination: str | Path | None = None,
    *,
    force: bool = False,
) -> dict[str, object]:
    """Restore a portable archive and re-point the restored config at its store."""
    target = destination if destination is not None else config.store
    return _restore(archive_path, target, force=force, config=config)
