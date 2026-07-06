"""End-to-end CLI tests: invoke actual skillmesh.py via main().

Covers M6: real CLI dual-machine convergence.
"""
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

# Ensure we can import skillmesh package
SKILLMESH_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(SKILLMESH_ROOT))

from skillmesh import cli as cli_mod, distribution
from skillmesh.cli import main
from conftest import set_test_home


HOST_A_ID = "aaaaaaaa-0000-0000-0000-000000000000"
HOST_B_ID = "bbbbbbbb-0000-0000-0000-000000000000"


def _make_machine(root: Path, host_id: str):
    """Set up a fake machine HOME with config + hub skeleton."""
    home = root / "home"
    home.mkdir(parents=True)
    config_dir = home / ".config" / "skillmesh"
    config_dir.mkdir(parents=True)
    hub = home / "hub"
    hub.mkdir()
    src_dir = home / "skills-source"
    src_dir.mkdir()

    # Force stable host_id via env var (cli.init would generate random)
    host_file = config_dir / "host.json"
    host_file.write_text(json.dumps({
        "host_id": host_id,
        "host_display_name": host_id[:8],
        "seq": 0,
        "lamport": 0,
        "created_at": 1720096800123456789,
    }))

    # Write config (JSON for portability across Python versions)
    config_path = config_dir / "config.json"
    config_path.write_text(json.dumps({
        "hub": {"path": str(hub), "sync_backend": "manual"},
        "sources": [{"label": "work", "prefix": "~/skills-source"}],
        "agents": [{
            "name": "codex",
            "dir": "~/.codex/skills",
            "accept_sources": ["work"],
            "layout": "directory",
        }],
        "formats": [{"name": "skill-md", "filename": "SKILL.md"}],
        "watch": {"dirs": ["~/skills-source"], "exclude": []},
    }))
    return home, hub, src_dir, config_path


