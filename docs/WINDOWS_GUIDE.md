# Windows Guide

Skillmesh 0.2.0 supports Windows 10 22H2 and Windows 11 with Python 3.9-3.13.
Place the hub under OneDrive, Dropbox, Syncthing, a git working tree, or any
other directory synchronized by an external tool.

The pre-push suite exercises Windows branches with mocks. A release must also
pass the repository's native `windows-latest` GitHub Actions jobs for every
supported Python version; local non-Windows results are not a substitute for
that native gate.

## Configuration

Run `python skillmesh.py init`. Windows stores configuration under
`%APPDATA%\skillmesh` and local state, logs, managed-copy ownership, and
backups under `%LOCALAPPDATA%\skillmesh`.

Each Agent accepts `link_mode = "auto"`:

- directory layout: NTFS junction (no administrator rights required);
- file layout: symbolic link when Developer Mode or privilege allows it;
- file symlink privilege failure: managed copy refreshed by `scan`/`apply`.

Use `symlink`, `junction`, or `copy` to force a mode. `junction` only accepts a
directory source. Managed copies are removed or replaced only while their hash
matches Skillmesh's local ownership registry; a user edit produces
`LOCAL-MODIFIED` and requires manual resolution.

## Scheduled scanning

`python skillmesh.py install_daemon` installs the per-user Task Scheduler task
`SkillmeshWatch`. The interval must be a whole number of minutes (minimum 60
seconds). Logs are written to
`%LOCALAPPDATA%\skillmesh\logs`. Remove it with `uninstall_daemon`.

## Cloud-drive notes

Skillmesh does not implement cloud synchronization. Wait for the provider to
finish syncing `snapshot.json`, `events/`, and `blobs/`, then run `scan`.
OneDrive Files On-Demand placeholders should be downloaded before adoption.
CAS directory names use `sha256-<digest>` so they are portable across Windows,
macOS, and Linux; the first 0.2 scan migrates legacy `sha256:<digest>` names.
Skillmesh also rejects reserved device names, alternate-data-stream colons,
invalid Windows filename characters, control characters, and trailing dots or
spaces in every synchronized skill path component. Hostname text used in an
event directory is sanitized while the original display name remains in the
event payload.
