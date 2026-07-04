"""Config loading and validation.

Supports TOML (Python 3.11+ tomllib) and JSON (all versions).
Schema: see docs/PRD.md §6.2, docs/ARCHITECTURE.md §13.

Path priority:
    1. --config <path>
    2. $SKILLMESH_CONFIG
    3. ~/.config/skillmesh/config.toml
    4. ~/.config/skillmesh/config.json
"""
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


CONFIG_ENV = "SKILLMESH_CONFIG"


def _default_config_dir() -> Path:
    """Compute default config dir based on current HOME (test-friendly)."""
    return Path(os.path.expanduser("~/.config/skillmesh"))

SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
LAYOUTS_V1 = {"directory", "file"}


class ConfigError(Exception):
    """Raised on config loading or validation errors."""
    pass


@dataclass
class Source:
    label: str
    prefix: str


@dataclass
class Agent:
    name: str
    dir: str
    accept_sources: List[str]
    layout: str = "directory"
    target_filename: Optional[str] = None

    def validate(self) -> None:
        if self.layout not in LAYOUTS_V1:
            raise ConfigError(
                f"agent {self.name}: layout must be one of {LAYOUTS_V1} in v1 "
                f"(got {self.layout!r}; 'single-file' is v1.1+)"
            )
        if self.layout == "file":
            if not self.target_filename:
                raise ConfigError(
                    f"agent {self.name}: layout=file requires target_filename"
                )
            if "{skill}" not in self.target_filename:
                raise ConfigError(
                    f"agent {self.name}: target_filename must contain "
                    f"'{{skill}}' placeholder (got {self.target_filename!r})"
                )


@dataclass
class Format:
    name: str
    filename: str


@dataclass
class Watch:
    dirs: List[str]
    interval: int = 60
    exclude: List[str] = field(default_factory=list)


@dataclass
class Conflicts:
    patterns: List[str] = field(default_factory=lambda: [
        r"\(\d+\)\.", "冲突", "_conflict", r"\.conflict$",
    ])


@dataclass
class Placeholders:
    suffixes: List[str] = field(default_factory=lambda: [
        ".icloud", ".baidudisk", ".downloading", ".tmp",
    ])


@dataclass
class Backup:
    path: str = "~/.local/state/skillmesh/backups"


@dataclass
class Hub:
    path: str
    sync_backend: str = "manual"
    compact_threshold: int = 1000


@dataclass
class Config:
    hub: Hub
    agents: List[Agent]
    watch: Watch
    sources: List[Source] = field(default_factory=list)
    formats: List[Format] = field(default_factory=list)
    conflicts: Conflicts = field(default_factory=Conflicts)
    placeholders: Placeholders = field(default_factory=Placeholders)
    backup: Backup = field(default_factory=Backup)

    def validate(self) -> None:
        if not self.hub.path:
            raise ConfigError("hub.path is required")
        if not self.agents:
            raise ConfigError("at least one agent is required")
        for agent in self.agents:
            agent.validate()
        if not self.watch.dirs:
            raise ConfigError("watch.dirs is required")
        # Check duplicate agent names
        names = [a.name for a in self.agents]
        if len(names) != len(set(names)):
            raise ConfigError(f"duplicate agent names: {names}")
        # M5: detect file-layout agents with same dir + target_filename
        # (would cause skills to overwrite each other's symlinks)
        file_targets = []
        for a in self.agents:
            if a.layout == "file" and a.target_filename:
                file_targets.append((a.dir, a.target_filename))
        if len(file_targets) != len(set(file_targets)):
            # Find duplicates for clearer error
            seen = {}
            for d, t in file_targets:
                key = (d, t)
                if key in seen:
                    raise ConfigError(
                        f"duplicate file-layout agent target: dir={d} "
                        f"target_filename={t}. Multiple agents writing to "
                        f"same path will overwrite each other."
                    )
                seen[key] = True

    def resolve_hub_path(self) -> Path:
        return _expand_path(self.hub.path)

    def resolve_agent_dir(self, agent: Agent) -> Path:
        return _expand_path(agent.dir)


def _expand_path(raw: str) -> Path:
    raw = os.path.expandvars(raw)
    return Path(raw).expanduser()


def load_config(explicit_path: Optional[str] = None) -> Config:
    """Load config from path priority list."""
    path = _resolve_config_path(explicit_path)
    if path is None:
        raise ConfigError(
            "no config found. Run `skillmesh init` to create one, or set "
            f"{CONFIG_ENV} env var, or pass --config <path>."
        )
    if path.suffix == ".toml":
        if sys.version_info < (3, 11):
            raise ConfigError(
                f"TOML config requires Python 3.11+ (current: {sys.version}). "
                f"Use config.json instead or upgrade Python."
            )
        import tomllib
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    elif path.suffix == ".json":
        raw = json.loads(path.read_text())
        raw = {k: v for k, v in raw.items() if not k.startswith("_")}
    else:
        raise ConfigError(f"unsupported config format: {path.suffix}")

    config = _normalize(raw)
    config.validate()
    return config


def _resolve_config_path(explicit: Optional[str]) -> Optional[Path]:
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise ConfigError(f"config not found: {p}")
        return p
    if env := os.environ.get(CONFIG_ENV):
        p = Path(env)
        if not p.exists():
            raise ConfigError(f"config not found: {p} (from env {CONFIG_ENV})")
        return p
    for name in ("config.toml", "config.json"):
        p = _default_config_dir() / name
        if p.exists():
            return p
    return None


def _normalize(raw: dict) -> Config:
    """Convert raw dict to Config dataclass, filling defaults."""
    hub_raw = raw.get("hub", {})
    hub = Hub(
        path=hub_raw.get("path", ""),
        sync_backend=hub_raw.get("sync_backend", "manual"),
        compact_threshold=hub_raw.get("compact_threshold", 1000),
    )

    agents = [
        Agent(
            name=a["name"],
            dir=a["dir"],
            accept_sources=list(a["accept_sources"]),
            layout=a.get("layout", "directory"),
            target_filename=a.get("target_filename"),
        )
        for a in raw.get("agents", [])
    ]

    sources = [
        Source(label=s["label"], prefix=s["prefix"])
        for s in raw.get("sources", [])
    ]

    formats = [
        Format(name=f["name"], filename=f["filename"])
        for f in raw.get("formats", [])
    ]

    watch_raw = raw.get("watch", {})
    watch = Watch(
        dirs=list(watch_raw.get("dirs", [])),
        interval=watch_raw.get("interval", 60),
        exclude=list(watch_raw.get("exclude", [])),
    )

    conflicts_raw = raw.get("conflicts") or {}
    if "patterns" in conflicts_raw:
        conflicts = Conflicts(patterns=list(conflicts_raw["patterns"]))
    else:
        conflicts = Conflicts()

    placeholders_raw = raw.get("placeholders") or {}
    if "suffixes" in placeholders_raw:
        placeholders = Placeholders(suffixes=list(placeholders_raw["suffixes"]))
    else:
        placeholders = Placeholders()

    backup_raw = raw.get("backup", {})
    backup = Backup(path=backup_raw.get("path", "~/.local/state/skillmesh/backups"))

    return Config(
        hub=hub,
        agents=agents,
        watch=watch,
        sources=sources,
        formats=formats,
        conflicts=conflicts,
        placeholders=placeholders,
        backup=backup,
    )
