"""Test backup and rollback: hub-only content, hash verification.

Covers F8.1-F8.7, T1, T16.
"""
import json
import os
from pathlib import Path

import pytest

from skillmesh import backup


def _make_hub(tmp_path):
    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "skills").mkdir()
    (hub / "skills" / "test-skill").mkdir()
    (hub / "skills" / "test-skill" / "SKILL.md").write_text("# test")
    (hub / "events").mkdir()
    (hub / "blobs").mkdir()
    (hub / ".uninstalled").mkdir()
    (hub / "snapshot.json").write_text('{"version":1,"skills":{},"included_events":[]}')
    (hub / "manifest.json").write_text('{"version":1,"skills":{}}')
    return hub


def test_backup_creates_tar_with_hash(tmp_path):
    """F8.1: backup creates tar with hash file."""
    hub = _make_hub(tmp_path)
    backup_root = tmp_path / "backups"

    backup_dir = backup.backup(hub, backup_root)

    assert (backup_dir / "hub.tar").exists()
    assert (backup_dir / "hashes.json").exists()
    hashes = json.loads((backup_dir / "hashes.json").read_text())
    assert "sha256" in hashes
    assert hashes["size"] > 0


def test_rollback_unlinks_junction_instead_of_traversing(tmp_path, monkeypatch):
    """Windows reparse points must not be recursively deleted on rollback."""
    hub = tmp_path / "hub"
    events = hub / "events"
    events.mkdir(parents=True)
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()

    import tarfile
    tar_path = backup_dir / "hub.tar"
    with tarfile.open(tar_path, "w:gz"):
        pass
    (backup_dir / "hashes.json").write_text(
        json.dumps(backup._compute_hashes(tar_path))
    )

    removed = []
    monkeypatch.setattr(
        backup.distribution, "is_junction", lambda path: path == events
    )
    real_rmdir = backup.os.rmdir

    def safe_rmdir(path):
        removed.append(Path(path))
        real_rmdir(path)

    monkeypatch.setattr(backup.os, "rmdir", safe_rmdir)
    backup.rollback(backup_dir, hub)
    assert removed == [events]
    assert not events.exists()


def test_backup_includes_all_targets(tmp_path):
    """F8.1: backup tar includes snapshot/events/blobs/skills/.uninstalled/manifest."""
    hub = _make_hub(tmp_path)
    backup_root = tmp_path / "backups"

    backup_dir = backup.backup(hub, backup_root)

    import tarfile
    with tarfile.open(backup_dir / "hub.tar", "r:gz") as tf:
        names = tf.getnames()
    assert "snapshot.json" in names
    assert "events" in names
    assert "blobs" in names
    assert "skills" in names
    assert ".uninstalled" in names
    assert "manifest.json" in names


def test_backup_does_not_include_agent_dirs(tmp_path):
    """F8.2: backup does NOT include agent directories."""
    hub = _make_hub(tmp_path)
    backup_root = tmp_path / "backups"

    backup_dir = backup.backup(hub, backup_root)

    import tarfile
    with tarfile.open(backup_dir / "hub.tar", "r:gz") as tf:
        names = tf.getnames()
    # No agent dirs in tar
    assert not any(".codex" in n for n in names)
    assert not any(".cursor" in n for n in names)


def test_backup_naming_unique(tmp_path):
    """F8.4/F8.5: multiple backups get unique names (timestamp-uuid8)."""
    hub = _make_hub(tmp_path)
    backup_root = tmp_path / "backups"

    d1 = backup.backup(hub, backup_root)
    d2 = backup.backup(hub, backup_root)
    assert d1 != d2
    assert d1.parent == d2.parent == backup_root


