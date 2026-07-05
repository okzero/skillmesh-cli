"""Manifest: derived cache from snapshot + events replay.

Layout:
    manifest.json (in hub, but derived - can be deleted and rebuilt)

Contains:
    - revision, generated_by_host
    - agents/watch (LOCAL config copy, display only - cross-machine may differ)
    - skills (logical state)
    - tombstones (lifecycle markers)
    - event_fingerprint (hash of processed events; only write manifest when changed)

See docs/ARCHITECTURE.md §5, §7.
"""
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .config import Config
from .events import Event, EventLog, SkillEntry
from .platform_support import atomic_replace


@dataclass
class Tombstone:
    state: str  # "pending", "uninstalled", "restoring", "purging", "gc_prepare"
    ts: int
    host: str  # host_id

    def to_dict(self) -> dict:
        return self.__dict__

    @classmethod
    def from_dict(cls, data: dict) -> "Tombstone":
        return cls(**data)


@dataclass
class ConflictRecord:
    """Records a conflict that wasn't auto-resolved (e.g., MIXED-VERSION-CONFLICT).

    See docs/PRD.md §9.6 F6.4.
    """
    type: str  # "MIXED-VERSION-CONFLICT"
    kept: dict  # skill entry that was kept (existing)
    rejected: dict  # skill entry that was rejected (incoming)
    ts: int
    host: str  # host_id of the incoming event

    def to_dict(self) -> dict:
        return self.__dict__

    @classmethod
    def from_dict(cls, data: dict) -> "ConflictRecord":
        return cls(**data)


@dataclass
class Manifest:
    version: int = 1
    revision: int = 0
    generated_by_host: str = ""
    agents: dict = field(default_factory=dict)  # display only
    watch: dict = field(default_factory=dict)  # display only
    skills: Dict[str, SkillEntry] = field(default_factory=dict)
    tombstones: Dict[str, Tombstone] = field(default_factory=dict)
    conflicts: Dict[str, ConflictRecord] = field(default_factory=dict)
    event_fingerprint: str = ""

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "revision": self.revision,
            "generated_by_host": self.generated_by_host,
            "agents": self.agents,
            "watch": self.watch,
            "skills": {k: v.to_dict() for k, v in self.skills.items()},
            "tombstones": {k: v.to_dict() for k, v in self.tombstones.items()},
            "conflicts": {k: v.to_dict() for k, v in self.conflicts.items()},
            "event_fingerprint": self.event_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Manifest":
        return cls(
            version=data.get("version", 1),
            revision=data.get("revision", 0),
            generated_by_host=data.get("generated_by_host", ""),
            agents=data.get("agents", {}),
            watch=data.get("watch", {}),
            skills={
                k: SkillEntry.from_dict(v)
                for k, v in data.get("skills", {}).items()
            },
            tombstones={
                k: Tombstone.from_dict(v)
                for k, v in data.get("tombstones", {}).items()
            },
            conflicts={
                k: ConflictRecord.from_dict(v)
                for k, v in data.get("conflicts", {}).items()
            },
            event_fingerprint=data.get("event_fingerprint", ""),
        )


def load_or_rebuild(manifest_path: Path, snapshot: dict, event_log: EventLog,
                    config: Config, host_id: str,
                    repair: bool = True) -> Manifest:
    """Load manifest if valid, else rebuild from snapshot + events.

    See docs/ARCHITECTURE.md §11.1 for failure recovery.
    """
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text())
            manifest = Manifest.from_dict(data)
            # Verify fingerprint matches current events
            current_fp = _compute_fingerprint(event_log.read_all())
            if manifest.event_fingerprint == current_fp:
                return manifest
        except (json.JSONDecodeError, KeyError):
            # Corrupt - quarantine only for an executing command. Read-only
            # planning must not mutate the hub.
            if repair:
                corrupt = manifest_path.with_suffix(".corrupt")
                atomic_replace(manifest_path, corrupt)

    return rebuild(snapshot, event_log, config, host_id)


def rebuild(snapshot: dict, event_log: EventLog, config: Config,
            host_id: str) -> Manifest:
    """Rebuild manifest from snapshot + events (deterministic replay)."""
    # Start from snapshot baseline
    skills = {
        name: SkillEntry.from_dict(entry)
        for name, entry in snapshot.get("skills", {}).items()
    }
    tombstones = {
        name: Tombstone.from_dict(entry)
        for name, entry in snapshot.get("tombstones", {}).items()
    }
    conflicts = {
        name: ConflictRecord.from_dict(entry)
        for name, entry in snapshot.get("conflicts", {}).items()
    }

    # Get events not folded into snapshot
    folded_ids = set(snapshot.get("included_events", []))
    events = event_log.read_all_filtered(folded_ids)

    # Check for duplicate (host_id, seq)
    seen = set()
    for e in events:
        key = (e.host, e.seq)
        if key in seen:
            raise RuntimeError(
                f"fail-closed: duplicate (host, seq)={key}. "
                f"Manual intervention required."
            )
        seen.add(key)

    # Apply events in deterministic order (already sorted by read_all)
    for event in events:
        _apply_event(skills, tombstones, conflicts, event)

    # Compute fingerprint
    all_events = event_log.read_all()
    fingerprint = _compute_fingerprint(all_events)

    # Build manifest with LOCAL config snapshot (display only)
    manifest = Manifest(
        revision=len(all_events),
        generated_by_host=host_id,
        agents=_snapshot_agents(config),
        watch=_snapshot_watch(config),
        skills=skills,
        tombstones=tombstones,
        conflicts=conflicts,
        event_fingerprint=fingerprint,
    )

    return manifest


