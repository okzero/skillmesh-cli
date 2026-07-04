"""Test status and invariants.

Covers F9.1-F9.5, T9, T15.
"""
import json
import os
from pathlib import Path

import pytest

from skillmesh import status as status_mod
from skillmesh.config import Agent, Config, Hub, Watch
from skillmesh.events import SkillEntry
from skillmesh.host import Host
from skillmesh.manifest import Manifest, Tombstone


def _make_config(hub_path):
    return Config(
        hub=Hub(path=str(hub_path)),
        agents=[
            Agent(name="codex", dir="~/.codex/skills",
                  accept_sources=["work"], layout="directory"),
        ],
        watch=Watch(dirs=["~/skills-source"]),
    )


def _make_entry(name="my-skill", source="work"):
    return SkillEntry(name=name, source=source, version="1.0.0",
                      content_hash="sha256:abc", blob_hash="sha256:def")


def test_status_lists_skills(tmp_path, monkeypatch):
    """F9.1: status lists skills with state."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    hub = tmp_path / "hub"
    (hub / "skills").mkdir(parents=True)
    (hub / "skills" / "my-skill").mkdir()
    (hub / "skills" / "my-skill" / "SKILL.md").write_text("# x")

    config = _make_config(hub)
    entry = _make_entry()
    manifest = Manifest(
        generated_by_host="test",
        skills={"my-skill": entry},
    )

    result = status_mod.status(manifest, config, hub)
    assert len(result.skills) == 1
    assert result.skills[0].name == "my-skill"
    assert result.skills[0].state == "active"
    assert result.skills[0].version == "1.0.0"


def test_status_shows_detached(tmp_path, monkeypatch):
    """F9.1: detached skill shows 'detached' state."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    hub = tmp_path / "hub"
    (hub / "skills").mkdir(parents=True)
    config = _make_config(hub)
    entry = _make_entry()
    entry.target_override = []
    manifest = Manifest(
        generated_by_host="test",
        skills={"my-skill": entry},
    )

    result = status_mod.status(manifest, config, hub)
    assert result.skills[0].state == "detached"


def test_status_shows_uninstalled(tmp_path, monkeypatch):
    """F9.1: uninstalled skill shows 'uninstalled' state."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    hub = tmp_path / "hub"
    hub.mkdir()
    config = _make_config(hub)
    entry = _make_entry()
    manifest = Manifest(
        generated_by_host="test",
        skills={"my-skill": entry},
        tombstones={"my-skill": Tombstone("uninstalled", 1720096800, "host")},
    )

    result = status_mod.status(manifest, config, hub)
    assert result.skills[0].state == "uninstalled"


def test_status_json_output(tmp_path, monkeypatch, capsys):
    """F9.2: --json produces structured output."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    hub = tmp_path / "hub"
    hub.mkdir()
    config = _make_config(hub)
    manifest = Manifest(skills={"x": _make_entry("x")})

    result = status_mod.StatusResult(skills=[
        status_mod.SkillStatus(name="x", source="work", format="skill-md",
                               version="1.0.0", in_hub=True, state="active")
    ])
    status_mod.print_status(result, json_output=True)
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert "skills" in parsed
    assert parsed["skills"][0]["name"] == "x"


def test_invariants_no_violations(tmp_path, monkeypatch):
    """invariants returns empty list when everything is OK."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    hub = tmp_path / "hub"
    (hub / "skills").mkdir(parents=True)
    (hub / "skills" / "x").mkdir()
    (hub / "skills" / "x" / "SKILL.md").write_text("# x")
    (hub / ".uninstalled").mkdir()

    config = _make_config(hub)
    manifest = Manifest(skills={"x": _make_entry("x")})

    # Create the symlink so invariant passes
    agent_dir = fake_home / ".codex" / "skills"
    agent_dir.mkdir(parents=True)
    os.symlink(hub / "skills" / "x", agent_dir / "x")

    violations = status_mod.invariants(manifest, config, hub)
    assert violations == []


def test_invariants_detects_broken_symlink(tmp_path, monkeypatch):
    """invariants detects broken symlinks in agent dirs."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    hub = tmp_path / "hub"
    hub.mkdir()

    config = _make_config(hub)
    manifest = Manifest(skills={})

    # Create a broken symlink
    agent_dir = fake_home / ".codex" / "skills"
    agent_dir.mkdir(parents=True)
    os.symlink("/nonexistent/target", agent_dir / "broken")

    violations = status_mod.invariants(manifest, config, hub)
    assert any("broken" in v.lower() for v in violations)


def test_invariants_detects_unfinished_lifecycle(tmp_path, monkeypatch):
    """invariants detects skills in pending/restoring state."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    hub = tmp_path / "hub"
    hub.mkdir()

    config = _make_config(hub)
    manifest = Manifest(
        skills={"x": _make_entry("x")},
        tombstones={"x": Tombstone("pending", 1, "host")},
    )

    violations = status_mod.invariants(manifest, config, hub)
    assert any("pending" in v for v in violations)


def test_invariants_detects_symlink_in_uninstalled(tmp_path, monkeypatch):
    """T15: invariants detects symlinks in .uninstalled/ (path escape risk)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    hub = tmp_path / "hub"
    (hub / ".uninstalled").mkdir(parents=True)

    # Create a symlink in .uninstalled/
    evil_target = tmp_path / "evil"
    evil_target.mkdir()
    os.symlink(evil_target, hub / ".uninstalled" / "evil-link")

    config = _make_config(hub)
    manifest = Manifest(skills={})

    violations = status_mod.invariants(manifest, config, hub)
    assert any("symlink" in v.lower() and "uninstalled" in v.lower() for v in violations)
