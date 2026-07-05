"""Backup and rollback: hub-only content, local storage.

Layout:
    <backup_path>/<timestamp>-<uuid8>/hub.tar
    <backup_path>/<timestamp>-<uuid8>/hashes.json

Does NOT backup agent dirs (links can be recreated via `skillmesh apply`).
Backup path is LOCAL (default ~/.local/state/skillmesh/backups/),
NOT inside hub (to avoid cloud drive syncing it).

See docs/PRD.md §9.8, docs/ARCHITECTURE.md §11.4.
"""
import hashlib
import json
import os
import tarfile
import time
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import List, Optional

from . import distribution
from .platform_support import system_name


BACKUP_TARGETS = [
    "snapshot.json",
    "manifest.json",
    "events",
    "blobs",
    "skills",
    ".uninstalled",
]


class BackupError(Exception):
    pass


def backup(hub_path: Path, backup_root: Path) -> Path:
    """Create a full backup of hub content. Returns backup directory path."""
    backup_root = backup_root.expanduser()
    backup_root.mkdir(parents=True, exist_ok=True)

    timestamp = time.time_ns()
    uid = uuid.uuid4().hex[:8]
    backup_dir = backup_root / f"{timestamp}-{uid}"
    backup_dir.mkdir()

    tar_path = backup_dir / "hub.tar"
    hashes_path = backup_dir / "hashes.json"

    import tarfile
    with tarfile.open(tar_path, "w:gz") as tf:
        for name in BACKUP_TARGETS:
            src = hub_path / name
            if src.exists():
                tf.add(src, arcname=name)

    hashes = _compute_hashes(tar_path)
    hashes_path.write_text(json.dumps(hashes, sort_keys=True))

    return backup_dir


def rollback(backup_dir: Path, hub_path: Path) -> None:
    """Restore hub from backup. Verifies hashes first."""
    tar_path = backup_dir / "hub.tar"
    hashes_path = backup_dir / "hashes.json"

    if not tar_path.exists():
        raise BackupError(f"backup tar not found: {tar_path}")

    # Verify hash
    if hashes_path.exists():
        expected = json.loads(hashes_path.read_text())
        actual = _compute_hashes(tar_path)
        if expected != actual:
            raise BackupError(
                f"backup hash mismatch - refusing to restore. "
                f"expected={expected.get('sha256')}, "
                f"actual={actual.get('sha256')}"
            )

    # Validate every archive member before touching the current hub.
    with tarfile.open(tar_path, "r:gz") as tf:
        _validate_members(tf, hub_path)

        import shutil
        for name in BACKUP_TARGETS:
            target = hub_path / name
            if target.is_symlink():
                target.unlink()
            elif distribution.is_junction(target):
                os.rmdir(target)
            elif target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()

        _safe_extract(tf, hub_path)


def _safe_extract(tar: "tarfile.TarFile", dest: Path) -> None:
    """Safely extract tar, refusing path traversal / symlinks / hardlinks.

    Prevents:
    - absolute paths (e.g., /etc/passwd)
    - paths containing .. (path traversal)
    - symlinks / hardlinks pointing outside dest
    - any member resolving outside dest

    See security review B3.
    """
    _validate_members(tar, dest)
    try:
        tar.extractall(dest, filter="data")
    except TypeError:
        tar.extractall(dest)


def _validate_members(tar: "tarfile.TarFile", dest: Path) -> None:
    """Validate all archive paths without changing the destination."""
    dest_resolved = dest.resolve()
    for member in tar.getmembers():
        posix_name = PurePosixPath(member.name)
        windows_name = PureWindowsPath(member.name)
        if (posix_name.is_absolute() or windows_name.is_absolute()
                or windows_name.drive or ".." in posix_name.parts
                or ".." in windows_name.parts
                or (system_name() == "Windows"
                    and any(":" in part for part in windows_name.parts))):
            raise BackupError(
                f"cross-platform path traversal detected: {member.name}"
            )
        # Reject symlinks/hardlinks (could escape dest)
        if member.issym() or member.islnk():
            raise BackupError(
                f"refusing to extract symlink/hardlink member: {member.name}"
            )
        if not (member.isfile() or member.isdir()):
            raise BackupError(
                f"refusing to extract special archive member: {member.name}"
            )
        # Compute resolved target path
        member_path = (dest / member.name).resolve()
        # Must be inside dest
        try:
            member_path.relative_to(dest_resolved)
        except ValueError:
            raise BackupError(
                f"path traversal detected, refusing to extract: {member.name}"
            )
        # Also block absolute paths explicitly (defense in depth)
        if member.name.startswith("/"):
            raise BackupError(
                f"absolute path in tar, refusing to extract: {member.name}"
            )


def list_backups(backup_root: Path) -> List[dict]:
    """List all backups, newest first."""
    backup_root = backup_root.expanduser()
    if not backup_root.exists():
        return []

    backups: List[dict] = []
    for d in backup_root.iterdir():
        if not d.is_dir():
            continue
        tar = d / "hub.tar"
        if not tar.exists():
            continue
        stat = tar.stat()
        backups.append({
            "dir": str(d),
            "timestamp": int(d.name.split("-")[0]),
            "size": stat.st_size,
        })

    backups.sort(key=lambda b: int(b["timestamp"]), reverse=True)
    return backups


def find_latest_backup(backup_root: Path) -> Optional[Path]:
    """Find the most recent backup directory."""
    backups = list_backups(backup_root)
    if not backups:
        return None
    return Path(backups[0]["dir"])


def _compute_hashes(file_path: Path) -> dict:
    """Compute sha256 of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return {"sha256": h.hexdigest(), "size": file_path.stat().st_size}