def test_cli_init_generates_config_and_host(tmp_path, monkeypatch):
    """init creates config + host.json."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    set_test_home(monkeypatch, fake_home)
    monkeypatch.delenv("SKILLMESH_CONFIG", raising=False)
    monkeypatch.delenv("SKILLMESH_HOST_ID", raising=False)
    monkeypatch.setenv(
        "SKILLMESH_CONFIG_DIR", str(fake_home / ".config" / "skillmesh")
    )

    rc = main(["init"])
    assert rc == 0

    config_dir = fake_home / ".config" / "skillmesh"
    assert (config_dir / "config.toml").exists() or (config_dir / "config.json").exists()
    assert (config_dir / "host.json").exists()


def test_windows_python310_init_rewrites_json_defaults(tmp_path, monkeypatch):
    """Windows 3.9/3.10 init must not emit macOS/Linux paths."""
    config_dir = tmp_path / "windows-config"
    monkeypatch.setenv("SKILLMESH_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(cli_mod.sys, "version_info", (3, 10, 0))
    monkeypatch.setattr(
        cli_mod.platform_support, "system_name", lambda: "Windows"
    )
    monkeypatch.setattr(
        cli_mod.host_mod,
        "load_or_create_host",
        lambda: type("TestHost", (), {
            "host_id": "windows-test-host",
            "display_name": "windows-test",
        })(),
    )

    assert main(["init"]) == 0
    generated = json.loads(
        (config_dir / "config.json").read_text(encoding="utf-8")
    )
    assert generated["hub"] == {
        "path": "%USERPROFILE%/skillmesh-hub",
        "sync_backend": "manual",
        "compact_threshold": 1000,
    }
    assert generated["backup"]["path"] == (
        "%LOCALAPPDATA%/skillmesh/backups"
    )


def test_cli_scan_discovers_and_distributes(tmp_path, monkeypatch):
    """scan via CLI discovers skill and creates symlinks."""
    home, hub, src_dir, config_path = _make_machine(tmp_path, HOST_A_ID)
    set_test_home(monkeypatch, home)
    monkeypatch.setenv("SKILLMESH_CONFIG", str(config_path))

    # Create a skill
    skill_dir = src_dir / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# test skill")

    rc = main(["scan"])
    assert rc == 0

    # Symlink created
    link = home / ".codex" / "skills" / "my-skill"
    assert distribution.inspect(link, hub / "skills" / "my-skill").correct
    assert (link / "SKILL.md").exists()


def test_cli_status_shows_skill(tmp_path, monkeypatch, capsys):
    """status via CLI shows the skill."""
    home, hub, src_dir, config_path = _make_machine(tmp_path, HOST_A_ID)
    set_test_home(monkeypatch, home)
    monkeypatch.setenv("SKILLMESH_CONFIG", str(config_path))

    skill_dir = src_dir / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# test skill")

    main(["scan"])
    capsys.readouterr()  # clear

    rc = main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "my-skill" in out
    assert "active" in out


def test_cli_uninstall_forget_roundtrip(tmp_path, monkeypatch):
    """uninstall + forget via CLI works end-to-end."""
    home, hub, src_dir, config_path = _make_machine(tmp_path, HOST_A_ID)
    set_test_home(monkeypatch, home)
    monkeypatch.setenv("SKILLMESH_CONFIG", str(config_path))

    skill_dir = src_dir / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# test skill")

    main(["scan"])

    # Uninstall
    rc = main(["uninstall", "my-skill"])
    assert rc == 0
    # Skill moved to .uninstalled/
    assert (hub / ".uninstalled" / "my-skill").exists()
    assert not (hub / "skills" / "my-skill").exists()

    # Forget (restore)
    rc = main(["forget", "my-skill"])
    assert rc == 0
    assert (hub / "skills" / "my-skill").exists()
    assert not (hub / ".uninstalled" / "my-skill").exists()


def test_cli_purge_requires_yes(tmp_path, monkeypatch):
    """purge without --yes exits non-zero (argparse required=True)."""
    home, hub, src_dir, config_path = _make_machine(tmp_path, HOST_A_ID)
    set_test_home(monkeypatch, home)
    monkeypatch.setenv("SKILLMESH_CONFIG", str(config_path))

    skill_dir = src_dir / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# test skill")
    main(["scan"])

    # purge without --yes should fail at argparse level
    with pytest.raises(SystemExit):
        main(["purge", "my-skill"])


def test_cli_dual_machine_convergence(tmp_path, monkeypatch):
    """M6: real CLI dual-machine convergence.

    A scans skill → sync hub to B → B scans → B has skill + symlinks.
    """
    # Set up two machines
    machine_a = tmp_path / "a"
    machine_b = tmp_path / "b"
    home_a, hub_a, src_a, config_a = _make_machine(machine_a, HOST_A_ID)
    home_b, hub_b, src_b, config_b = _make_machine(machine_b, HOST_B_ID)

    # A creates and scans skill
    set_test_home(monkeypatch, home_a)
    monkeypatch.setenv("SKILLMESH_CONFIG", str(config_a))
    skill_dir = src_a / "shared-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# from machine A")
    rc = main(["scan"])
    assert rc == 0

    # Verify A has skill
    assert (hub_a / "skills" / "shared-skill").exists()
    assert distribution.inspect(
        home_a / ".codex" / "skills" / "shared-skill",
        hub_a / "skills" / "shared-skill",
    ).correct

    # Sync hub from A to B (simulating cloud drive)
    shutil.rmtree(hub_b)
    shutil.copytree(hub_a, hub_b)

    # B scans
    set_test_home(monkeypatch, home_b)
    monkeypatch.setenv("SKILLMESH_CONFIG", str(config_b))
    rc = main(["scan"])
    assert rc == 0

    # B should have skill materialized and symlinked
    assert (hub_b / "skills" / "shared-skill").exists()
    assert (hub_b / "skills" / "shared-skill" / "SKILL.md").read_text() == "# from machine A"
    # B's agent dir has symlink
    link_b = home_b / ".codex" / "skills" / "shared-skill"
    assert distribution.inspect(
        link_b, hub_b / "skills" / "shared-skill"
    ).correct


def test_cli_backup_rollback_roundtrip(tmp_path, monkeypatch):
    """backup + rollback via CLI works end-to-end."""
    home, hub, src_dir, config_path = _make_machine(tmp_path, HOST_A_ID)
    set_test_home(monkeypatch, home)
    monkeypatch.setenv("SKILLMESH_CONFIG", str(config_path))

    skill_dir = src_dir / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# original")
    main(["scan"])

    # Backup
    rc = main(["backup"])
    assert rc == 0

    # Delete hub content
    shutil.rmtree(hub / "skills")
    (hub / "snapshot.json").unlink()
    (hub / "manifest.json").unlink()

    # Rollback
    rc = main(["rollback"])
    assert rc == 0

    # Content restored
    assert (hub / "skills" / "my-skill").exists()
    assert (hub / "skills" / "my-skill" / "SKILL.md").read_text() == "# original"
    # Symlink rebuilt by rollback's apply step
    assert distribution.inspect(
        home / ".codex" / "skills" / "my-skill",
        hub / "skills" / "my-skill",
    ).correct


def test_cli_invariants_passes_when_clean(tmp_path, monkeypatch):
    """invariants via CLI returns 0 when no violations."""
    home, hub, src_dir, config_path = _make_machine(tmp_path, HOST_A_ID)
    set_test_home(monkeypatch, home)
    monkeypatch.setenv("SKILLMESH_CONFIG", str(config_path))

    skill_dir = src_dir / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# test")
    main(["scan"])

    rc = main(["invariants"])
    assert rc == 0


def test_cli_1000_empty_scans_do_not_rewrite_manifest(
    tmp_path, monkeypatch, capsys
):
    home, hub, src_dir, config_path = _make_machine(tmp_path, HOST_A_ID)
    set_test_home(monkeypatch, home)
    monkeypatch.setenv("SKILLMESH_CONFIG", str(config_path))
    (src_dir / "my-skill").mkdir()
    (src_dir / "my-skill" / "SKILL.md").write_text("# stable")
    assert main(["scan"]) == 0
    capsys.readouterr()

    manifest_path = hub / "manifest.json"
    original = manifest_path.read_bytes()
    original_mtime = manifest_path.stat().st_mtime_ns
    for _ in range(1000):
        assert main(["scan"]) == 0

    assert manifest_path.read_bytes() == original
    assert manifest_path.stat().st_mtime_ns == original_mtime


def test_cli_dry_run_makes_no_changes(tmp_path, monkeypatch):
    """scan --dry-run writes nothing."""
    home, hub, src_dir, config_path = _make_machine(tmp_path, HOST_A_ID)
    set_test_home(monkeypatch, home)
    monkeypatch.setenv("SKILLMESH_CONFIG", str(config_path))

    skill_dir = src_dir / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# test")

    rc = main(["scan", "--dry-run"])
    assert rc == 0

    # No blobs, no events, no symlinks
    assert not (hub / "blobs").exists()
    assert not (hub / "events").exists()
    assert not (home / ".codex" / "skills" / "my-skill").exists()


def test_cli_dry_run_does_not_create_missing_hub(tmp_path, monkeypatch):
    home, hub, src_dir, config_path = _make_machine(tmp_path, HOST_A_ID)
    set_test_home(monkeypatch, home)
    monkeypatch.setenv("SKILLMESH_CONFIG", str(config_path))
    (src_dir / "my-skill").mkdir()
    (src_dir / "my-skill" / "SKILL.md").write_text("# test")
    shutil.rmtree(hub)

    assert main(["scan", "--dry-run"]) == 0
    assert not hub.exists()
