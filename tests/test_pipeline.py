"""Test pipeline: discover, plan, validate, execute with directory/file layouts.

Covers F4.1-F4.7, T29a, T29b, T29c, T29d.
"""
import os
from pathlib import Path

import pytest

from skillmesh import cas, discover, distribution, pipeline
from skillmesh import manifest as manifest_mod
from skillmesh.config import Agent, Config, Format, Hub, Source, Watch
from skillmesh.events import EventLog, SkillEntry
from skillmesh.host import Host
from skillmesh.manifest import Manifest
from conftest import set_test_home, symlink_or_skip


def _make_config(hub_path, agents=None):
    if agents is None:
        agents = [
            Agent(name="codex", dir="~/.codex/skills",
                  accept_sources=["work"], layout="directory"),
            Agent(name="cursor", dir="~/.cursor/rules",
                  accept_sources=["work"], layout="file",
                  target_filename="{skill}.mdc"),
        ]
    return Config(
        hub=Hub(path=str(hub_path)),
        agents=agents,
        sources=[Source(label="work", prefix="~/skills-source")],
        formats=[Format(name="skill-md", filename="SKILL.md")],
        watch=Watch(dirs=["~/skills-source"], exclude=["skillmesh"]),
    )


def _make_skill(src_dir, name, content="# test"):
    skill_dir = src_dir / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content)
    return skill_dir


def test_discover_finds_skill(tmp_path, monkeypatch):
    """F2.1: discover finds skill in watch.dirs."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    set_test_home(monkeypatch, fake_home)
    src_dir = fake_home / "skills-source"
    src_dir.mkdir()
    _make_skill(src_dir, "my-skill", "# hello")

    config = _make_config(tmp_path / "hub")
    result = discover.discover(config)

    assert len(result.candidates) == 1
    assert result.candidates[0].name == "my-skill"
    assert result.candidates[0].source == "work"
    assert result.candidates[0].format == "skill-md"


def test_discover_skips_invalid_names(tmp_path, monkeypatch):
    """Invalid skill names (slash, space) skipped with warning."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    set_test_home(monkeypatch, fake_home)
    src_dir = fake_home / "skills-source"
    src_dir.mkdir()

    # Valid name
    _make_skill(src_dir, "valid-skill")
    # Invalid name (space) - discover should skip
    bad = src_dir / "bad name"
    bad.mkdir()
    (bad / "SKILL.md").write_text("# bad")

    config = _make_config(tmp_path / "hub")
    result = discover.discover(config)
    names = [c.name for c in result.candidates]
    assert "valid-skill" in names
    assert "bad name" not in names


def test_discover_excludes_patterns(tmp_path, monkeypatch):
    """F2.4: exclude patterns applied."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    set_test_home(monkeypatch, fake_home)
    src_dir = fake_home / "skills-source"
    src_dir.mkdir()
    _make_skill(src_dir, "keep")
    excluded = src_dir / "skillmesh"
    excluded.mkdir()
    _make_skill(excluded, "should-skip")

    config = _make_config(tmp_path / "hub")
    result = discover.discover(config)
    names = [c.name for c in result.candidates]
    assert "keep" in names
    assert "should-skip" not in names  # under skillmesh/, excluded


def test_discover_dedup_by_name(tmp_path, monkeypatch):
    """Duplicate skill names skipped (keep first)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    set_test_home(monkeypatch, fake_home)
    src_dir = fake_home / "skills-source"
    src_dir.mkdir()
    other = fake_home / "other-source"
    other.mkdir()

    _make_skill(src_dir, "dup")
    _make_skill(other, "dup")

    config = Config(
        hub=Hub(path=str(tmp_path / "hub")),
        agents=[Agent(name="codex", dir="~/.codex/skills",
                      accept_sources=["work"], layout="directory")],
        sources=[Source(label="work", prefix="~/skills-source"),
                 Source(label="other", prefix="~/other-source")],
        formats=[Format(name="skill-md", filename="SKILL.md")],
        watch=Watch(dirs=["~/skills-source", "~/other-source"], exclude=[]),
    )
    result = discover.discover(config)
    assert len(result.candidates) == 1  # deduped