def test_rollback_restores_hub(tmp_path):
    """T1/T16: rollback restores hub content after deletion."""
    hub = _make_hub(tmp_path)
    backup_root = tmp_path / "backups"

    backup_dir = backup.backup(hub, backup_root)

    # Delete hub contents
    import shutil
    shutil.rmtree(hub / "skills")
    shutil.rmtree(hub / "events")
    (hub / "snapshot.json").unlink()
    (hub / "manifest.json").unlink()

    # Rollback
    backup.rollback(backup_dir, hub)

    # Verify restored
    assert (hub / "snapshot.json").exists()
    assert (hub / "skills" / "test-skill" / "SKILL.md").read_text() == "# test"


def test_rollback_rejects_corrupt_backup(tmp_path):
    """F8.4: rollback with hash mismatch is refused."""
    hub = _make_hub(tmp_path)
    backup_root = tmp_path / "backups"
    backup_dir = backup.backup(hub, backup_root)

    # Tamper with tar
    (backup_dir / "hub.tar").write_bytes(b"tampered")

    with pytest.raises(backup.BackupError, match="hash mismatch"):
        backup.rollback(backup_dir, hub)


def test_list_backups_returns_newest_first(tmp_path):
    """list_backups returns list sorted by timestamp desc."""
    hub = _make_hub(tmp_path)
    backup_root = tmp_path / "backups"

    b1 = backup.backup(hub, backup_root)
    b2 = backup.backup(hub, backup_root)

    backups = backup.list_backups(backup_root)
    assert len(backups) == 2
    assert backups[0]["timestamp"] >= backups[1]["timestamp"]


def test_find_latest_backup(tmp_path):
    hub = _make_hub(tmp_path)
    backup_root = tmp_path / "backups"
    backup.backup(hub, backup_root)
    backup.backup(hub, backup_root)

    latest = backup.find_latest_backup(backup_root)
    assert latest is not None
    assert latest.exists()


def test_find_latest_backup_returns_none_when_empty(tmp_path):
    backup_root = tmp_path / "empty"
    backup_root.mkdir()
    assert backup.find_latest_backup(backup_root) is None


def test_safe_extract_rejects_path_traversal(tmp_path):
    """B3: rollback refuses to extract tar with path traversal members."""
    hub = _make_hub(tmp_path)
    backup_root = tmp_path / "backups"
    backup_dir = backup.backup(hub, backup_root)

    # Replace tar with one containing traversal member
    import tarfile
    import io
    tar_path = backup_dir / "hub.tar"

    # Build malicious tar
    data = b"evil content"
    member = tarfile.TarInfo(name="../../../etc/evil")
    member.size = len(data)
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.addfile(member, io.BytesIO(data))

    # Update hashes to match (so we get to extract step)
    import hashlib
    h = hashlib.sha256()
    with open(tar_path, "rb") as f:
        h.update(f.read())
    hashes = {"sha256": h.hexdigest(), "size": tar_path.stat().st_size}
    (backup_dir / "hashes.json").write_text(json.dumps(hashes))

    with pytest.raises(backup.BackupError, match="traversal|symlink"):
        backup.rollback(backup_dir, hub)
    assert (hub / "skills" / "test-skill" / "SKILL.md").exists()


def test_safe_extract_rejects_symlink_member(tmp_path):
    """B3: rollback refuses to extract tar with symlink members."""
    hub = _make_hub(tmp_path)
    backup_root = tmp_path / "backups"
    backup_dir = backup.backup(hub, backup_root)

    import tarfile
    tar_path = backup_dir / "hub.tar"

    # Build tar with symlink member
    member = tarfile.TarInfo(name="evil-link")
    member.type = tarfile.SYMTYPE
    member.linkname = "/etc/passwd"
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.addfile(member)

    import hashlib
    h = hashlib.sha256()
    with open(tar_path, "rb") as f:
        h.update(f.read())
    hashes = {"sha256": h.hexdigest(), "size": tar_path.stat().st_size}
    (backup_dir / "hashes.json").write_text(json.dumps(hashes))

    with pytest.raises(backup.BackupError, match="symlink"):
        backup.rollback(backup_dir, hub)
    assert (hub / "skills" / "test-skill" / "SKILL.md").exists()
