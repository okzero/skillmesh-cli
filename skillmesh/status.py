"""Status and invariants: observability commands.

See docs/PRD.md §9.9.
"""
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from .config import Config
from .manifest import Manifest


@dataclass
class SkillStatus:
    name: str
    source: str
    format: str
    version: str
    in_hub: bool
    state: str  # "active", "detached", "uninstalled", "purging"
    targets: List[str] = field(default_factory=list)
    current_links: List[str] = field(default_factory=list)
    orphan: bool = False
    wrong_target: bool = False
    sync_pending: bool = False
    conflict: str = ""  # "MIXED-VERSION-CONFLICT" or "VERSIONLESS-CONFLICT"


@dataclass
class StatusResult:
    skills: List[SkillStatus] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "skills": [s.__dict__ for s in self.skills],
            "warnings": self.warnings,
        }


def status(manifest: Manifest, config: Config, hub_path: Path) -> StatusResult:
    """Build status report. Pure read, no writes."""
    result = StatusResult()
    skills_dir = hub_path / "skills"

    for name, entry in manifest.skills.items():
        tomb = manifest.tombstones.get(name)
        if tomb:
            state = tomb.state
        elif entry.target_override == []:
            state = "detached"
        else:
            state = "active" if entry.in_hub else "isolated"

        targets = _compute_target_names(entry, config.agents)
        current = _read_current_link_names(name, config)

        skill_status = SkillStatus(
            name=name,
            source=entry.source,
            format=entry.format,
            version=entry.version,
            in_hub=entry.in_hub,
            state=state,
            targets=targets,
            current_links=current,
        )

        # Detect issues
        # M2: don't mark ORPHAN for skills in lifecycle isolation
        if entry.in_hub and state == "active":
            if not (skills_dir / name).exists():
                skill_status.orphan = True
        if set(targets) != set(current) and state == "active":
            skill_status.wrong_target = True

        # Show conflict if recorded
        conflict = manifest.conflicts.get(name)
        if conflict:
            skill_status.conflict = conflict.type

        result.skills.append(skill_status)

    return result


def invariants(manifest: Manifest, config: Config, hub_path: Path) -> List[str]:
    """Check invariants. Returns list of violations (empty = OK)."""
    violations = []
    skills_dir = hub_path / "skills"
    uninstalled_dir = hub_path / ".uninstalled"

    # 1. No duplicate skill names (already enforced in config, double-check)
    names = list(manifest.skills.keys())
    if len(names) != len(set(names)):
        violations.append(f"duplicate skill names: {names}")

    # 2. No skills in unfinished lifecycle state
    for name, tomb in manifest.tombstones.items():
        if tomb.state in ("pending", "restoring"):
            violations.append(
                f"skill {name!r} in unfinished state: {tomb.state}"
            )

    # 3. No path escapes in .uninstalled/
    import re
    name_re = re.compile(r"^[a-zA-Z0-9._-]+$")
    if uninstalled_dir.exists():
        for entry in uninstalled_dir.iterdir():
            if entry.is_symlink():
                violations.append(
                    f".uninstalled/{entry.name} is a symlink (path escape risk)"
                )
            if not name_re.match(entry.name):
                violations.append(
                    f".uninstalled/{entry.name} has invalid name"
                )

    # 4. No broken symlinks in agent dirs
    for agent in config.agents:
        agent_dir = config.resolve_agent_dir(agent)
        if not agent_dir.exists():
            continue
        for link in agent_dir.iterdir():
            if link.is_symlink() and not link.exists():
                violations.append(f"broken symlink: {link}")

    # 5. All active skills should have links to all target agents
    for name, entry in manifest.skills.items():
        if entry.target_override == []:
            continue
        if not entry.in_hub:
            continue
        targets = _compute_target_names(entry, config.agents)
        current = _read_current_link_names(name, config)
        if set(targets) != set(current):
            violations.append(
                f"skill {name!r}: targets={targets} but links={current}"
            )

    return violations


def print_status(result: StatusResult, json_output: bool = False) -> None:
    """Print status to stdout."""
    if json_output:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return

    if not result.skills:
        print("No skills registered. Run `skillmesh scan` to discover.")
        return

    # Human-readable table
    print(f"{'NAME':<30} {'STATE':<12} {'VERSION':<10} {'TARGETS':<30} STATUS")
    print("-" * 90)
    for s in result.skills:
        target_str = ",".join(s.targets) if s.targets else "-"
        issues = []
        if s.orphan:
            issues.append("ORPHAN")
        if s.wrong_target:
            issues.append("WRONG-TARGET")
        if s.sync_pending:
            issues.append("SYNC-PENDING")
        if s.conflict:
            issues.append(s.conflict)
        status_str = " ".join(issues) if issues else "OK"
        print(
            f"{s.name:<30} {s.state:<12} {s.version:<10} "
            f"{target_str:<30} {status_str}"
        )

    if result.warnings:
        print("\nWarnings:")
        for w in result.warnings:
            print(f"  - {w}")


# ============================ helpers ============================

def _compute_target_names(entry, agents) -> List[str]:
    """F4.1 target derivation - returns agent names."""
    if entry.target_override is not None:
        return entry.target_override
    return [a.name for a in agents if entry.source in a.accept_sources]


def _read_current_link_names(name: str, config: Config) -> List[str]:
    """Find which agents currently have a symlink for this skill."""
    result = []
    for agent in config.agents:
        agent_dir = config.resolve_agent_dir(agent)
        if agent.layout == "directory":
            link_path = agent_dir / name
        elif agent.layout == "file":
            target_filename = agent.target_filename.replace("{skill}", name)
            link_path = agent_dir / target_filename
        else:
            continue
        if link_path.is_symlink():
            result.append(agent.name)
    return result
