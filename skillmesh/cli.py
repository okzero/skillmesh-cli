"""CLI command router.

Implements commands per docs/PRD.md §8.2 with default behavior per §8.4.

All write commands support --dry-run.
purge requires --yes (not --dry-run).
"""
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

from . import (
    backup,
    cas,
    config as config_mod,
    discover,
    events,
    host as host_mod,
    lifecycle,
    manifest as manifest_mod,
    pipeline,
    platform_daemon,
    status as status_mod,
)


def main(argv: list) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    try:
        return _dispatch(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillmesh",
        description="Multi-agent, multi-machine skill sync via any cloud drive.",
    )
    parser.add_argument("--config", help="config file path (TOML or JSON)")
    parser.add_argument("--version", action="version", version="skillmesh 0.1.0")

    sub = parser.add_subparsers(dest="command")

    # init
    sub.add_parser("init", help="generate config template and hub skeleton")

    # scan
    p_scan = sub.add_parser("scan", help="discover + plan + validate + execute + apply")
    p_scan.add_argument("--dry-run", action="store_true")

    # apply
    p_apply = sub.add_parser("apply", help="relink skills to agents only")
    p_apply.add_argument("--dry-run", action="store_true")

    # adopt
    p_adopt = sub.add_parser("adopt", help="adopt existing skills (first-time import)")
    p_adopt.add_argument("--yes", action="store_true", help="skip per-stage confirmation")
    p_adopt.add_argument("--dry-run", action="store_true")

    # status
    p_status = sub.add_parser("status", help="show current state")
    p_status.add_argument("--json", action="store_true")

    # invariants
    sub.add_parser("invariants", help="check invariants")

    # detach / attach
    p_detach = sub.add_parser("detach", help="detach a skill (remove links, keep content)")
    p_detach.add_argument("name")
    p_detach.add_argument("--dry-run", action="store_true")

    p_attach = sub.add_parser("attach", help="re-attach a detached skill")
    p_attach.add_argument("name")
    p_attach.add_argument("--dry-run", action="store_true")

    # uninstall / forget
    p_uninstall = sub.add_parser("uninstall", help="move skill to .uninstalled/")
    p_uninstall.add_argument("name")
    p_uninstall.add_argument("--dry-run", action="store_true")

    p_forget = sub.add_parser("forget", help="restore from .uninstalled/")
    p_forget.add_argument("name")
    p_forget.add_argument("--dry-run", action="store_true")

    # purge
    p_purge = sub.add_parser("purge", help="permanently delete a skill (requires --yes)")
    p_purge.add_argument("name")
    p_purge.add_argument("--yes", action="store_true", required=True)

    # gc
    p_gc = sub.add_parser("gc", help="garbage collect unreferenced blobs")
    p_gc.add_argument("--dry-run", action="store_true")

    # compact
    p_compact = sub.add_parser("compact", help="compact events into new snapshot")

    # backup / rollback
    sub.add_parser("backup", help="create a full hub backup")
    p_rollback = sub.add_parser("rollback", help="restore from backup")
    p_rollback.add_argument("backup_dir", nargs="?", help="backup dir (default: latest)")

    # install_daemon / uninstall_daemon
    sub.add_parser("install_daemon", help="install launchd/systemd daemon")
    sub.add_parser("uninstall_daemon", help="uninstall daemon")

    return parser


