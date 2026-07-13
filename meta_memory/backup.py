"""Consistent portable backup and guarded restore for a local memory store."""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def backup_store(store: str | Path, destination: str | Path | None = None) -> dict[str, object]:
    root = Path(store).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Memory store does not exist: {root}")
    target = Path(destination).expanduser().resolve() if destination else root.parent / f"meta-memory-backup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    database = root / "db" / "memory_index.sqlite"
    with tempfile.TemporaryDirectory(prefix="meta-memory-backup-") as temp:
        snapshot = Path(temp) / "memory_index.sqlite"
        if database.exists():
            source = sqlite3.connect(database)
            copied = sqlite3.connect(snapshot)
            source.backup(copied)
            copied.close(); source.close()
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in root.rglob("*"):
                if not path.is_file() or path.name.endswith(("-wal", "-shm")):
                    continue
                relative = path.relative_to(root)
                archive.write(snapshot if path == database and snapshot.exists() else path, Path("store") / relative)
    return {"status": "ok", "backup": str(target), "store": str(root)}


def restore_store(archive_path: str | Path, destination: str | Path, *, force: bool = False) -> dict[str, object]:
    archive_file = Path(archive_path).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    if not archive_file.is_file():
        raise FileNotFoundError(f"Backup archive does not exist: {archive_file}")
    if target.exists() and any(target.iterdir()) and not force:
        raise ValueError("Restore destination is not empty; pass --force only after confirming its contents can be replaced.")
    with tempfile.TemporaryDirectory(prefix="meta-memory-restore-") as temp:
        stage = Path(temp)
        with zipfile.ZipFile(archive_file) as archive:
            for member in archive.infolist():
                path = (stage / member.filename).resolve()
                if not str(path).startswith(str(stage.resolve())):
                    raise ValueError("Backup contains an unsafe path.")
            archive.extractall(stage)
        source = stage / "store"
        if not source.is_dir():
            raise ValueError("Backup does not contain a Meta Memory store.")
        if target.exists() and force:
            shutil.rmtree(target)
        shutil.copytree(source, target, dirs_exist_ok=True)
    return {"status": "ok", "restored_to": str(target), "backup": str(archive_file)}
