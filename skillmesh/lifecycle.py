"""Lifecycle state machine: detach, attach, uninstall, forget, purge, gc.

All ops are GLOBAL scope (write event, propagate to all machines).
Local target override (per-machine "don't install to cursor") is v1.1+.

State diagram (docs/ARCHITECTURE.md §8):
    active --detach--> detached --attach--> active
    active --uninstall--> uninstalled --forget--> active
    uninstalled --purge--> purging --gc--> removed

Safety:
- skill name validated (alphanumeric . _ -)
- path must resolve to .uninstalled/<name> directly (no ../)
- path must not be symlink
- purge requires --yes

See docs/PRD.md §9.5.
"""
import os
import re
import shutil
from pathlib import Path

from . import distribution
from .config import Config
from .events import EventLog, SkillEntry
from .host import Host
from .manifest import Manifest
from .platform_support import atomic_replace, is_safe_portable_name

SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


class LifecycleError(Exception):
    pass


class ConfirmationRequired(LifecycleError):
    pass


class PathEscape(LifecycleError):
    pass


def detach(name: str, manifest: Manifest, event_log: EventLog,
           config: Config, skills_dir: Path) -> None:
    """Set target_override=[] and remove symlinks. Skill stays active in hub.

    Event-first: writes detach event BEFORE mutating in-memory entry.
    """
    entry = manifest.skills.get(name)
    if entry is None:
        raise LifecycleError(f"skill not found: {name}")

    # Build a copy with new override, write event first
    new_entry = SkillEntry(
        name=entry.name, source=entry.source, in_hub=entry.in_hub,
        format=entry.format, version=entry.version,
        blob_hash=entry.blob_hash, content_hash=entry.content_hash,
        target_override=[],
    )
    event_log.write("detach", new_entry)
    # Event written successfully, now mutate in-memory state
    entry.target_override = []
    _unlink_all(name, config)


def attach(name: str, manifest: Manifest, event_log: EventLog,
           config: Config, skills_dir: Path) -> None:
    """Restore target_override=null and re-symlink.

    Event-first: writes attach event BEFORE mutating in-memory entry.
    """
    entry = manifest.skills.get(name)
    if entry is None:
        raise LifecycleError(f"skill not found: {name}")

    new_entry = SkillEntry(
        name=entry.name, source=entry.source, in_hub=entry.in_hub,
        format=entry.format, version=entry.version,
        blob_hash=entry.blob_hash, content_hash=entry.content_hash,
        target_override=None,
    )
    event_log.write("attach", new_entry)
    entry.target_override = None
    _link_all(name, entry, config, skills_dir)


def uninstall(name: str, manifest: Manifest, event_log: EventLog,
              config: Config, hub_path: Path, dry_run: bool = False) -> None:
    """Move skill to .uninstalled/ and remove symlinks. Reversible via forget.

    Event-first: writes uninstall event BEFORE moving files. If write succeeds
    but move fails, scan/finalize will retry on next scan. If write fails,
    no files are touched (atomic from caller's perspective).
    """
    entry = manifest.skills.get(name)
    if entry is None:
        raise LifecycleError(f"skill not found: {name}")

    if dry_run:
        return

    uninstalled_dir = hub_path / ".uninstalled"
    skills_dir = hub_path / "skills"
    target = _safe_uninstall_path(name, uninstalled_dir)
    source = skills_dir / name

    # Event-first: write event before touching files
    entry.in_hub = False
    event_log.write("uninstall", entry)

    # Now safe to mutate files; if this fails, next scan sees the event
    # and finalizes (entry.in_hub=False, tombstone=uninstalled)
    _unlink_all(name, config)
    if source.exists():
        os.makedirs(uninstalled_dir, exist_ok=True)
        try:
            atomic_replace(source, target)
        except OSError:
            # Move failed; event already written. Next scan will retry.
            # Mark entry.in_hub back to True so user can retry.
            entry.in_hub = True
            raise


