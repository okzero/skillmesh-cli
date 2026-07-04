"""Pipeline: discover → plan → validate → execute four-phase.

See docs/ARCHITECTURE.md §9, docs/PRD.md §8.1.
"""
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional

from . import cas, discover, events
from .config import Agent, Config
from .discover import DiscoverResult, SkillCandidate
from .events import EventLog, SkillEntry
from .manifest import Manifest
from .host import Host


class OpType(Enum):
    ADD = "add"
    UPDATE = "update"
    IGNORE = "ignore"
    ORPHAN = "orphan"
    RELINK = "relink"
    MOVE = "move"  # for adopt


@dataclass
class Op:
    type: OpType
    name: str
    candidate: Optional[SkillCandidate] = None
    existing: Optional[SkillEntry] = None
    targets: Optional[List[Agent]] = None  # for RELINK
    current_links: Optional[List[Agent]] = None
    blocked: bool = False
    block_reason: str = ""
    skipped: bool = False

    def block(self, reason: str) -> None:
        self.blocked = True
        self.block_reason = reason

    def skip(self) -> None:
        self.skipped = True


@dataclass
class PlanResult:
    ops: List[Op] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def actionable(self) -> List[Op]:
        return [o for o in self.ops if not o.blocked and not o.skipped]


@dataclass
class ExecuteResult:
    succeeded: List[Op] = field(default_factory=list)
    failed: List[Op] = field(default_factory=list)
    skipped: List[Op] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def plan(discover_result: DiscoverResult, manifest: Manifest,
         config: Config) -> PlanResult:
    """Generate operation list by comparing candidates with manifest."""
    result = PlanResult()

    # ADD / UPDATE / IGNORE
    candidate_by_name = {c.name: c for c in discover_result.candidates}
    for candidate in discover_result.candidates:
        existing = manifest.skills.get(candidate.name)
        if existing is None:
            result.ops.append(Op(type=OpType.ADD, name=candidate.name,
                                 candidate=candidate))
        elif _content_differs(candidate, existing):
            result.ops.append(Op(type=OpType.UPDATE, name=candidate.name,
                                 candidate=candidate, existing=existing))
        else:
            result.ops.append(Op(type=OpType.IGNORE, name=candidate.name,
                                 candidate=candidate))

    # ORPHAN: in manifest but not in candidates
    for name, entry in manifest.skills.items():
        if name not in candidate_by_name:
            # Skip if in .uninstalled/ (lifecycle isolation, not orphan)
            if entry.in_hub is False:
                continue
            result.ops.append(Op(type=OpType.ORPHAN, name=name, existing=entry))

    # RELINK: targets differ from current symlinks
    for name, entry in manifest.skills.items():
        if entry.target_override == []:
            continue  # detached
        targets = _compute_targets(entry, config.agents)
        current = _read_current_links(name, config.agents, config)
        if {a.name for a in targets} != {a.name for a in current}:
            result.ops.append(Op(
                type=OpType.RELINK, name=name,
                targets=targets, current_links=current,
            ))

    return result


def validate(plan_result: PlanResult, config: Config,
             hub_path: Path) -> PlanResult:
    """Validate plan: check source exists, target writable, etc."""
    for op in plan_result.ops:
        if op.type in (OpType.ADD, OpType.UPDATE):
            if not op.candidate.path.exists():
                op.block("source missing")
            elif op.candidate.status == "sync-pending":
                op.block("sync-pending (placeholder file detected)")
        elif op.type == OpType.RELINK:
            if op.targets:
                for agent in op.targets:
                    agent_dir = config.resolve_agent_dir(agent)
                    if not agent_dir.parent.exists():
                        op.block(f"agent parent dir missing: {agent_dir.parent}")
                    # M2: detect user content at target path - refuse to overwrite
                    link_path = _agent_link_path(op.name, agent, config)
                    if link_path.exists() and not link_path.is_symlink():
                        op.block(
                            f"refusing to overwrite non-symlink at "
                            f"{link_path} (user content may be present). "
                            f"Remove it manually first."
                        )
        elif op.type == OpType.ORPHAN:
            # Check if entry is in .uninstalled/
            uninstalled = hub_path / ".uninstalled" / op.name
            if uninstalled.exists():
                op.skip()  # not orphan, just isolated
    return plan_result


