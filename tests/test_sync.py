"""Test dual-machine convergence: events sync, replay consistent.

Covers T7, T12, T13, T9, T14.
"""
import json
import shutil
from pathlib import Path

import pytest

from skillmesh import (
    cas, discover, distribution, events, manifest as manifest_mod, pipeline,
)
from skillmesh.config import Agent, Config, Format, Hub, Source, Watch
from skillmesh.events import EventLog, SkillEntry
from skillmesh.host import Host
from conftest import set_test_home


HOST_A_ID = "aaaaaaaa-0000-0000-0000-000000000000"
HOST_B_ID = "bbbbbbbb-0000-0000-0000-000000000000"


def _make_config(hub_path):
    return Config(
        hub=Hub(path=str(hub_path)),
        agents=[Agent(name="codex", dir="~/.codex/skills",
                      accept_sources=["work"], layout="directory")],
        sources=[Source(label="work", prefix="~/skills-source")],
        formats=[Format(name="skill-md", filename="SKILL.md")],
        watch=Watch(dirs=["~/skills-source"], exclude=["skillmesh"]),
    )


def _make_machine(root: Path, host_id: str):
    """Set up a fake machine with hub and config."""
    home = root / "home"
    home.mkdir(parents=True)
    hub = home / "hub"
    hub.mkdir()
    (hub / "skills").mkdir()
    (hub / "blobs").mkdir()
    (hub / "events").mkdir()
    (hub / ".uninstalled").mkdir()
    src_dir = home / "skills-source"
    src_dir.mkdir()

    host = Host(host_id=host_id, display_name=host_id[:8])
    return home, hub, host, src_dir


def _init_snapshot(hub: Path, host: Host):
    """Init genesis snapshot if missing."""
    snapshot_path = hub / "snapshot.json"
    if snapshot_path.exists():
        return
    snapshot = {
        "version": 1, "schema_version": 1,
        "created_by_host": host.host_id,
        "created_at": 1720096800123456789,
        "skills": {}, "tombstones": {}, "included_events": [],
    }
    import hashlib
    content = json.dumps({k: v for k, v in snapshot.items() if k != "content_hash"}, sort_keys=True)
    snapshot["content_hash"] = f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"
    snapshot_path.write_text(json.dumps(snapshot, sort_keys=True))


def _scan(home: Path, hub: Path, host: Host, config: Config):
    """Run a scan operation on this machine."""
    _init_snapshot(hub, host)
    event_log = EventLog(hub / "events", host)
    snapshot = json.loads((hub / "snapshot.json").read_text())
    manifest = manifest_mod.load_or_rebuild(hub / "manifest.json", snapshot, event_log, config, host.host_id)

    disc = discover.discover(config)
    plan = pipeline.plan(disc, manifest, config)
    plan = pipeline.validate(plan, config, hub)
    pipeline.execute(plan, config, host, hub, event_log, dry_run=False)

    # Rebuild and save manifest
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    manifest_mod.save(manifest, hub / "manifest.json")

    # Reconcile skills/ working view with manifest's logical state.
    # This handles: ADD materialization, sync from other machines, link refresh.
    pipeline.reconcile_skills(manifest, config, hub)


def test_dual_machine_convergence(tmp_path, monkeypatch):
    """T7/T12: A scans skill → B syncs hub → B scans → B has skill."""
    home_a, hub_a, host_a, src_a = _make_machine(tmp_path / "a", HOST_A_ID)
    home_b, hub_b, host_b, src_b = _make_machine(tmp_path / "b", HOST_B_ID)

    # A creates a skill and scans
    set_test_home(monkeypatch, home_a)
    config_a = _make_config(hub_a)
    skill_dir = src_a / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# from A")
    _scan(home_a, hub_a, host_a, config_a)

    # Verify A has skill in hub
    assert (hub_a / "skills" / "my-skill").exists()
    events_a = EventLog(hub_a / "events", host_a).read_all()
    assert len(events_a) == 1
    assert events_a[0].op == "add"

    # Sync hub from A to B (simulating cloud drive)
    shutil.rmtree(hub_b)
    shutil.copytree(hub_a, hub_b)

    # B scans (should pick up the skill via event replay)
    set_test_home(monkeypatch, home_b)
    config_b = _make_config(hub_b)
    _scan(home_b, hub_b, host_b, config_b)

    # B should have the skill materialized and symlinked
    assert (hub_b / "skills" / "my-skill").exists()
    assert (hub_b / "skills" / "my-skill" / "SKILL.md").read_text() == "# from A"

    # B's agent dir should have the symlink
    link = home_b / ".codex" / "skills" / "my-skill"
    assert distribution.inspect(link, hub_b / "skills" / "my-skill").correct