def forget(name: str, manifest: Manifest, event_log: EventLog,
           config: Config, hub_path: Path, skills_dir: Path,
           dry_run: bool = False) -> None:
    """Restore from .uninstalled/ to skills/. Reverse of uninstall.

    Event-first: writes forget event BEFORE moving files.
    """
    uninstalled_dir = hub_path / ".uninstalled"
    source = _safe_uninstall_path(name, uninstalled_dir)
    target = skills_dir / name

    if not source.exists():
        raise LifecycleError(f"skill not in .uninstalled/: {name}")

    if dry_run:
        return

    if target.exists():
        raise LifecycleError(f"target already exists: {target}")

    entry = manifest.skills.get(name)

    # Event-first
    if entry is not None:
        entry.in_hub = True
        event_log.write("forget", entry)

    # Now move; if fails, next scan sees forget event and retries
    try:
        atomic_replace(source, target)
    except OSError:
        if entry is not None:
            entry.in_hub = False
        raise

    if entry is not None:
        _link_all(name, entry, config, skills_dir)


def purge(name: str, manifest: Manifest, event_log: EventLog,
          config: Config, hub_path: Path, yes: bool = False,
          dry_run: bool = False) -> None:
    """Mark for GC. Requires --yes. Actual deletion happens in gc().

    Event-first: writes purge event BEFORE deleting files. gc() then
    removes unreferenced blobs.
    """
    if not yes:
        raise ConfirmationRequired(
            f"This will permanently delete skill '{name}'. "
            f"Re-run with --yes to confirm."
        )

    entry = manifest.skills.get(name)
    if entry is None:
        # Already purged? Check tombstones
        if name in manifest.tombstones:
            return
        raise LifecycleError(f"skill not found: {name}")

    if dry_run:
        return

    # Event-first: write purge event before any deletion
    event_log.write("purge", entry)

    # Remove symlinks (safe, reversible by re-apply if needed)
    _unlink_all(name, config)
    # Remove from .uninstalled/ if present
    uninstalled_dir = hub_path / ".uninstalled"
    uninstalled_target = _safe_uninstall_path(name, uninstalled_dir)
    if uninstalled_target.exists():
        shutil.rmtree(uninstalled_target)
    # Remove from skills/ working view if skillmesh-owned (uses cas._safe_remove
    # which checks for .skillmesh-owned marker - won't delete user content).
    skills_target = hub_path / "skills" / name
    if skills_target.exists() or skills_target.is_symlink():
        from . import cas
        cas._safe_remove(skills_target)


def gc(manifest: Manifest, event_log: EventLog, hub_path: Path,
       blobs_dir: Path, dry_run: bool = False) -> int:
    """Garbage collect unreferenced + purged blobs.

    Three-phase cross-machine protection (PRD §8.3, ARCHITECTURE §8.3):

    Phase 1 (purge, already done by `purge` command):
        tombstone state="purging", event "purge" written.

    Phase 2 (gc, this function):
        For each purging skill, check ALL known hosts have written a "purge"
        event for it. If yes, delete blob + write "gc" event (removes from
        manifest.skills). If no, skip (wait for cross-machine sync).

        Also delete truly unreferenced blobs (no skill in manifest references
        them) - these are safe orphans from failed operations.

    Phase 3 (gc event propagation):
        The "gc" event propagates to other machines via sync. They rebuild
        manifest, see skill removed, and their next gc can clean up too.

    Cross-machine safety:
        - We only delete a purging skill's blob if ALL known host_dirs have
          a "purge" event for that skill. "Known hosts" = subdirs under
          events/ (each host writes its own event_dir).
        - This prevents deleting a blob that another machine still references
          (e.g., A purges + gcs, but B hasn't received purge event yet).

    See docs/PRD.md §9.5 F5.6, docs/ARCHITECTURE.md §8.3.
    """
    if not blobs_dir.exists():
        return 0

    events_dir = hub_path / "events"
    known_hosts = _list_known_hosts(events_dir)

    # Build referenced set: all skills EXCEPT purging ones (their blobs
    # are eligible for deletion after cross-machine confirmation).
    referenced = set()
    purging_skills = []  # (name, entry) candidates for blob deletion

    for name, entry in manifest.skills.items():
        tomb = manifest.tombstones.get(name)
        if tomb and tomb.state == "purging":
            # Check if all known hosts have written "purge" for this skill
            if _all_hosts_confirmed_purge(events_dir, known_hosts, name):
                purging_skills.append((name, entry))
                # Don't add to referenced - blob can be deleted
            else:
                # Not all hosts confirmed yet - keep blob, skip deletion
                # Add to referenced to protect it
                if entry.content_hash:
                    referenced.add(entry.content_hash.replace(":", "-"))
        else:
            # Active / detached / uninstalled - all keep their blobs
            # (uninstalled is recoverable via forget, needs blob)
            if entry.content_hash:
                referenced.add(entry.content_hash.replace(":", "-"))

    removed = 0

    # Phase A: delete truly unreferenced blobs (orphans from failed ops)
    for blob_dir in blobs_dir.iterdir():
        if not blob_dir.is_dir() or blob_dir.name.startswith("."):
            continue
        if blob_dir.name in referenced:
            continue
        if dry_run:
            print(f"would remove orphan blob: {blob_dir.name}")
        else:
            shutil.rmtree(blob_dir)
        removed += 1

    # Phase B: delete purging skills' blobs (cross-machine confirmed)
    # and write "gc" event to remove from manifest
    for name, entry in purging_skills:
        if not entry.content_hash:
            continue
        from .cas import blob_path
        blob_dir = blob_path(blobs_dir, entry.content_hash)
        if blob_dir.exists():
            if dry_run:
                print(f"would remove purged blob: {entry.content_hash}")
            else:
                shutil.rmtree(blob_dir)
            removed += 1
        # Write gc event to remove skill from manifest
        if not dry_run:
            event_log.write("gc", entry)

    return removed


