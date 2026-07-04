"""Test lifecycle: detach, attach, uninstall, forget, purge safety.

Covers F5.1-F5.8, T15 (path attack fail-closed).
"""
import json
import os
import shutil
from pathlib import Path

import pytest

from skillmesh import events, lifecycle, manifest as manifest_mod
from skillmesh.events import EventLog, SkillEntry
from skillmesh.host import Host
from skillmesh.config import Config, Hub, Agent, Watch


def _setup_env(tmp_path):
    host = Host(host_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", display_name="test")
    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "skills").mkdir()
    (hub / "events").mkdir()
    (hub / "blobs").mkdir()
    (hub / ".uninstalled").mkdir()

    config = Config(
        hub=Hub(path=str(hub)),
        agents=[Agent(name="codex", dir="~/.codex/skills",
                      accept_sources=["work"], layout="directory")],
        watch=Watch(dirs=["~/.codex/skills"]),
    )

    # Add a skill to manifest
    entry = SkillEntry(name="my-skill", source="work", version="1.0.0",
                       content_hash="sha256:x", blob_hash="sha256:y")
    skills_dir = hub / "skills"
    (skills_dir / "my-skill").mkdir()
    (skills_dir / "my-skill" / "SKILL.md").write_text("# my skill")
    # Mark as skillmesh-owned so purge/lifecycle can safely remove
    (skills_dir / "my-skill" / ".skillmesh-owned").write_text("skillmesh\n")

    return host, hub, config, entry, skills_dir


def _make_manifest_with_skill(entry, host):
    """Build a manifest containing one skill."""
    return manifest_mod.Manifest(
        revision=1,
        generated_by_host=host.host_id,
        skills={entry.name: entry},
    )


def test_detach_removes_links_keeps_entry(tmp_path):
    """F5.1: detach sets target_override=[] and removes symlinks."""
    host, hub, config, entry, skills_dir = _setup_env(tmp_path)
    event_log = EventLog(hub / "events", host)
    manifest = _make_manifest_with_skill(entry, host)

    # Create a symlink to test removal
    agent_dir = Path("~/.codex/skills").expanduser()
    agent_dir.mkdir(parents=True, exist_ok=True)
    link = agent_dir / "my-skill"
    if link.exists() or link.is_symlink():
        link.unlink()
    os.symlink(skills_dir / "my-skill", link)

    lifecycle.detach("my-skill", manifest, event_log, config, skills_dir)

    assert entry.target_override == []
    assert not link.is_symlink()  # link removed
    # Skill content still in hub
    assert (skills_dir / "my-skill" / "SKILL.md").exists()


def test_attach_restores_links(tmp_path):
    """F5.2: attach restores target_override and re-links."""
    host, hub, config, entry, skills_dir = _setup_env(tmp_path)
    event_log = EventLog(hub / "events", host)
    manifest = _make_manifest_with_skill(entry, host)

    entry.target_override = []  # detached
    lifecycle.attach("my-skill", manifest, event_log, config, skills_dir)

    assert entry.target_override is None
    # Link recreated
    link = Path("~/.codex/skills/my-skill").expanduser()
    assert link.is_symlink()


def test_uninstall_moves_to_uninstalled_dir(tmp_path):
    """F5.3: uninstall moves skill to .uninstalled/."""
    host, hub, config, entry, skills_dir = _setup_env(tmp_path)
    event_log = EventLog(hub / "events", host)
    manifest = _make_manifest_with_skill(entry, host)

    lifecycle.uninstall("my-skill", manifest, event_log, config, hub)

    # Skill moved out of skills/
    assert not (skills_dir / "my-skill").exists()
    # Skill now in .uninstalled/
    assert (hub / ".uninstalled" / "my-skill").exists()
    assert (hub / ".uninstalled" / "my-skill" / "SKILL.md").exists()
    assert entry.in_hub is False


def test_forget_restores_from_uninstalled(tmp_path):
    """F5.4: forget moves skill back from .uninstalled/ to skills/."""
    host, hub, config, entry, skills_dir = _setup_env(tmp_path)
    event_log = EventLog(hub / "events", host)
    manifest = _make_manifest_with_skill(entry, host)

    lifecycle.uninstall("my-skill", manifest, event_log, config, hub)
    lifecycle.forget("my-skill", manifest, event_log, config, hub, skills_dir)

    # Back in skills/
    assert (skills_dir / "my-skill").exists()
    assert not (hub / ".uninstalled" / "my-skill").exists()
    assert entry.in_hub is True