def _dispatch(args) -> int:
    cmd = args.command

    if cmd == "init":
        return _cmd_init(args)

    # All other commands need config + host
    config = config_mod.load_config(getattr(args, "config", None))
    host = host_mod.load_or_create_host()
    hub_path = config.resolve_hub_path()
    hub_path.mkdir(parents=True, exist_ok=True)

    if cmd == "scan":
        return _cmd_scan(args, config, host, hub_path)
    elif cmd == "apply":
        return _cmd_apply(args, config, host, hub_path)
    elif cmd == "adopt":
        return _cmd_adopt(args, config, host, hub_path)
    elif cmd == "status":
        return _cmd_status(args, config, host, hub_path)
    elif cmd == "invariants":
        return _cmd_invariants(args, config, host, hub_path)
    elif cmd == "detach":
        return _cmd_detach(args, config, host, hub_path)
    elif cmd == "attach":
        return _cmd_attach(args, config, host, hub_path)
    elif cmd == "uninstall":
        return _cmd_uninstall(args, config, host, hub_path)
    elif cmd == "forget":
        return _cmd_forget(args, config, host, hub_path)
    elif cmd == "purge":
        return _cmd_purge(args, config, host, hub_path)
    elif cmd == "gc":
        return _cmd_gc(args, config, host, hub_path)
    elif cmd == "compact":
        return _cmd_compact(args, config, host, hub_path)
    elif cmd == "backup":
        return _cmd_backup(args, config, host, hub_path)
    elif cmd == "rollback":
        return _cmd_rollback(args, config, host, hub_path)
    elif cmd == "install_daemon":
        return _cmd_install_daemon(args, config, host, hub_path)
    elif cmd == "uninstall_daemon":
        return _cmd_uninstall_daemon(args)
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        return 1


# ============================ command implementations ============================

def _cmd_init(args) -> int:
    """Generate config template and hub skeleton."""
    config_dir = host_mod._default_host_file().parent
    config_dir.mkdir(parents=True, exist_ok=True)

    # Generate config (TOML for 3.11+, JSON for older)
    import sys
    example_dir = Path(__file__).parent.parent
    if sys.version_info >= (3, 11):
        src = example_dir / "config.example.toml"
        dst = config_dir / "config.toml"
    else:
        src = example_dir / "config.example.json"
        dst = config_dir / "config.json"

    if dst.exists():
        print(f"config already exists: {dst}")
    else:
        import shutil
        shutil.copy(src, dst)
        print(f"created config: {dst}")
        print("Edit it to match your setup, then run `skillmesh scan`.")

    # Generate host.json
    host = host_mod.load_or_create_host()
    print(f"host_id: {host.host_id}")
    print(f"display_name: {host.display_name}")
    return 0


def _cmd_scan(args, config, host, hub_path) -> int:
    dry_run = getattr(args, "dry_run", False)
    if dry_run:
        print("DRY RUN - no changes will be made")

    blobs_dir = hub_path / "blobs"
    skills_dir = hub_path / "skills"
    events_dir = hub_path / "events"
    manifest_path = hub_path / "manifest.json"
    snapshot_path = hub_path / "snapshot.json"

    blobs_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)
    events_dir.mkdir(parents=True, exist_ok=True)

    # Load or create snapshot
    snapshot = _load_or_init_snapshot(snapshot_path, host)

    # Load event log
    event_log = events.EventLog(events_dir, host)

    # Load manifest (rebuild if needed)
    manifest = manifest_mod.load_or_rebuild(
        manifest_path, snapshot, event_log, config, host.host_id
    )

    # Discover
    result = discover.discover(config)
    for w in result.warnings:
        print(f"warn: {w}", file=sys.stderr)

    # Plan
    plan_result = pipeline.plan(result, manifest, config)
    plan_result = pipeline.validate(plan_result, config, hub_path)

    # Execute
    exec_result = pipeline.execute(
        plan_result, config, host, hub_path, event_log, dry_run=dry_run
    )

    # Save manifest (only if changed)
    if not dry_run:
        # Rebuild manifest after execution to reflect new events
        manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
        manifest_mod.save(manifest, manifest_path)
        # Reconcile skills/ working view with manifest's logical state.
        # This handles: ADD (materialize new), UPDATE (materialize winner,
        # including reverting to OLD if mixed-conflict), sync from other
        # machines, link refresh, etc. Single source of truth for skills/.
        changes = pipeline.reconcile_skills(manifest, config, hub_path)
        if changes:
            print(f"reconciled: {changes} skill(s)")

    # Report
    print(f"succeeded: {len(exec_result.succeeded)}")
    print(f"failed: {len(exec_result.failed)}")
    print(f"skipped: {len(exec_result.skipped)}")
    for w in exec_result.warnings:
        print(f"warn: {w}", file=sys.stderr)

    return 0 if not exec_result.failed else 2


