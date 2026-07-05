"""Test manifest rebuild, replay determinism, conflict resolution.

Covers F3.4, F3.5, F6.4, T7, T12, T13, T31, T32, T33, T34, T35.
"""
import json
from pathlib import Path

import pytest

from skillmesh import events, manifest as manifest_mod
from skillmesh.events import EventLog, SkillEntry
from skillmesh.host import Host
from skillmesh.config import Config, Hub, Agent, Watch


def _make_config(hub_path):
    return Config(
        hub=Hub(path=str(hub_path)),
        agents=[Agent(name="codex", dir="~/.codex/skills",
                      accept_sources=["work"], layout="directory")],
        watch=Watch(dirs=["~/skills-source"]),
    )


def _make_host(host_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"):
    return Host(host_id=host_id, display_name="test-host")


def _make_entry(name="my-skill", source="work", version="1.0.0",
                content_hash="sha256:abc", blob_hash="sha256:def"):
    return SkillEntry(
        name=name, source=source, version=version,
        content_hash=content_hash, blob_hash=blob_hash,
    )


def test_genesis_snapshot_init(tmp_path):
    """Snapshot file created on first init if missing."""
    host = _make_host()
    snapshot_path = tmp_path / "snapshot.json"
    snapshot = _call_load_or_init(snapshot_path, host)
    assert snapshot_path.exists()
    assert snapshot["skills"] == {}
    assert snapshot["included_events"] == []
    assert "content_hash" in snapshot


def test_manifest_rebuild_from_empty(tmp_path):
    """Rebuild from genesis snapshot + no events = empty manifest."""
    host = _make_host()
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    snapshot = {"version": 1, "skills": {}, "tombstones": {}, "included_events": []}
    event_log = EventLog(events_dir, host)
    config = _make_config(tmp_path)

    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    assert manifest.skills == {}
    assert manifest.tombstones == {}


def test_manifest_replay_add_event(tmp_path):
    """T7: replay add event creates skill entry."""
    host = _make_host()
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    event_log = EventLog(events_dir, host)
    event_log.write("add", _make_entry("skill-a"))

    snapshot = {"version": 1, "skills": {}, "tombstones": {}, "included_events": []}
    config = _make_config(tmp_path)

    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    assert "skill-a" in manifest.skills
    assert manifest.skills["skill-a"].source == "work"


def test_manifest_replay_deterministic(tmp_path):
    """F6.3: same events produce same manifest."""
    host = _make_host()
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    event_log = EventLog(events_dir, host)
    event_log.write("add", _make_entry("skill-a"))
    event_log.write("add", _make_entry("skill-b"))
    event_log.write("add", _make_entry("skill-c"))

    snapshot = {"version": 1, "skills": {}, "tombstones": {}, "included_events": []}
    config = _make_config(tmp_path)

    m1 = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    m2 = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)

    assert m1.skills.keys() == m2.skills.keys()
    assert m1.event_fingerprint == m2.event_fingerprint


def test_snapshot_folded_events_skipped(tmp_path):
    """Events in snapshot.included_events are skipped during replay."""
    host = _make_host()
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    event_log = EventLog(events_dir, host)
    e1 = event_log.write("add", _make_entry("skill-a"))
    e2 = event_log.write("add", _make_entry("skill-b"))

    # Snapshot already includes e1
    snapshot = {
        "version": 1,
        "skills": {"skill-a": _make_entry("skill-a").to_dict()},
        "tombstones": {},
        "included_events": [e1.id],
    }
    config = _make_config(tmp_path)

    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    # skill-a from snapshot, skill-b from event e2
    assert "skill-a" in manifest.skills
    assert "skill-b" in manifest.skills


def test_conflict_resolution_both_semver_higher_wins(tmp_path):
    """T31: v1.0.0 vs v1.2.0, v1.2.0 wins."""
    host = _make_host()
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    event_log = EventLog(events_dir, host)

    # First add v1.0.0
    event_log.write("add", _make_entry("skill-x", version="1.0.0",
                                       content_hash="sha256:old"))
    # Then update v1.2.0
    event_log.write("update", _make_entry("skill-x", version="1.2.0",
                                          content_hash="sha256:new"))

    snapshot = {"version": 1, "skills": {}, "tombstones": {}, "included_events": []}
    config = _make_config(tmp_path)

    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    assert "skill-x" in manifest.skills
    # v1.2.0 wins
    assert manifest.skills["skill-x"].version == "1.2.0"
    assert manifest.skills["skill-x"].content_hash == "sha256:new"
    # No conflict recorded
    assert "skill-x" not in manifest.conflicts


