"""Test config loading: TOML/JSON dual format, schema validation, Python version handling.

Covers T21a, T21b, T21c, T21d, T37, T38.
"""
import json
import sys
from pathlib import Path

import pytest

from skillmesh import config as cfg
from conftest import _minimal_toml_config, _minimal_json_config, set_test_home


def test_load_toml_config(isolated_env):
    """T21a: TOML config loads correctly on Python 3.11+."""
    if sys.version_info < (3, 11):
        pytest.skip("TOML requires Python 3.11+")
    config = cfg.load_config(str(isolated_env.config))
    assert config.hub.path.endswith("hub")
    assert len(config.agents) == 2
    assert config.agents[0].name == "codex"
    assert config.agents[0].layout == "directory"
    assert config.agents[1].layout == "file"
    assert config.agents[1].target_filename == "{skill}.mdc"


def test_load_json_config(isolated_env_json):
    """T21b: JSON config loads with same behavior as TOML."""
    config = cfg.load_config(str(isolated_env_json.config))
    assert config.hub.path.endswith("hub")
    assert len(config.agents) == 2
    assert config.agents[1].target_filename == "{skill}.mdc"


def test_toml_on_old_python_errors(isolated_env, monkeypatch):
    """T21c: Python 3.10- loading .toml raises clear error."""
    if sys.version_info >= (3, 11):
        pytest.skip("Test only applies to Python < 3.11")
    # The generic fixture creates JSON on old Python. Use an explicit TOML
    # input so this test exercises the unsupported format.
    toml_path = isolated_env.config_dir / "config.toml"
    toml_path.write_text(_minimal_toml_config(isolated_env), encoding="utf-8")
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load_config(str(toml_path))
    assert "Python 3.11" in str(exc.value) or "JSON" in str(exc.value)


def test_init_generates_correct_format(isolated_env, tmp_path, monkeypatch):
    """T21d: skillmesh init picks TOML on 3.11+, JSON on older."""
    # init is tested in test_cli.py; here just verify config file exists
    # and is in the right format
    config_path = isolated_env.config
    assert config_path.exists()
    if sys.version_info >= (3, 11):
        assert config_path.suffix == ".toml"
    else:
        assert config_path.suffix == ".json"


def test_layout_must_be_directory_or_file(tmp_path):
    """T38: v1 schema rejects 'single-file' layout."""
    raw = {
        "hub": {"path": str(tmp_path / "hub")},
        "agents": [{
            "name": "x", "dir": "~/x",
            "accept_sources": ["work"],
            "layout": "single-file",
        }],
        "watch": {"dirs": ["~/x"]},
    }
    config = cfg._normalize(raw)
    with pytest.raises(cfg.ConfigError) as exc:
        config.validate()
    assert "single-file" in str(exc.value) or "v1.1" in str(exc.value)


def test_file_layout_requires_target_filename(tmp_path):
    """T37: layout=file without target_filename is rejected."""
    raw = {
        "hub": {"path": str(tmp_path / "hub")},
        "agents": [{
            "name": "x", "dir": "~/x",
            "accept_sources": ["work"],
            "layout": "file",
            # missing target_filename
        }],
        "watch": {"dirs": ["~/x"]},
    }
    config = cfg._normalize(raw)
    with pytest.raises(cfg.ConfigError) as exc:
        config.validate()
    assert "target_filename" in str(exc.value)


def test_link_mode_defaults_to_auto_and_rejects_unknown(isolated_env_json):
    config = cfg.load_config(str(isolated_env_json.config))
    assert all(agent.link_mode == "auto" for agent in config.agents)
    config.agents[0].link_mode = "magic"
    with pytest.raises(cfg.ConfigError, match="link_mode"):
        config.validate()


def test_junction_link_mode_requires_directory_layout():
    agent = cfg.Agent(
        "cursor", "~/rules", ["work"], layout="file",
        target_filename="{skill}.mdc", link_mode="junction",
    )
    with pytest.raises(cfg.ConfigError, match="junction.*directory"):
        agent.validate()


def test_file_layout_requires_skill_placeholder(tmp_path):
    """T37: target_filename without {skill} placeholder is rejected."""
    raw = {
        "hub": {"path": str(tmp_path / "hub")},
        "agents": [{
            "name": "x", "dir": "~/x",
            "accept_sources": ["work"],
            "layout": "file",
            "target_filename": "fixed-name.mdc",  # no {skill}
        }],
        "watch": {"dirs": ["~/x"]},
    }
    config = cfg._normalize(raw)
    with pytest.raises(cfg.ConfigError) as exc:
        config.validate()
    assert "{skill}" in str(exc.value)


def test_duplicate_agent_names_rejected(tmp_path):
    raw = {
        "hub": {"path": str(tmp_path / "hub")},
        "agents": [
            {"name": "x", "dir": "~/x", "accept_sources": [], "layout": "directory"},
            {"name": "x", "dir": "~/y", "accept_sources": [], "layout": "directory"},
        ],
        "watch": {"dirs": ["~/x"]},
    }
    config = cfg._normalize(raw)
    with pytest.raises(cfg.ConfigError, match="duplicate"):
        config.validate()


def test_nonportable_file_target_template_rejected(tmp_path):
    raw = {
        "hub": {"path": str(tmp_path / "hub")},
        "agents": [{
            "name": "x", "dir": "~/x", "accept_sources": [],
            "layout": "file", "target_filename": "{skill}:stream",
        }],
        "formats": [{"name": "skill", "filename": "SKILL.md"}],
        "watch": {"dirs": ["~/x"]},
    }
    with pytest.raises(cfg.ConfigError, match="not portable"):
        cfg._normalize(raw).validate()


def test_nonportable_format_filename_rejected(tmp_path):
    raw = {
        "hub": {"path": str(tmp_path / "hub")},
        "agents": [{
            "name": "x", "dir": "~/x", "accept_sources": [],
            "layout": "directory",
        }],
        "formats": [{"name": "bad", "filename": "AUX.txt"}],
        "watch": {"dirs": ["~/x"]},
    }
    with pytest.raises(cfg.ConfigError, match="not portable"):
        cfg._normalize(raw).validate()


def test_missing_config_path_errors():
    """ConfigError raised when config not found."""
    with pytest.raises(cfg.ConfigError, match="config not found|no config"):
        cfg.load_config("/nonexistent/path.toml")


def test_no_config_anywhere_errors(tmp_path, monkeypatch):
    """ConfigError raised when no config found anywhere."""
    set_test_home(monkeypatch, tmp_path)
    monkeypatch.delenv("SKILLMESH_CONFIG", raising=False)
    with pytest.raises(cfg.ConfigError, match="no config"):
        cfg.load_config(None)


def test_default_conflict_patterns_loaded(tmp_path):
    """Defaults populated when conflicts/placeholders sections absent."""
    raw = {
        "hub": {"path": str(tmp_path / "hub")},
        "agents": [{"name": "x", "dir": "~/x", "accept_sources": [], "layout": "directory"}],
        "watch": {"dirs": ["~/x"]},
    }
    config = cfg._normalize(raw)
    assert r"\(\d+\)\." in config.conflicts.patterns
    assert "冲突" in config.conflicts.patterns
    assert ".icloud" in config.placeholders.suffixes


def test_resolve_hub_path_expands_home(isolated_env):
    config = cfg.load_config(str(isolated_env.config))
    p = config.resolve_hub_path()
    assert str(p) == str(isolated_env.hub)