def _cmd_apply(args, config, host, hub_path) -> int:
    """Re-link all skills per current manifest + config."""
    dry_run = getattr(args, "dry_run", False)
    skills_dir = hub_path / "skills"
    events_dir = hub_path / "events"
    manifest_path = hub_path / "manifest.json"
    snapshot_path = hub_path / "snapshot.json"

    snapshot = _load_or_init_snapshot(snapshot_path, host)
    event_log = events.EventLog(events_dir, host)
    manifest = manifest_mod.load_or_rebuild(
        manifest_path, snapshot, event_log, config, host.host_id
    )

    from .pipeline import _apply_links
    count = 0
    for name, entry in manifest.skills.items():
        if entry.target_override == []:
            continue
        if not entry.in_hub:
            continue
        if dry_run:
            print(f"would link: {name}")
        else:
            _apply_links(name, entry, config, skills_dir)
            count += 1
    print(f"linked: {count}")
    return 0


def _cmd_adopt(args, config, host, hub_path) -> int:
    """First-time adoption of existing skills."""
    # adopt = scan with --yes skipping per-stage confirmation
    # For v1 skeleton, just call scan
    args.dry_run = getattr(args, "dry_run", False)
    return _cmd_scan(args, config, host, hub_path)


def _cmd_status(args, config, host, hub_path) -> int:
    events_dir = hub_path / "events"
    manifest_path = hub_path / "manifest.json"
    snapshot_path = hub_path / "snapshot.json"

    snapshot = _load_or_init_snapshot(snapshot_path, host)
    event_log = events.EventLog(events_dir, host)
    manifest = manifest_mod.load_or_rebuild(
        manifest_path, snapshot, event_log, config, host.host_id
    )

    result = status_mod.status(manifest, config, hub_path)
    status_mod.print_status(result, json_output=getattr(args, "json", False))
    return 0


def _cmd_invariants(args, config, host, hub_path) -> int:
    events_dir = hub_path / "events"
    manifest_path = hub_path / "manifest.json"
    snapshot_path = hub_path / "snapshot.json"

    snapshot = _load_or_init_snapshot(snapshot_path, host)
    event_log = events.EventLog(events_dir, host)
    manifest = manifest_mod.load_or_rebuild(
        manifest_path, snapshot, event_log, config, host.host_id
    )

    violations = status_mod.invariants(manifest, config, hub_path)
    if not violations:
        print("OK - no invariant violations")
        return 0
    print(f"FOUND {len(violations)} violation(s):")
    for v in violations:
        print(f"  - {v}")
    return 2


def _cmd_detach(args, config, host, hub_path) -> int:
    events_dir = hub_path / "events"
    manifest_path = hub_path / "manifest.json"
    snapshot_path = hub_path / "snapshot.json"
    skills_dir = hub_path / "skills"

    snapshot = _load_or_init_snapshot(snapshot_path, host)
    event_log = events.EventLog(events_dir, host)
    manifest = manifest_mod.load_or_rebuild(
        manifest_path, snapshot, event_log, config, host.host_id
    )

    if args.dry_run:
        print(f"would detach: {args.name}")
        return 0

    lifecycle.detach(args.name, manifest, event_log, config, skills_dir)
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    manifest_mod.save(manifest, manifest_path)
    print(f"detached: {args.name}")
    return 0


def _cmd_attach(args, config, host, hub_path) -> int:
    events_dir = hub_path / "events"
    manifest_path = hub_path / "manifest.json"
    snapshot_path = hub_path / "snapshot.json"
    skills_dir = hub_path / "skills"

    snapshot = _load_or_init_snapshot(snapshot_path, host)
    event_log = events.EventLog(events_dir, host)
    manifest = manifest_mod.load_or_rebuild(
        manifest_path, snapshot, event_log, config, host.host_id
    )

    if args.dry_run:
        print(f"would attach: {args.name}")
        return 0

    lifecycle.attach(args.name, manifest, event_log, config, skills_dir)
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    manifest_mod.save(manifest, manifest_path)
    print(f"attached: {args.name}")
    return 0


