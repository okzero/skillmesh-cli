"""Shared fixtures for skillmesh tests.

Isolated HOME + temp config (TOML & JSON), no real dirs touched.
See docs/PRD.md §11.1 / docs/ARCHITECTURE.md §17.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Spawn an isolated skillmesh environment with fake HOME."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("SKILLMESH_HOST_ID", raising=False)
    monkeypatch.delenv("SKILLMESH_CONFIG", raising=False)
    monkeypatch.delenv("SKILLMESH_HOST_FILE", raising=False)

    config_dir = fake_home / ".config" / "skillmesh"
    config_dir.mkdir(parents=True)

    hub_dir = fake_home / "hub"
    hub_dir.mkdir()

    # Write minimal config (TOML if 3.11+, else JSON)
    if sys.version_info >= (3, 11):
        config_path = config_dir / "config.toml"
        config_path.write_text(_minimal_toml_config(hub_dir))
    else:
        config_path = config_dir / "config.json"
        config_path.write_text(json.dumps(_minimal_json_config(hub_dir), indent=2))

    return SkillmeshEnv(home=fake_home, hub=hub_dir, config=config_path)


@pytest.fixture
def isolated_env_json(isolated_env):
    """Force JSON config (for testing both formats on Python 3.11+)."""
    config_dir = isolated_env.config.parent
    toml = config_dir / "config.toml"
    json_path = config_dir / "config.json"
    if toml.exists():
        toml.unlink()
    json_path.write_text(json.dumps(_minimal_json_config(isolated_env.hub), indent=2))
    isolated_env.config = json_path
    return isolated_env


@pytest.fixture
def dual_envs(tmp_path, monkeypatch):
    """Two isolated environments simulating two machines."""
    env_a = _make_env(tmp_path / "machine_a", monkeypatch, "host-a")
    env_b = _make_env(tmp_path / "machine_b", monkeypatch, "host-b")
    return env_a, env_b


class SkillmeshEnv:
    """Test environment wrapper."""

    def __init__(self, home: Path, hub: Path, config: Path):
        self.home = home
        self.hub = hub
        self.config = config

    @property
    def config_dir(self) -> Path:
        return self.config.parent

    def make_skill(self, name: str, content: str = "# test skill",
                   fmt: str = "SKILL.md", version: str = "") -> Path:
        """Create a skill dir under ~/skills-source/<name> with given content."""
        src = self.home / "skills-source"
        src.mkdir(exist_ok=True)
        skill_dir = src / name
        skill_dir.mkdir(exist_ok=True)
        skill_file = skill_dir / fmt
        if version and fmt.endswith(".md"):
            skill_file.write_text(f"---\nversion: {version}\n---\n{content}\n")
        else:
            skill_file.write_text(content)
        return skill_dir

    def sync_hub_to(self, other_env: "SkillmeshEnv") -> None:
        """Simulate sync backend: copy hub dir to another env."""
        if other_env.hub.exists():
            shutil.rmtree(other_env.hub)
        shutil.copytree(self.hub, other_env.hub)


def _make_env(root: Path, monkeypatch, host_id: str) -> SkillmeshEnv:
    root.mkdir(parents=True)
    fake_home = root / "home"
    fake_home.mkdir()
    config_dir = fake_home / ".config" / "skillmesh"
    config_dir.mkdir(parents=True)
    hub_dir = fake_home / "hub"
    hub_dir.mkdir()

    if sys.version_info >= (3, 11):
        config_path = config_dir / "config.toml"
        config_path.write_text(_minimal_toml_config(hub_dir))
    else:
        config_path = config_dir / "config.json"
        config_path.write_text(json.dumps(_minimal_json_config(hub_dir), indent=2))

    # Force a stable host_id for testing
    host_file = config_dir / "host.json"
    host_file.write_text(json.dumps({
        "host_id": host_id,
        "host_display_name": host_id,
        "seq": 0,
        "lamport": 0,
        "created_at": 1720096800123456789,
    }))

    env = SkillmeshEnv(home=fake_home, hub=hub_dir, config=config_path)

    # Temporarily set HOME for this env's operations
    # (caller must manage monkeypatch when switching between envs)
    env._orig_home = os.environ.get("HOME")
    env._host_id = host_id
    return env


def activate(env: SkillmeshEnv, monkeypatch) -> None:
    """Switch monkeypatch to use the given env's HOME and host_id."""
    monkeypatch.setenv("HOME", str(env.home))
    monkeypatch.setenv("SKILLMESH_HOST_ID", env._host_id)


def _minimal_toml_config(hub_dir: Path) -> str:
    return f"""[hub]
path = "{hub_dir}"
sync_backend = "manual"

[[sources]]
label = "work"
prefix = "~/skills-source"

[[agents]]
name = "codex"
dir = "~/.codex/skills"
accept_sources = ["work"]
layout = "directory"

[[agents]]
name = "cursor"
dir = "~/.cursor/rules"
accept_sources = ["work"]
layout = "file"
target_filename = "{{skill}}.mdc"

[[formats]]
name = "skill-md"
filename = "SKILL.md"

[watch]
interval = 60
dirs = ["~/skills-source"]
exclude = ["skillmesh", "/.git/"]
"""


def _minimal_json_config(hub_dir: Path) -> dict:
    return {
        "hub": {
            "path": str(hub_dir),
            "sync_backend": "manual",
        },
        "sources": [
            {"label": "work", "prefix": "~/skills-source"},
        ],
        "agents": [
            {
                "name": "codex",
                "dir": "~/.codex/skills",
                "accept_sources": ["work"],
                "layout": "directory",
            },
            {
                "name": "cursor",
                "dir": "~/.cursor/rules",
                "accept_sources": ["work"],
                "layout": "file",
                "target_filename": "{skill}.mdc",
            },
        ],
        "formats": [
            {"name": "skill-md", "filename": "SKILL.md"},
        ],
        "watch": {
            "interval": 60,
            "dirs": ["~/skills-source"],
            "exclude": ["skillmesh", "/.git/"],
        },
    }