def save(manifest: Manifest, manifest_path: Path) -> None:
    """Atomically write manifest. Only writes if fingerprint changed.

    See docs/ARCHITECTURE.md §11 / §6 (空 scan 幂等).
    """
    if manifest_path.exists():
        try:
            existing = Manifest.from_dict(json.loads(manifest_path.read_text()))
            if existing.event_fingerprint == manifest.event_fingerprint:
                return  # no change, skip write (空 scan 幂等)
        except (json.JSONDecodeError, KeyError):
            pass  # will overwrite

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = manifest_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest.to_dict(), sort_keys=True, ensure_ascii=False))
    atomic_replace(tmp, manifest_path)


def _apply_event(skills: dict, tombstones: dict, conflicts: dict,
                 event: Event) -> None:
    """Apply a single event to the logical state.

    See docs/ARCHITECTURE.md §7.2 for apply rules.
    """
    name = event.skill.name
    tomb = tombstones.get(name)

    if event.op == "add":
        if tomb and tomb.state in ("uninstalled", "purging", "gc_prepare"):
            return  # skip: skill is in lifecycle isolation
        skills[name] = event.skill
        # Adding resolves any prior mixed conflict (new content wins)
        conflicts.pop(name, None)
    elif event.op == "update":
        if tomb and tomb.state in ("uninstalled", "purging", "gc_prepare"):
            return
        existing = skills.get(name)
        if existing:
            # F6.4 conflict resolution
            winner = _resolve_conflict(existing, event.skill)
            if winner is None:
                # MIXED-VERSION-CONFLICT: keep existing, record conflict
                conflicts[name] = ConflictRecord(
                    type="MIXED-VERSION-CONFLICT",
                    kept=existing.to_dict(),
                    rejected=event.skill.to_dict(),
                    ts=event.ts,
                    host=event.host,
                )
                # Keep existing entry; both blobs preserved in blobs/
            else:
                skills[name] = winner
                # Update resolved the conflict
                conflicts.pop(name, None)
        else:
            skills[name] = event.skill
    elif event.op == "detach":
        if name in skills:
            skills[name].target_override = []
    elif event.op == "attach":
        if name in skills:
            skills[name].target_override = None
    elif event.op == "uninstall":
        tombstones[name] = Tombstone("uninstalled", event.ts, event.host)
    elif event.op == "forget":
        tombstones.pop(name, None)
    elif event.op == "purge":
        tombstones[name] = Tombstone("purging", event.ts, event.host)
    elif event.op == "gc_prepare":
        if name in tombstones:
            tombstones[name].state = "gc_prepare"
    elif event.op == "gc":
        tombstones.pop(name, None)
        skills.pop(name, None)


def _resolve_conflict(existing: SkillEntry, incoming: SkillEntry) -> Optional[SkillEntry]:
    """F6.4 conflict resolution. Returns winner or None for MIXED-VERSION-CONFLICT.

    See docs/PRD.md §9.6 F6.4.

    Note: events are applied in (lamport, host_id, seq, id) order, so `incoming`
    is always the later-arrived event in replay order. When both lack SemVer,
    the incoming (higher lamport) wins.
    """
    a_ver = _parse_semver(existing.version)
    b_ver = _parse_semver(incoming.version)

    if a_ver is not None and b_ver is not None:
        return existing if a_ver >= b_ver else incoming
    if a_ver is None and b_ver is None:
        # Both lack SemVer - incoming wins (higher lamport, applied later)
        return incoming
    # Mixed: one has SemVer, one doesn't
    return None  # MIXED-VERSION-CONFLICT


def _parse_semver(version: str):
    """Parse '1.2.3' -> (1, 2, 3). Returns None if not SemVer."""
    if not version:
        return None
    parts = version.split(".")
    try:
        return tuple(int(p) for p in parts[:3])
    except ValueError:
        return None


def _compute_fingerprint(events: List[Event]) -> str:
    """Hash of all event ids (sorted) - changes iff events set changes."""
    h = hashlib.sha256()
    ids = sorted(e.id for e in events)
    for eid in ids:
        h.update(f"{eid}\n".encode("utf-8"))
    return f"sha256:{h.hexdigest()}"


def _snapshot_agents(config: Config) -> dict:
    """Capture local agents config for display in manifest (not synced truth)."""
    return {
        a.name: {
            "dir": a.dir,
            "accept_sources": a.accept_sources,
            "layout": a.layout,
            "target_filename": a.target_filename,
            "link_mode": a.link_mode,
        }
        for a in config.agents
    }


def _snapshot_watch(config: Config) -> dict:
    return {
        "dirs": config.watch.dirs,
        "interval": config.watch.interval,
        "exclude": config.watch.exclude,
    }