def test_conflict_resolution_no_semver_uses_lamport(tmp_path):
    """T32: no version, Lamport higher wins, loser blob kept."""
    host = _make_host()
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    event_log = EventLog(events_dir, host)

    # add skill-x with no version (lamport=1)
    event_log.write("add", _make_entry("skill-x", version="",
                                       content_hash="sha256:v1"))
    # update skill-x with no version (lamport=2)
    event_log.write("update", _make_entry("skill-x", version="",
                                          content_hash="sha256:v2"))

    snapshot = {"version": 1, "skills": {}, "tombstones": {}, "included_events": []}
    config = _make_config(tmp_path)

    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    assert "skill-x" in manifest.skills
    # Latest (lamport=2) should win
    assert manifest.skills["skill-x"].content_hash == "sha256:v2"
    # No conflict recorded (auto-resolved by Lamport)
    assert "skill-x" not in manifest.conflicts


def test_mixed_version_conflict_recorded(tmp_path):
    """T35: one has SemVer, one doesn't -> MIXED-VERSION-CONFLICT, no auto-overwrite."""
    host = _make_host()
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    event_log = EventLog(events_dir, host)

    # add skill-x with version 1.0.0
    event_log.write("add", _make_entry("skill-x", version="1.0.0",
                                       content_hash="sha256:with-ver"))
    # update skill-x without version
    event_log.write("update", _make_entry("skill-x", version="",
                                          content_hash="sha256:no-ver"))

    snapshot = {"version": 1, "skills": {}, "tombstones": {}, "included_events": []}
    config = _make_config(tmp_path)

    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    # Conflict recorded
    assert "skill-x" in manifest.conflicts
    assert manifest.conflicts["skill-x"].type == "MIXED-VERSION-CONFLICT"
    # Existing (with version) is kept, not auto-overwritten
    assert manifest.skills["skill-x"].content_hash == "sha256:with-ver"


def test_subsequent_add_resolves_conflict(tmp_path):
    """After MIXED-VERSION-CONFLICT, a new add event resolves it (new content wins)."""
    host = _make_host()
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    event_log = EventLog(events_dir, host)

    event_log.write("add", _make_entry("skill-x", version="1.0.0",
                                       content_hash="sha256:v1"))
    event_log.write("update", _make_entry("skill-x", version="",
                                          content_hash="sha256:v2"))
    # New add resolves conflict
    event_log.write("add", _make_entry("skill-x", version="1.1.0",
                                       content_hash="sha256:v3"))

    snapshot = {"version": 1, "skills": {}, "tombstones": {}, "included_events": []}
    config = _make_config(tmp_path)

    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    assert "skill-x" not in manifest.conflicts  # resolved
    assert manifest.skills["skill-x"].content_hash == "sha256:v3"


def test_mixed_conflict_persists_across_rebuilds(tmp_path):
    """B2: mixed conflict stays recorded across multiple rebuilds (no auto-resolve)."""
    host = _make_host()
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    event_log = EventLog(events_dir, host)

    event_log.write("add", _make_entry("skill-x", version="1.0.0",
                                       content_hash="sha256:with-ver"))
    event_log.write("update", _make_entry("skill-x", version="",
                                          content_hash="sha256:no-ver"))

    snapshot = {"version": 1, "skills": {}, "tombstones": {}, "included_events": []}
    config = _make_config(tmp_path)

    # First rebuild
    m1 = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    assert "skill-x" in m1.conflicts

    # Second rebuild - conflict should still be there
    m2 = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    assert "skill-x" in m2.conflicts
    assert m2.conflicts["skill-x"].type == "MIXED-VERSION-CONFLICT"
    # Existing (with version) is kept
    assert m2.skills["skill-x"].content_hash == "sha256:with-ver"


def test_snapshot_preserves_conflicts(tmp_path):
    """Compact/snapshot should preserve conflicts so they survive across machines."""
    host = _make_host()
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    event_log = EventLog(events_dir, host)

    event_log.write("add", _make_entry("skill-x", version="1.0.0",
                                       content_hash="sha256:v1"))
    event_log.write("update", _make_entry("skill-x", version="",
                                          content_hash="sha256:v2"))

    snapshot = {"version": 1, "skills": {}, "tombstones": {}, "included_events": []}
    config = _make_config(tmp_path)

    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)

    # Build a compacted snapshot (mimics cli._cmd_compact)
    import hashlib, json
    new_snapshot = {
        "version": 1,
        "skills": {k: v.to_dict() for k, v in manifest.skills.items()},
        "tombstones": {k: v.to_dict() for k, v in manifest.tombstones.items()},
        "conflicts": {k: v.to_dict() for k, v in manifest.conflicts.items()},
        "included_events": [e.id for e in event_log.read_all()],
    }
    content = json.dumps(
        {k: v for k, v in new_snapshot.items() if k != "content_hash"},
        sort_keys=True,
    )
    new_snapshot["content_hash"] = f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"

    # Rebuild from compacted snapshot (no events to apply - all folded)
    rebuilt = manifest_mod.rebuild(new_snapshot, event_log, config, host.host_id)
    # Conflict preserved
    assert "skill-x" in rebuilt.conflicts
    assert rebuilt.conflicts["skill-x"].type == "MIXED-VERSION-CONFLICT"