def _cmd_uninstall(args, config, host, hub_path) -> int:
    events_dir = hub_path / "events"
    manifest_path = hub_path / "manifest.json"
    snapshot_path = hub_path / "snapshot.json"

    snapshot = _load_or_init_snapshot(snapshot_path, host)
    event_log = events.EventLog(events_dir, host)
    manifest = manifest_mod.load_or_rebuild(
        manifest_path, snapshot, event_log, config, host.host_id
    )

    if args.dry_run:
        print(f"would uninstall: {args.name}")
        return 0

    lifecycle.uninstall(args.name, manifest, event_log, config, hub_path)
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    manifest_mod.save(manifest, manifest_path)
    print(f"uninstalled: {args.name}")
    return 0


def _cmd_forget(args, config, host, hub_path) -> int:
    events_dir = hub_path / "events"
    manifest_path = hub_path / "manifest.json"
    snapshot_path = hub_path / "snapshot.json"
    skills_dir = hub_path / "skills"

    snapshot = _load_or_init_snapshot(snapshot_path, host)
    event_log = events.EventLog(events_dir, host)
    manifest = manifest_mod.load_or_rebuild(
        manifest_path, snapshot, event_log, config, host.host_id
    )

    if args.dry_run:
        print(f"would forget (restore): {args.name}")
        return 0

    lifecycle.forget(args.name, manifest, event_log, config, hub_path, skills_dir)
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    manifest_mod.save(manifest, manifest_path)
    print(f"restored: {args.name}")
    return 0


def _cmd_purge(args, config, host, hub_path) -> int:
    events_dir = hub_path / "events"
    manifest_path = hub_path / "manifest.json"
    snapshot_path = hub_path / "snapshot.json"
    blobs_dir = hub_path / "blobs"

    snapshot = _load_or_init_snapshot(snapshot_path, host)
    event_log = events.EventLog(events_dir, host)
    manifest = manifest_mod.load_or_rebuild(
        manifest_path, snapshot, event_log, config, host.host_id
    )

    lifecycle.purge(args.name, manifest, event_log, config, hub_path, yes=True)
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    manifest_mod.save(manifest, manifest_path)
    print(f"purged: {args.name}")
    return 0


def _cmd_gc(args, config, host, hub_path) -> int:
    events_dir = hub_path / "events"
    manifest_path = hub_path / "manifest.json"
    snapshot_path = hub_path / "snapshot.json"
    blobs_dir = hub_path / "blobs"

    snapshot = _load_or_init_snapshot(snapshot_path, host)
    event_log = events.EventLog(events_dir, host)
    manifest = manifest_mod.load_or_rebuild(
        manifest_path, snapshot, event_log, config, host.host_id
    )

    removed = lifecycle.gc(
        manifest, event_log, hub_path, blobs_dir,
        dry_run=getattr(args, "dry_run", False),
    )
    if getattr(args, "dry_run", False):
        print(f"gc would remove: {removed} blob(s)")
    else:
        print(f"gc removed: {removed} blob(s)")
    return 0


def _cmd_compact(args, config, host, hub_path) -> int:
    """Compact events into a new snapshot. See docs/ARCHITECTURE.md §5.3."""
    events_dir = hub_path / "events"
    snapshot_path = hub_path / "snapshot.json"
    manifest_path = hub_path / "manifest.json"

    event_log = events.EventLog(events_dir, host)
    all_events = event_log.read_all()

    snapshot = _load_or_init_snapshot(snapshot_path, host)
    folded_ids = set(snapshot.get("included_events", []))
    new_folded = folded_ids | {e.id for e in all_events}

    # Rebuild current state
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)

    # Build new snapshot (include conflicts too, so they survive compact)
    import hashlib
    new_snapshot = {
        "version": 1,
        "schema_version": 1,
        "created_by_host": host.host_id,
        "created_at": _now_ns(),
        "skills": {k: v.to_dict() for k, v in manifest.skills.items()},
        "tombstones": {k: v.to_dict() for k, v in manifest.tombstones.items()},
        "conflicts": {k: v.to_dict() for k, v in manifest.conflicts.items()},
        "included_events": sorted(new_folded),
    }
    content = json.dumps(
        {k: v for k, v in new_snapshot.items() if k != "content_hash"},
        sort_keys=True,
    )
    new_snapshot["content_hash"] = f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"

    # Atomic write snapshot
    tmp = snapshot_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(new_snapshot, sort_keys=True, ensure_ascii=False))
    os.rename(tmp, snapshot_path)

    # M5: force rebuild + save manifest after compact
    manifest = manifest_mod.rebuild(new_snapshot, event_log, config, host.host_id)
    manifest_mod.save(manifest, manifest_path)

    print(f"compacted: {len(new_folded)} events folded into snapshot")
    return 0


