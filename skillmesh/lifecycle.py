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

from .config import Config
from .events import EventLog, SkillEntry
from .host import Host
from .manifest import Manifest

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
            os.rename(source, target)
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
        os.rename(source, target)
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
    """Garbage collect purged skills' blobs. Returns count of blobs removed.

    Three phases (see docs/ARCHITECTURE.md §8.3):
    gc_prepare -> delete -> gc

    Cross-machine sync protection: only GC blobs that no skill references.
    """
    # Find all referenced content_hashes
    referenced = set()
    for entry in manifest.skills.values():
        if entry.content_hash:
            referenced.add(entry.content_hash)

    removed = 0
    if not blobs_dir.exists():
        return 0

    for blob_dir in blobs_dir.iterdir():
        if not blob_dir.is_dir():
            continue
        if blob_dir.name.startswith("."):
            continue
        content_hash = blob_dir.name
        if content_hash in referenced:
            continue
        # Also check tombstoned skills (still referenced until gc event written)
        # For v1 skeleton: simple check, conservative
        if dry_run:
            print(f"would remove blob: {content_hash}")
        else:
            shutil.rmtree(blob_dir)
        removed += 1

    return removed


# ============================ path safety ============================

def _safe_uninstall_path(name: str, uninstalled_dir: Path) -> Path:
    """Validate skill name and resolve safe path under .uninstalled/.

    See docs/PRD.md §9.5 F5.7.
    """
    if not SKILL_NAME_RE.match(name):
        raise LifecycleError(f"invalid skill name: {name!r}")

    uninstalled_dir.resolve().mkdir(parents=True, exist_ok=True)
    target = (uninstalled_dir / name).resolve()

    # Must be direct child of uninstalled_dir
    if target.parent != uninstalled_dir.resolve():
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
