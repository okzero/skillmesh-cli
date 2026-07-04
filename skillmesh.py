#!/usr/bin/env python3
"""Skillmesh CLI entry point.

Usage:
    skillmesh <command> [options]

Commands: init, scan, apply, adopt, status, invariants, detach, attach,
          uninstall, forget, purge, gc, backup, rollback,
          install_daemon, uninstall_daemon, compact

See docs/PRD.md §8.2 for command semantics.
"""
import sys
from skillmesh.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