def test_directory_layout_creates_symlink(tmp_path, monkeypatch):
    """T29a: directory layout symlinks whole skill dir to <agent>/<skill>/."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    set_test_home(monkeypatch, fake_home)
    src_dir = fake_home / "skills-source"
    src_dir.mkdir()
    _make_skill(src_dir, "my-skill", "# content")

    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "skills").mkdir()
    (hub / "blobs").mkdir()
    (hub / "events").mkdir()

    config = _make_config(hub)
    host = Host(host_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", display_name="test")
    event_log = EventLog(hub / "events", host)

    # Run discover → plan → execute
    disc = discover.discover(config)
    manifest = Manifest()
    plan = pipeline.plan(disc, manifest, config)
    plan = pipeline.validate(plan, config, hub)
    pipeline.execute(plan, config, host, hub, event_log, dry_run=False)
    # Reconcile to materialize + link (execute only writes blob + event)
    snapshot = {"version": 1, "skills": {}, "tombstones": {}, "included_events": []}
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    pipeline.reconcile_skills(manifest, config, hub)

    # Check symlink
    link = fake_home / ".codex" / "skills" / "my-skill"
    assert distribution.inspect(link, hub / "skills" / "my-skill").correct
    assert (link / "SKILL.md").exists()


def test_file_layout_creates_file_symlink(tmp_path, monkeypatch):
    """T29b: file layout symlinks entry file to <agent>/<target_filename>."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    set_test_home(monkeypatch, fake_home)
    src_dir = fake_home / "skills-source"
    src_dir.mkdir()
    _make_skill(src_dir, "my-skill", "# content")

    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "skills").mkdir()
    (hub / "blobs").mkdir()
    (hub / "events").mkdir()

    config = _make_config(hub)
    host = Host(host_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", display_name="test")
    event_log = EventLog(hub / "events", host)

    disc = discover.discover(config)
    manifest = Manifest()
    plan = pipeline.plan(disc, manifest, config)
    plan = pipeline.validate(plan, config, hub)
    pipeline.execute(plan, config, host, hub, event_log, dry_run=False)
    # Reconcile to materialize + link
    snapshot = {"version": 1, "skills": {}, "tombstones": {}, "included_events": []}
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    pipeline.reconcile_skills(manifest, config, hub)

    # Check cursor file symlink (target_filename = {skill}.mdc -> my-skill.mdc)
    link = fake_home / ".cursor" / "rules" / "my-skill.mdc"
    assert distribution.inspect(
        link, hub / "skills" / "my-skill" / "SKILL.md"
    ).correct


def test_dry_run_makes_no_changes(tmp_path, monkeypatch):
    """T10/F5.8: --dry-run writes nothing."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    set_test_home(monkeypatch, fake_home)
    src_dir = fake_home / "skills-source"
    src_dir.mkdir()
    _make_skill(src_dir, "my-skill")

    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "skills").mkdir()
    (hub / "blobs").mkdir()
    (hub / "events").mkdir()

    config = _make_config(hub)
    host = Host(host_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", display_name="test")
    event_log = EventLog(hub / "events", host)

    disc = discover.discover(config)
    manifest = Manifest()
    plan = pipeline.plan(disc, manifest, config)
    plan = pipeline.validate(plan, config, hub)
    result = pipeline.execute(plan, config, host, hub, event_log, dry_run=True)

    assert len(result.succeeded) == 0
    assert len(result.skipped) > 0
    # No blobs written
    assert not any((hub / "blobs").iterdir())
    # No events written
    assert not any((hub / "events").iterdir())
    # No symlinks
    link = fake_home / ".codex" / "skills" / "my-skill"
    assert not link.exists()


def test_target_derivation_from_accept_sources(tmp_path):
    """F4.1: targets derived from accept_sources match."""
    entry = SkillEntry(name="x", source="work")
    agents = [
        Agent(name="codex", dir="~/.codex/skills", accept_sources=["work"], layout="directory"),
        Agent(name="personal-only", dir="~/.personal/skills", accept_sources=["personal"], layout="directory"),
    ]
    targets = pipeline._compute_targets(entry, agents)
    assert [a.name for a in targets] == ["codex"]


def test_target_override_overrides_accept_sources(tmp_path):
    """F4.1: target_override takes precedence over accept_sources."""
    entry = SkillEntry(name="x", source="work", target_override=["personal-only"])
    agents = [
        Agent(name="codex", dir="~/.codex/skills", accept_sources=["work"], layout="directory"),
        Agent(name="personal-only", dir="~/.personal/skills", accept_sources=["personal"], layout="directory"),
    ]
    targets = pipeline._compute_targets(entry, agents)
    assert [a.name for a in targets] == ["personal-only"]


def test_content_differs_recomputes_hash(tmp_path, monkeypatch):
    """B1: _content_differs recomputes content_hash, not just version compare.

    Same version, different content -> differs=True.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    set_test_home(monkeypatch, fake_home)
    src_dir = fake_home / "skills-source"
    src_dir.mkdir()

    # Create skill with content A
    skill_dir = src_dir / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# content A")

    from skillmesh import cas
    config = Config(
        hub=Hub(path=str(tmp_path / "hub")),
        agents=[Agent(name="codex", dir="~/.codex/skills",
                      accept_sources=["work"], layout="directory")],
        sources=[Source(label="work", prefix="~/skills-source")],
        formats=[Format(name="skill-md", filename="SKILL.md")],
        watch=Watch(dirs=["~/skills-source"], exclude=[]),
    )

    # Discover skill A
    disc = discover.discover(config)
    assert len(disc.candidates) == 1
    candidate_a = disc.candidates[0]

    # Compute hash of A
    hash_a = cas.compute_content_hash(candidate_a.path, candidate_a.name,
                                      candidate_a.format, candidate_a.version)

    # Existing entry has same version but different content_hash
    existing = SkillEntry(
        name="my-skill", source="work", version="",
        content_hash="sha256:different-content-hash",
    )

    # Should detect difference (recompute hash)
    assert pipeline._content_differs(candidate_a, existing) is True

    # Now existing has correct hash - should not differ
    existing.content_hash = hash_a
    assert pipeline._content_differs(candidate_a, existing) is False


def test_update_writes_new_blob_when_content_changes(tmp_path, monkeypatch):
    """B1: scan with changed skill content writes update event + new blob."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    set_test_home(monkeypatch, fake_home)
    src_dir = fake_home / "skills-source"
    src_dir.mkdir()

    skill_dir = src_dir / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# v1")

    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "skills").mkdir()
    (hub / "blobs").mkdir()
    (hub / "events").mkdir()

    config = Config(
        hub=Hub(path=str(hub)),
        agents=[Agent(name="codex", dir="~/.codex/skills",
                      accept_sources=["work"], layout="directory")],
        sources=[Source(label="work", prefix="~/skills-source")],
        formats=[Format(name="skill-md", filename="SKILL.md")],
        watch=Watch(dirs=["~/skills-source"], exclude=[]),
    )
    host = Host(host_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", display_name="test")
    event_log = EventLog(hub / "events", host)

    # First scan: add
    disc = discover.discover(config)
    manifest = Manifest()
    plan = pipeline.plan(disc, manifest, config)
    plan = pipeline.validate(plan, config, hub)
    pipeline.execute(plan, config, host, hub, event_log, dry_run=False)
    # Reconcile to materialize
    snapshot = {"version": 1, "skills": {}, "tombstones": {}, "included_events": []}
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    pipeline.reconcile_skills(manifest, config, hub)

    initial_blobs = list((hub / "blobs").iterdir())
    initial_events = event_log.read_all()
    assert len(initial_events) == 1
    assert initial_events[0].op == "add"

    # Change content
    (skill_dir / "SKILL.md").write_text("# v2 - changed")

    # Rebuild manifest with current state
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)

    # Second scan: should detect content change and write update
    disc = discover.discover(config)
    plan = pipeline.plan(disc, manifest, config)
    plan = pipeline.validate(plan, config, hub)
    pipeline.execute(plan, config, host, hub, event_log, dry_run=False)
    # Reconcile to materialize the update
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    pipeline.reconcile_skills(manifest, config, hub)

    events = event_log.read_all()
    ops = [e.op for e in events]
    assert "update" in ops  # update event written

    # New blob created (different content)
    final_blobs = list((hub / "blobs").iterdir())
    assert len(final_blobs) > len(initial_blobs)


def test_link_refuses_to_delete_user_content(tmp_path, monkeypatch):
    """B5: _safe_remove_link refuses to delete non-symlink agent target (user content)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    set_test_home(monkeypatch, fake_home)

    # Pre-existing user file at agent target
    agent_dir = fake_home / ".codex" / "skills"
    agent_dir.mkdir(parents=True)
    user_file = agent_dir / "my-skill"
    user_file.mkdir()
    (user_file / "user-data.txt").write_text("important user data")

    # Try to remove - should refuse
    with pytest.raises(RuntimeError, match="refusing to delete"):
        pipeline._safe_remove_link(user_file)

    # User content preserved
    assert (user_file / "user-data.txt").exists()


def test_link_removes_symlink_only(tmp_path):
    """B5: _safe_remove_link removes symlinks but not real files."""
    import os
    link = tmp_path / "link"
    target = tmp_path / "target"
    target.mkdir()
    symlink_or_skip(target, link, target_is_directory=True)

    pipeline._safe_remove_link(link)
    assert not link.exists()
    assert target.exists()  # target preserved


def test_reconcile_keeps_old_on_mixed_conflict(tmp_path, monkeypatch):
    """B2: on MIXED-VERSION-CONFLICT, reconcile keeps manifest's winner (old),
    not the newly-written blob.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    set_test_home(monkeypatch, fake_home)
    src_dir = fake_home / "skills-source"
    src_dir.mkdir()

    # Initial skill with version 1.0.0 (frontmatter)
    skill_dir = src_dir / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nversion: 1.0.0\n---\n# v1.0.0\n")

    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "skills").mkdir()
    (hub / "blobs").mkdir()
    (hub / "events").mkdir()

    config = Config(
        hub=Hub(path=str(hub)),
        agents=[Agent(name="codex", dir="~/.codex/skills",
                      accept_sources=["work"], layout="directory")],
        sources=[Source(label="work", prefix="~/skills-source")],
        formats=[Format(name="skill-md", filename="SKILL.md")],
        watch=Watch(dirs=["~/skills-source"], exclude=[]),
    )
    host = Host(host_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", display_name="test")
    event_log = EventLog(hub / "events", host)

    # First add (with version 1.0.0)
    disc = discover.discover(config)
    manifest = Manifest()
    plan = pipeline.plan(disc, manifest, config)
    pipeline.execute(plan, config, host, hub, event_log, dry_run=False)
    snapshot = {"version": 1, "skills": {}, "tombstones": {}, "included_events": []}
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    pipeline.reconcile_skills(manifest, config, hub)

    # Capture original content_hash (the v1.0.0 one)
    original_hash = manifest.skills["my-skill"].content_hash
    assert original_hash

    # Now modify skill to remove version (simulate mixed conflict)
    (skill_dir / "SKILL.md").write_text("# no version content")

    # Rebuild manifest with current state (still has v1.0.0 from before)
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)

    # Re-scan: writes update event (new blob without version)
    disc = discover.discover(config)
    plan = pipeline.plan(disc, manifest, config)
    pipeline.execute(plan, config, host, hub, event_log, dry_run=False)

    # Rebuild manifest - should record MIXED-VERSION-CONFLICT, keep old
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    assert "my-skill" in manifest.conflicts
    assert manifest.conflicts["my-skill"].type == "MIXED-VERSION-CONFLICT"
    # Manifest keeps the OLD content_hash (with version)
    assert manifest.skills["my-skill"].content_hash == original_hash

    # Reconcile: should materialize the OLD content (manifest's winner),
    # NOT the newly-written blob
    pipeline.reconcile_skills(manifest, config, hub)

    # Verify skills/my-skill/SKILL.md has OLD content
    materialized = hub / "skills" / "my-skill" / "SKILL.md"
    assert materialized.exists()
    assert "v1.0.0" in materialized.read_text()
    assert "no version content" not in materialized.read_text()