def test_purge_requires_yes(tmp_path):
    """F5.5: purge without --yes raises ConfirmationRequired."""
    host, hub, config, entry, skills_dir = _setup_env(tmp_path)
    event_log = EventLog(hub / "events", host)
    manifest = _make_manifest_with_skill(entry, host)

    with pytest.raises(lifecycle.ConfirmationRequired):
        lifecycle.purge("my-skill", manifest, event_log, config, hub, yes=False)


def test_purge_with_yes_deletes(tmp_path):
    """F5.5: purge with --yes removes skill and event written."""
    host, hub, config, entry, skills_dir = _setup_env(tmp_path)
    event_log = EventLog(hub / "events", host)
    manifest = _make_manifest_with_skill(entry, host)

    lifecycle.purge("my-skill", manifest, event_log, config, hub, yes=True)

    assert not (skills_dir / "my-skill").exists()
    # Event was written
    events_list = event_log.read_all()
    ops = [e.op for e in events_list]
    assert "purge" in ops


def test_invalid_skill_name_rejected(tmp_path):
    """T15/F5.7: invalid skill name (path traversal) rejected."""
    host, hub, config, entry, skills_dir = _setup_env(tmp_path)
    event_log = EventLog(hub / "events", host)
    manifest = _make_manifest_with_skill(entry, host)

    bad_names = [
        "../../../etc/passwd",
        "..\\..\\windows",
        "skill/with/slash",
        "skill with space",
        "skill;rm -rf /",
    ]
    for bad in bad_names:
        with pytest.raises(lifecycle.LifecycleError):
            lifecycle.uninstall(bad, manifest, event_log, config, hub)


def test_uninstall_rejects_symlink_in_target(tmp_path):
    """T15/F5.7: .uninstalled/<name> must not be a symlink (path escape)."""
    host, hub, config, entry, skills_dir = _setup_env(tmp_path)
    event_log = EventLog(hub / "events", host)
    manifest = _make_manifest_with_skill(entry, host)

    # Create a symlink in .uninstalled/ pointing outside
    evil_target = tmp_path / "evil"
    evil_target.mkdir()
    link = hub / ".uninstalled" / "my-skill"
    if link.exists() or link.is_symlink():
        link.unlink()
    os.symlink(evil_target, link)

    # Try to uninstall - should refuse (symlink in .uninstalled/)
    # Note: uninstall would rename source to target. Since target is symlink,
    # _safe_uninstall_path should reject.
    with pytest.raises(lifecycle.PathEscape):
        lifecycle.uninstall("my-skill", manifest, event_log, config, hub)


def test_dry_run_does_nothing(tmp_path):
    """F5.8: --dry-run makes no changes."""
    host, hub, config, entry, skills_dir = _setup_env(tmp_path)
    event_log = EventLog(hub / "events", host)
    manifest = _make_manifest_with_skill(entry, host)

    initial_events = len(event_log.read_all())
    lifecycle.uninstall("my-skill", manifest, event_log, config, hub, dry_run=True)

    # Skill still in skills/, no new events
    assert (skills_dir / "my-skill").exists()
    assert len(event_log.read_all()) == initial_events


def test_purge_nonexistent_skill_errors(tmp_path):
    """purge on non-existent skill raises (unless already in tombstones)."""
    host, hub, config, entry, skills_dir = _setup_env(tmp_path)
    event_log = EventLog(hub / "events", host)
    manifest = _make_manifest_with_skill(entry, host)

    with pytest.raises(lifecycle.LifecycleError):
        lifecycle.purge("nonexistent", manifest, event_log, config, hub, yes=True)


def test_detach_nonexistent_skill_errors(tmp_path):
    host, hub, config, entry, skills_dir = _setup_env(tmp_path)
    event_log = EventLog(hub / "events", host)
    manifest = _make_manifest_with_skill(entry, host)

    with pytest.raises(lifecycle.LifecycleError):
        lifecycle.detach("nonexistent", manifest, event_log, config, skills_dir)


def test_forget_not_in_uninstalled_errors(tmp_path):
    """forget on skill not in .uninstalled/ raises."""
    host, hub, config, entry, skills_dir = _setup_env(tmp_path)
    event_log = EventLog(hub / "events", host)
    manifest = _make_manifest_with_skill(entry, host)

    with pytest.raises(lifecycle.LifecycleError):
        lifecycle.forget("nonexistent", manifest, event_log, config, hub, skills_dir)