def _agent_link_path(name: str, agent: Agent, config: Config) -> Path:
    """Compute the symlink path for a skill in an agent's dir."""
    agent_dir = config.resolve_agent_dir(agent)
    if agent.layout == "directory":
        return agent_dir / name
    elif agent.layout == "file":
        target_filename = agent.target_filename.replace("{skill}", name)
        return agent_dir / target_filename
    return agent_dir / name  # fallback


def execute(plan_result: PlanResult, config: Config, host: Host,
            hub_path: Path, event_log: EventLog,
            dry_run: bool = False) -> ExecuteResult:
    """Execute plan. Each op is transactional (rollback on failure)."""
    result = ExecuteResult()

    blobs_dir = hub_path / "blobs"
    skills_dir = hub_path / "skills"

    for op in plan_result.actionable:
        if dry_run:
            result.skipped.append(op)
            continue

        try:
            if op.type == OpType.ADD:
                _execute_add(op, config, host, blobs_dir, skills_dir, event_log)
                result.succeeded.append(op)
            elif op.type == OpType.UPDATE:
                _execute_update(op, config, host, blobs_dir, skills_dir, event_log)
                result.succeeded.append(op)
            elif op.type == OpType.RELINK:
                _execute_relink(op, config, skills_dir)
                result.succeeded.append(op)
            elif op.type == OpType.IGNORE:
                result.skipped.append(op)
            elif op.type == OpType.ORPHAN:
                # Just report, don't delete (let user decide)
                result.warnings.append(f"orphan: {op.name}")
                result.skipped.append(op)
        except Exception as e:
            op.block(str(e))
            result.failed.append(op)
            result.warnings.append(f"{op.name} failed: {e}")

    return result


def materialize_missing(manifest: Manifest, config: Config, hub_path: Path) -> int:
    """Materialize skills that exist in manifest but not in skills/ dir.

    Used after sync from another machine: events are replayed into manifest,
    but skills/<name>/ working view needs to be created from blobs.
    Returns count of materialized skills.

    Note: this only ADDS missing skills. To also refresh stale materializations
    (e.g., after update where manifest's content_hash differs from what's
    materialized), use reconcile_skills() instead.
    """
    skills_dir = hub_path / "skills"
    blobs_dir = hub_path / "blobs"
    skills_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    for name, entry in manifest.skills.items():
        if not entry.in_hub:
            continue
        if entry.target_override == []:
            continue  # detached, skip linking
        target = skills_dir / name
        if target.exists() or target.is_symlink():
            continue  # already there
        if not entry.content_hash:
            continue
        try:
            cas.materialize(entry.content_hash, name, skills_dir, blobs_dir)
            _write_owned_marker(target, entry.content_hash)
            _apply_links(name, entry, config, skills_dir)
            count += 1
        except FileNotFoundError:
            # blob not yet synced, will retry on next scan
            pass
    return count


def reconcile_skills(manifest: Manifest, config: Config, hub_path: Path) -> int:
    """Reconcile skills/ working view with manifest's logical state.

    Single source of truth for skills/<name>/ contents. Used after execute,
    rollback, sync, etc.

    For each skill in manifest:
    - If tombstone state is purging/gc_prepare: remove from skills/ (if
      skillmesh-owned)
    - If in_hub=False: skip (content is in .uninstalled/)
    - If skills/<name>/ missing: materialize from blob
    - If skills/<name>/ exists but content_hash marker differs from manifest:
      rematerialize (covers update with mixed-conflict where manifest keeps
      old version but execute materialized new)
    - Apply links to all target agents

    Returns count of changes (materialized + rematerialized + removed).
    """
    skills_dir = hub_path / "skills"
    blobs_dir = hub_path / "blobs"
    skills_dir.mkdir(parents=True, exist_ok=True)

    changes = 0
    for name, entry in manifest.skills.items():
        tomb = manifest.tombstones.get(name)
        target = skills_dir / name

        # Purging skills: remove working view
        if tomb and tomb.state in ("purging", "gc_prepare"):
            if target.exists() or target.is_symlink():
                try:
                    cas._safe_remove(target)
                    changes += 1
                except RuntimeError:
                    pass  # refuses to delete user content
            continue

        if not entry.in_hub:
            continue  # uninstalled, content is in .uninstalled/

        if not entry.content_hash:
            continue

        # Check if materialization matches manifest
        needs_materialize = False
        if not target.exists() and not target.is_symlink():
            needs_materialize = True
        else:
            # Compare marker's content_hash with manifest's
            current_hash = _read_owned_marker(target)
            if current_hash != entry.content_hash:
                needs_materialize = True

        if needs_materialize:
            try:
                cas.materialize(entry.content_hash, name, skills_dir, blobs_dir)
                _write_owned_marker(target, entry.content_hash)
                changes += 1
            except FileNotFoundError:
                # blob not yet synced, will retry on next scan
                continue
            except RuntimeError:
                # _safe_remove refused to delete user content - skip
                continue

        # Apply links (idempotent)
        if entry.target_override != []:
            _apply_links(name, entry, config, skills_dir)

    return changes