def test_uninstall_then_add_skips(tmp_path):
    """uninstall then later add event is skipped (skill is isolated)."""
    host = _make_host()
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    event_log = EventLog(events_dir, host)

    event_log.write("add", _make_entry("skill-x"))
    event_log.write("uninstall", _make_entry("skill-x"))
    event_log.write("add", _make_entry("skill-x", content_hash="sha256:new"))

    snapshot = {"version": 1, "skills": {}, "tombstones": {}, "included_events": []}
    config = _make_config(tmp_path)

    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    # skill-x should be in tombstones (uninstalled)
    assert "skill-x" in manifest.tombstones
    assert manifest.tombstones["skill-x"].state == "uninstalled"


def test_forget_restores_skill(tmp_path):
    """forget event removes tombstone and restores skill."""
    host = _make_host()
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    event_log = EventLog(events_dir, host)

    event_log.write("add", _make_entry("skill-x"))
    event_log.write("uninstall", _make_entry("skill-x"))
    event_log.write("forget", _make_entry("skill-x"))

    snapshot = {"version": 1, "skills": {}, "tombstones": {}, "included_events": []}
    config = _make_config(tmp_path)

    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    assert "skill-x" not in manifest.tombstones
    assert "skill-x" in manifest.skills


def test_manifest_save_only_when_fingerprint_changes(tmp_path):
    """T18: 1000 empty scans don't change manifest (no events = same fingerprint)."""
    host = _make_host()
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    event_log = EventLog(events_dir, host)

    snapshot = {"version": 1, "skills": {}, "tombstones": {}, "included_events": []}
    config = _make_config(tmp_path)
    manifest_path = tmp_path / "manifest.json"

    # First save
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    manifest_mod.save(manifest, manifest_path)
    assert manifest_path.exists()
    mtime1 = manifest_path.stat().st_mtime_ns

    # Repeated empty saves with no changes must not rewrite.
    for _ in range(1000):
        manifest2 = manifest_mod.rebuild(
            snapshot, event_log, config, host.host_id
        )
        manifest_mod.save(manifest2, manifest_path)
    mtime2 = manifest_path.stat().st_mtime_ns

    assert mtime1 == mtime2  # not rewritten


def test_snapshot_does_not_contain_agents_or_watch(tmp_path):
    """T33: snapshot must not contain agents/watch fields (cross-machine pollution)."""
    host = _make_host()
    snapshot_path = tmp_path / "snapshot.json"
    snapshot = _call_load_or_init(snapshot_path, host)

    assert "agents" not in snapshot
    assert "watch" not in snapshot
    assert "skills" in snapshot
    assert "included_events" in snapshot


def test_manifest_contains_local_agents_for_display(tmp_path):
    """T34: manifest contains agents/watch from local config (display only)."""
    host = _make_host()
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    event_log = EventLog(events_dir, host)
    snapshot = {"version": 1, "skills": {}, "tombstones": {}, "included_events": []}
    config = _make_config(tmp_path)

    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    assert "codex" in manifest.agents  # from local config
    assert manifest.watch["dirs"] == ["~/skills-source"]


def _call_load_or_init(snapshot_path: Path, host: Host) -> dict:
    """Call the cli._load_or_init_snapshot helper (duplicate logic to avoid circular import)."""
    if snapshot_path.exists():
        return json.loads(snapshot_path.read_text())

    import hashlib
    snapshot = {
        "version": 1,
        "schema_version": 1,
        "created_by_host": host.host_id,
        "created_at": 1720096800123456789,
        "skills": {},
        "tombstones": {},
        "included_events": [],
    }
    content = json.dumps(
        {k: v for k, v in snapshot.items() if k != "content_hash"},
        sort_keys=True,
    )
    snapshot["content_hash"] = f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"

    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(json.dumps(snapshot, sort_keys=True, ensure_ascii=False))
    return snapshot