def _cmd_backup(args, config, host, hub_path) -> int:
    backup_root = Path(config.backup.path).expanduser()
    backup_dir = backup.backup(hub_path, backup_root)
    print(f"backup created: {backup_dir}")
    return 0


def _cmd_rollback(args, config, host, hub_path) -> int:
    backup_root = Path(config.backup.path).expanduser()
    if args.backup_dir:
        backup_dir = Path(args.backup_dir)
    else:
        backup_dir = backup.find_latest_backup(backup_root)
        if backup_dir is None:
            print("no backup found", file=sys.stderr)
            return 1

    print(f"restoring from: {backup_dir}")
    backup.rollback(backup_dir, hub_path)

    # Re-apply links
    print("rebuilding symlinks...")
    events_dir = hub_path / "events"
    manifest_path = hub_path / "manifest.json"
    snapshot_path = hub_path / "snapshot.json"
    snapshot = _load_or_init_snapshot(snapshot_path, host)
    event_log = events.EventLog(events_dir, host)
    manifest = manifest_mod.rebuild(snapshot, event_log, config, host.host_id)
    manifest_mod.save(manifest, manifest_path)

    skills_dir = hub_path / "skills"
    from .pipeline import _apply_links
    for name, entry in manifest.skills.items():
        if entry.in_hub and entry.target_override != []:
            _apply_links(name, entry, config, skills_dir)

    print("rollback complete")
    return 0


def _cmd_install_daemon(args, config, host, hub_path) -> int:
    script_path = Path(__file__).parent.parent / "skillmesh.py"
    label = platform_daemon.install_daemon(
        script_path, interval=config.watch.interval
    )
    print(f"daemon installed: {label}")
    print(f"logs: {platform_daemon.logs_dir()}")
    return 0


def _cmd_uninstall_daemon(args) -> int:
    platform_daemon.uninstall_daemon()
    print("daemon uninstalled")
    return 0


# ============================ helpers ============================

def _load_or_init_snapshot(snapshot_path: Path, host) -> dict:
    """Load snapshot, or create genesis snapshot if not exists."""
    if snapshot_path.exists():
        try:
            data = json.loads(snapshot_path.read_text())
            # Verify content_hash (without mutating data)
            expected = data.get("content_hash")
            if expected:
                payload = {k: v for k, v in data.items() if k != "content_hash"}
                content = json.dumps(payload, sort_keys=True)
                import hashlib
                actual = f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"
                if expected != actual:
                    raise RuntimeError(
                        "snapshot content_hash mismatch - fail-closed. "
                        "Restore from backup: `skillmesh rollback`"
                    )
            return data
        except (json.JSONDecodeError, KeyError):
            raise RuntimeError(
                "snapshot corrupt - fail-closed. "
                "Restore from backup: `skillmesh rollback`"
            )

    # Genesis snapshot
    snapshot = {
        "version": 1,
        "schema_version": 1,
        "created_by_host": host.host_id,
        "created_at": _now_ns(),
        "skills": {},
        "tombstones": {},
        "included_events": [],
    }
    import hashlib
    content = json.dumps(
        {k: v for k, v in snapshot.items() if k != "content_hash"},
        sort_keys=True,
    )
    snapshot["content_hash"] = f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"

    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = snapshot_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot, sort_keys=True, ensure_ascii=False))
    os.rename(tmp, snapshot_path)
    return snapshot


def _now_ns() -> int:
    import time
    return time.time_ns()