def test_gc_keeps_uninstalled_skill_blob(tmp_path):
    """B1: gc does NOT delete blob of uninstalled skill (recoverable via forget)."""
    host, hub, config, entry, skills_dir = _setup_env(tmp_path)
    event_log = EventLog(hub / "events", host)

    # Create the blob
    from skillmesh import cas
    blobs_dir = hub / "blobs"
    content_hash = cas.write_blob(skills_dir / "my-skill", "my-skill",
                                  "skill-md", "1.0.0", blobs_dir, host.host_id)
    entry.content_hash = content_hash

    # Write add event first so manifest.skills has the entry after rebuild
    event_log.write("add", entry)
    # Rebuild manifest with the add event
    snapshot = {"version": 1, "skills": {}, "tombstones": {}, "included_events": []}
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    # Uninstall (move to .uninstalled/)
    lifecycle.uninstall("my-skill", manifest, event_log, config, hub)
    # Rebuild manifest to reflect uninstall event
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)

    # Run gc - should NOT delete the blob (uninstalled is recoverable)
    removed = lifecycle.gc(manifest, event_log, hub, blobs_dir, dry_run=False)
    assert removed == 0  # nothing removed
    # Blob still exists
    assert (blobs_dir / content_hash).exists()


def test_gc_deletes_purged_blob_only_when_all_hosts_confirmed(tmp_path):
    """M4: gc only deletes purged skill's blob when ALL hosts confirmed purge."""
    host, hub, config, entry, skills_dir = _setup_env(tmp_path)
    event_log = EventLog(hub / "events", host)

    from skillmesh import cas
    blobs_dir = hub / "blobs"
    content_hash = cas.write_blob(skills_dir / "my-skill", "my-skill",
                                  "skill-md", "1.0.0", blobs_dir, host.host_id)
    entry.content_hash = content_hash

    # Write add event first
    event_log.write("add", entry)
    snapshot = {"version": 1, "skills": {}, "tombstones": {}, "included_events": []}
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    # Purge
    lifecycle.purge("my-skill", manifest, event_log, config, hub, yes=True)

    # Rebuild manifest
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)

    # gc with only ONE host (this one) - it wrote purge, so all known hosts confirmed
    removed = lifecycle.gc(manifest, event_log, hub, blobs_dir, dry_run=False)
    # Single host, all confirmed - blob should be deleted
    assert removed >= 1
    assert not (blobs_dir / content_hash).exists()


def test_gc_does_not_delete_when_other_host_missing_purge(tmp_path):
    """M4: gc refuses to delete purged blob when another host hasn't purged yet."""
    host, hub, config, entry, skills_dir = _setup_env(tmp_path)
    event_log = EventLog(hub / "events", host)

    from skillmesh import cas
    blobs_dir = hub / "blobs"
    content_hash = cas.write_blob(skills_dir / "my-skill", "my-skill",
                                  "skill-md", "1.0.0", blobs_dir, host.host_id)
    entry.content_hash = content_hash

    # Write add event first
    event_log.write("add", entry)
    snapshot = {"version": 1, "skills": {}, "tombstones": {}, "included_events": []}
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    # Purge on this host
    lifecycle.purge("my-skill", manifest, event_log, config, hub, yes=True)

    # Simulate another host's event_dir exists (with non-purge event)
    other_host_dir = hub / "events" / "other-host-deadbeef"
    other_host_dir.mkdir(parents=True)
    # Write a non-purge event (e.g., add) from other host
    import json, time
    other_event = {
        "id": "other-uuid",
        "host": "deadbeef-0000-0000-0000-000000000000",
        "host_display_name": "other-host",
        "ts": time.time_ns(),
        "seq": 1,
        "lamport": 1,
        "op": "add",
        "skill": entry.to_dict(),
        "prev_lamport": 0,
        "schema_version": 1,
    }
    (other_host_dir / "1-1-deadbeefdeadbeef.json").write_text(
        json.dumps(other_event, sort_keys=True)
    )

    # Rebuild manifest
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)

    # gc - other host hasn't written purge, should refuse to delete blob
    removed = lifecycle.gc(manifest, event_log, hub, blobs_dir, dry_run=False)
    # Blob preserved (other host hasn't confirmed purge)
    assert (blobs_dir / content_hash).exists()