def test_dual_machine_simultaneous_add_different_skills(tmp_path, monkeypatch):
    """T9: both A and B add different skills simultaneously, sync, converge."""
    home_a, hub_a, host_a, src_a = _make_machine(tmp_path / "a", HOST_A_ID)
    home_b, hub_b, host_b, src_b = _make_machine(tmp_path / "b", HOST_B_ID)

    # A adds skill-a
    set_test_home(monkeypatch, home_a)
    config_a = _make_config(hub_a)
    (src_a / "skill-a").mkdir()
    (src_a / "skill-a" / "SKILL.md").write_text("# A")
    _scan(home_a, hub_a, host_a, config_a)

    # B adds skill-b (independently, before sync)
    set_test_home(monkeypatch, home_b)
    config_b = _make_config(hub_b)
    (src_b / "skill-b").mkdir()
    (src_b / "skill-b" / "SKILL.md").write_text("# B")
    _scan(home_b, hub_b, host_b, config_b)

    # Sync: merge both event dirs into both hubs
    events_a_dir = hub_a / "events" / host_a.event_dir
    events_b_dir = hub_b / "events" / host_b.event_dir

    # Copy A's events to B's hub
    target_b = hub_b / "events" / host_a.event_dir
    if target_b.exists():
        shutil.rmtree(target_b)
    shutil.copytree(events_a_dir, target_b)
    # Copy A's blobs to B
    for blob in (hub_a / "blobs").iterdir():
        target = hub_b / "blobs" / blob.name
        if not target.exists():
            shutil.copytree(blob, target)

    # Copy B's events to A's hub
    target_a = hub_a / "events" / host_b.event_dir
    if target_a.exists():
        shutil.rmtree(target_a)
    shutil.copytree(events_b_dir, target_a)
    # Copy B's blobs to A
    for blob in (hub_b / "blobs").iterdir():
        target = hub_a / "blobs" / blob.name
        if not target.exists():
            shutil.copytree(blob, target)

    # Both machines scan again
    set_test_home(monkeypatch, home_a)
    _scan(home_a, hub_a, host_a, config_a)
    set_test_home(monkeypatch, home_b)
    _scan(home_b, hub_b, host_b, config_b)

    # Both should have both skills
    assert (hub_a / "skills" / "skill-a").exists()
    assert (hub_a / "skills" / "skill-b").exists()
    assert (hub_b / "skills" / "skill-a").exists()
    assert (hub_b / "skills" / "skill-b").exists()


def test_corrupt_event_aborts_sync_without_mutation(tmp_path, monkeypatch):
    """T14: corrupt synced truth source aborts replay and remains intact."""
    home_a, hub_a, host_a, src_a = _make_machine(tmp_path / "a", HOST_A_ID)

    set_test_home(monkeypatch, home_a)
    config_a = _make_config(hub_a)

    # Write a valid event first
    (src_a / "good").mkdir()
    (src_a / "good" / "SKILL.md").write_text("# good")
    _scan(home_a, hub_a, host_a, config_a)

    # Inject a corrupt event file in same host subdir
    host_subdir = hub_a / "events" / host_a.event_dir
    corrupt = host_subdir / "999-999-deadbeefdeadbeef.json"
    corrupt.write_text("{ invalid json")

    with pytest.raises(events.EventCorruptError, match="Replay aborted"):
        _scan(home_a, hub_a, host_a, config_a)
    assert corrupt.exists()


def test_manifest_converges_after_sync(tmp_path, monkeypatch):
    """T12: after sync, both machines have same manifest skills (modulo agents/watch display)."""
    home_a, hub_a, host_a, src_a = _make_machine(tmp_path / "a", HOST_A_ID)
    home_b, hub_b, host_b, src_b = _make_machine(tmp_path / "b", HOST_B_ID)

    # A adds skill
    set_test_home(monkeypatch, home_a)
    config_a = _make_config(hub_a)
    (src_a / "shared").mkdir()
    (src_a / "shared" / "SKILL.md").write_text("# shared")
    _scan(home_a, hub_a, host_a, config_a)

    # Sync to B
    shutil.rmtree(hub_b)
    shutil.copytree(hub_a, hub_b)

    # B scans
    set_test_home(monkeypatch, home_b)
    config_b = _make_config(hub_b)
    _scan(home_b, hub_b, host_b, config_b)

    # Compare manifest skills (the truth source)
    event_log_a = EventLog(hub_a / "events", host_a)
    event_log_b = EventLog(hub_b / "events", host_b)
    snap_a = json.loads((hub_a / "snapshot.json").read_text())
    snap_b = json.loads((hub_b / "snapshot.json").read_text())

    manifest_a = manifest_mod.rebuild(snap_a, event_log_a, config_a, host_a.host_id)
    manifest_b = manifest_mod.rebuild(snap_b, event_log_b, config_b, host_b.host_id)

    # Skills should match
    assert set(manifest_a.skills.keys()) == set(manifest_b.skills.keys())
    assert "shared" in manifest_a.skills

    # event_fingerprint should match (same set of events)
    assert manifest_a.event_fingerprint == manifest_b.event_fingerprint