def _list_known_hosts(events_dir: Path) -> set:
    """Get set of host_dir names (each is <hostname>-<uuid8>)."""
    if not events_dir.exists():
        return set()
    return {
        d.name for d in events_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    }


def _all_hosts_confirmed_purge(events_dir: Path, known_hosts: set,
                                skill_name: str) -> bool:
    """Check that every known host has written a "purge" event for skill_name.

    Reads each host_dir's events and looks for any event with op="purge"
    and skill.name == skill_name.
    """
    if not known_hosts:
        # No hosts known yet - be conservative, don't delete
        return False

    import json
    for host_dir_name in known_hosts:
        host_dir = events_dir / host_dir_name
        if not host_dir.exists():
            continue
        found_purge = False
        for event_file in host_dir.glob("*.json"):
            try:
                data = json.loads(event_file.read_text())
                if (data.get("op") == "purge"
                        and data.get("skill", {}).get("name") == skill_name):
                    found_purge = True
                    break
            except (json.JSONDecodeError, KeyError):
                continue
        if not found_purge:
            return False  # this host hasn't purged yet
    return True


# ============================ path safety ============================

def _safe_uninstall_path(name: str, uninstalled_dir: Path) -> Path:
    """Validate skill name and resolve safe path under .uninstalled/.

    See docs/PRD.md §9.5 F5.7.
    """
    if not SKILL_NAME_RE.match(name) or not is_safe_portable_name(name):
        raise LifecycleError(f"invalid skill name: {name!r}")

    if (uninstalled_dir.is_symlink()
            or distribution.is_junction(uninstalled_dir)):
        raise PathEscape(".uninstalled root is a link/junction, refusing")

    uninstalled_dir.mkdir(parents=True, exist_ok=True)
    safe_root = uninstalled_dir.resolve()
    target = (uninstalled_dir / name).resolve()

    # Must be direct child of uninstalled_dir
    if target.parent != safe_root:
        raise PathEscape(f"path escapes .uninstalled/: {name}")

    # Must not be a symlink (could point outside)
    if target.is_symlink():
        raise PathEscape(f".uninstalled/{name} is a symlink, refusing")

    return target


# ============================ link helpers ============================

def _link_all(name: str, entry: SkillEntry, config: Config,
              skills_dir: Path) -> None:
    """Create symlinks for all target agents (used by attach/forget)."""
    if entry.target_override == []:
        return
    from .pipeline import _apply_links
    _apply_links(name, entry, config, skills_dir)


def _unlink_all(name: str, config: Config) -> None:
    """Remove symlinks from all agents (used by detach/uninstall)."""
    from .pipeline import _unlink_skill
    for agent in config.agents:
        _unlink_skill(name, agent, config)