def _write_owned_marker(target: Path, content_hash: str) -> None:
    """Write .skillmesh-owned marker with current content_hash for reconcile."""
    (target / ".skillmesh-owned").write_text(
        f"content_hash={content_hash}\n"
    )


def _read_owned_marker(target: Path) -> str:
    """Read content_hash from .skillmesh-owned marker, or empty if missing."""
    marker = target / ".skillmesh-owned"
    if not marker.exists():
        return ""
    try:
        text = marker.read_text()
        for line in text.splitlines():
            if line.startswith("content_hash="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def relink_all(manifest: Manifest, config: Config, hub_path: Path) -> int:
    """Rebuild all symlinks per current manifest + config.

    Used by `apply` command and after rollback.
    """
    skills_dir = hub_path / "skills"
    count = 0
    for name, entry in manifest.skills.items():
        if not entry.in_hub or entry.target_override == []:
            continue
        target = skills_dir / name
        if not target.exists():
            continue
        _apply_links(name, entry, config, skills_dir)
        count += 1
    return count


# ============================ executors ============================

def _execute_add(op: Op, config: Config, host: Host, blobs_dir: Path,
                 skills_dir: Path, event_log: EventLog) -> None:
    """Write blob + add event. Materialization deferred to reconcile_skills().

    Why defer: F6.4 conflict resolution happens in manifest replay, not here.
    If this is a mixed-version conflict, replay may decide to keep the OLD
    version. Materializing here would overwrite skills/ with NEW content,
    creating inconsistency with manifest. reconcile_skills() runs after
    manifest rebuild and materializes whatever manifest says is the winner.

    Orphan blob note: if reconcile fails (e.g., blob deleted before sync),
    the blob written here remains unreferenced. lifecycle.gc() will clean
    it up as an orphan. Self-healing.
    """
    c = op.candidate
    content_hash = cas.write_blob(
        skill_dir=c.path,
        skill_name=c.name,
        format_name=c.format,
        version=c.version,
        blobs_dir=blobs_dir,
        host_id=host.host_id,
    )
    blob_hash = cas.read_meta(content_hash, blobs_dir).blob_hash

    entry = SkillEntry(
        name=c.name, source=c.source, in_hub=True,
        format=c.format, version=c.version,
        blob_hash=blob_hash, content_hash=content_hash,
    )
    event_log.write("add", entry)
    # Materialization + linking handled by reconcile_skills() in caller


def _execute_update(op: Op, config: Config, host: Host, blobs_dir: Path,
                    skills_dir: Path, event_log: EventLog) -> None:
    """Write new blob + update event. Materialization deferred to reconcile_skills().

    F6.4 conflict resolution: this writes the new blob + event. Manifest
    replay will decide winner (SemVer / Lamport / MIXED-VERSION-CONFLICT).
    reconcile_skills() then materializes manifest's chosen version. If mixed
    conflict, manifest keeps OLD content_hash, so skills/ stays at OLD -
    consistent with manifest.
    """
    c = op.candidate
    content_hash = cas.write_blob(
        skill_dir=c.path, skill_name=c.name,
        format_name=c.format, version=c.version,
        blobs_dir=blobs_dir, host_id=host.host_id,
    )
    blob_hash = cas.read_meta(content_hash, blobs_dir).blob_hash

    entry = SkillEntry(
        name=c.name, source=c.source, in_hub=True,
        format=c.format, version=c.version,
        blob_hash=blob_hash, content_hash=content_hash,
    )
    event_log.write("update", entry)
    # Materialization + linking handled by reconcile_skills() in caller


def _execute_relink(op: Op, config: Config, skills_dir: Path) -> None:
    # Remove old links not in targets
    if op.current_links:
        for agent in op.current_links:
            if op.targets and agent in op.targets:
                continue
            _unlink_skill(op.name, agent, config)
    # Add new links
    if op.targets:
        for agent in op.targets:
            _link_skill(op.name, agent, config, skills_dir)


def _apply_links(name: str, entry: SkillEntry, config: Config,
                 skills_dir: Path) -> None:
    """Create symlinks for all target agents."""
    if entry.target_override == []:
        return  # detached
    targets = _compute_targets(entry, config.agents)
    for agent in targets:
        _link_skill(name, agent, config, skills_dir)


def _link_skill(name: str, agent: Agent, config: Config, skills_dir: Path) -> None:
    """Create symlink from agent dir to hub/skills/<name>."""
    agent_dir = config.resolve_agent_dir(agent)
    agent_dir.mkdir(parents=True, exist_ok=True)

    source = skills_dir / name
    if not source.exists():
        return

    if agent.layout == "directory":
        link_path = agent_dir / name
    elif agent.layout == "file":
        # Find entry file in skill dir
        entry_file = _find_entry_file(source, config.formats)
        if entry_file is None:
            return
        target_filename = agent.target_filename.replace("{skill}", name)
        link_path = agent_dir / target_filename
        source = entry_file
    else:
        return  # v1 only supports directory/file

    # Remove existing symlink (or wrong target)
    if link_path.is_symlink() or link_path.exists():
        _safe_remove_link(link_path)

    os.symlink(source, link_path)


def _unlink_skill(name: str, agent: Agent, config: Config) -> None:
    agent_dir = config.resolve_agent_dir(agent)
    if agent.layout == "directory":
        link_path = agent_dir / name
    elif agent.layout == "file":
        target_filename = agent.target_filename.replace("{skill}", name)
        link_path = agent_dir / target_filename
    else:
        return

    if link_path.is_symlink():
        link_path.unlink()


def _find_entry_file(skill_dir: Path, formats) -> Optional[Path]:
    for fmt in formats:
        candidate = skill_dir / fmt.filename
        if candidate.exists():
            return candidate
    return None


def _compute_targets(entry: SkillEntry, agents: List[Agent]) -> List[Agent]:
    """F4.1: derive target agents from accept_sources (or target_override)."""
    if entry.target_override is not None:
        return [a for a in agents if a.name in entry.target_override]
    return [a for a in agents if entry.source in a.accept_sources]


def _read_current_links(name: str, agents: List[Agent], config: Config) -> List[Agent]:
    """Find which agents currently have a symlink for this skill."""
    result = []
    for agent in agents:
        agent_dir = config.resolve_agent_dir(agent)
        if agent.layout == "directory":
            link_path = agent_dir / name
        elif agent.layout == "file":
            target_filename = agent.target_filename.replace("{skill}", name)
            link_path = agent_dir / target_filename
        else:
            continue
        if link_path.is_symlink():
            result.append(agent)
    return result


def _content_differs(candidate: SkillCandidate, existing: SkillEntry) -> bool:
    """Check if candidate content differs from existing entry.

    Compares by content_hash (canonical merkle hash of skill directory).
    Version difference is a fast-path shortcut; if versions match, we still
    recompute hash to catch content changes without version bump.
    """
    if candidate.version != existing.version:
        return True
    # Recompute content_hash from candidate path and compare
    new_hash = cas.compute_content_hash(
        skill_dir=candidate.path,
        skill_name=candidate.name,
        format_name=candidate.format,
        version=candidate.version,
    )
    return new_hash != existing.content_hash


def _safe_remove_link(path: Path) -> None:
    """Remove a symlink at path. Refuse to delete real files/dirs.

    Security: agent target dirs may contain user content that predates
    skillmesh. We only remove our own symlinks, never rmtree real dirs.

    See review B5.
    """
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        # Real file or dir exists at target path - refuse to delete.
        # Caller should block the operation and prompt user to resolve manually.
        raise RuntimeError(
            f"refusing to delete existing non-symlink at {path} "
            f"(user content may be present). Remove it manually first."
        )
