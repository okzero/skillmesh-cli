# Skillmesh

> Multi-agent, multi-machine skill peer sync. Install once, symlink to all agents; hub dir syncs across machines via any cloud drive (iCloud / Dropbox / Google Drive / Syncthing / Nutstore / Baidu / Quark / Aliyun Drive / git); all nodes are peers, no central writer.

English | [中文](README.md)

## Why Skillmesh

The AI coding agent ecosystem is exploding. A developer commonly uses 2-5 agents simultaneously:

- codex: `~/.codex/skills/` + `SKILL.md`
- claude code: `~/.claude/skills/` + `SKILL.md` / `CLAUDE.md`
- cursor: `.cursor/rules/` + `.cursorrules`
- windsurf / cline / aider / roo / continue...

Each agent has its own skill storage location and filename convention. Once a skill is written, you want it shared across agents — manually copying to each agent dir means N copies, content drift, and painful multi-machine sync.

**Skillmesh solves**:

- **Single machine, multi-agent**: install once, symlink-distribute to every agent that declares it
- **Multi-machine sync**: place hub dir inside any sync backend (iCloud/Dropbox/domestic cloud drives), auto-converges
- **Peer architecture**: every machine reads and writes; no master/slave, no writer/non-writer distinction

## Core Features

| Feature | Description |
| --- | --- |
| **Any sync backend** | iCloud / Dropbox / GDrive / Syncthing / Nutstore / Baidu / Quark / Aliyun Drive / git / manual |
| **Peer-to-peer concurrent writes** | event log dual-write + Lamport clock + CAS blob; two machines editing simultaneously won't conflict |
| **Complete lifecycle state machine** | detach / attach / uninstall / forget / purge / gc, all recoverable |
| **Config-driven multi-agent** | any agent declared in config; zero hardcoding in source |
| **Multi skill formats** | SKILL.md / CLAUDE.md / .cursorrules / .windsurfrules / skill.json / custom |
| **directory / file layout** | directory-level symlink (codex/claude) or file-level symlink (cursor .mdc) |
| **Cross-platform** | macOS (launchd) + Linux (systemd), same codebase |
| **Zero third-party deps** | Python3 std-lib only; `python3 skillmesh.py` runs directly |

## Competitor Comparison

| Capability | qufei1993/skills-hub ★1101 | Duducoco/skillstash ★5 | kamusis/axon-cli ★3 | **skillmesh** |
| --- | :-: | :-: | :-: | :-: |
| Form factor | GUI (Tauri) | CLI (npm) | CLI (Go binary) | CLI (Python std-lib) |
| Windows support | ✅ | ✅ | ✅ | ❌ (v2) |
| Agent count | 46 built-in | auto-detect + custom | 20 built-in + custom | config-declared (any) |
| Distribution | symlink-preferred + copy fallback | copy only | symlink | symlink |
| Multi-machine sync | Git URL import (non-realtime) | git remote + 3-way merge | git sync | **any sync backend** |
| Non-git sync backend (incl. domestic cloud) | ❌ | ❌ | ❌ | ✅ |
| Peer-write concurrency (both machines edit) | ❌ | ⚠️ (via git merge) | ⚠️ (via git merge) | ✅ |
| CAS blob storage | ❌ | ❌ | ❌ | ✅ |
| Complete lifecycle state machine | ❌ | ❌ | ❌ | ✅ |
| Single-script auditable (no build toolchain) | ❌ (cargo) | ❌ (tsc/npm) | ❌ (go build) | ✅ |

Each has its strengths. See `docs/PRD.md` §5.4 for detailed comparison.

## Quick Start

### 1. Install

No install needed, just clone:

```bash
git clone https://github.com/<you>/skillmesh-cli.git
cd skillmesh-cli
```

Requires: Python 3.9+ (bundled with macOS / Linux). TOML config requires 3.11+; 3.9/3.10 use JSON config.

### 2. Initialize

```bash
python3 skillmesh.py init
```

Generates:
- `~/.config/skillmesh/config.toml` (or `.json`, based on Python version)
- `~/.config/skillmesh/host.json` (host UUID)

### 3. Edit config

```bash
$EDITOR ~/.config/skillmesh/config.toml
```

Key fields:

```toml
[hub]
path = "~/Library/Mobile Documents/com~apple~CloudDocs/skillmesh"  # inside iCloud sync dir
sync_backend = "icloud"

[[agents]]
name = "codex"
dir = "~/.codex/skills"
accept_sources = ["work", "personal"]
layout = "directory"

[[agents]]
name = "cursor"
dir = "~/.cursor/rules"
accept_sources = ["personal"]
layout = "file"
target_filename = "{skill}.mdc"
```

Full examples: `config.example.toml` / `config.example.json`.

### 4. Scan and distribute

```bash
python3 skillmesh.py scan
python3 skillmesh.py status
```

### 5. Multi-machine setup

On second machine:
1. Wait for sync backend to sync hub dir
2. `python3 skillmesh.py init` (generates new host UUID)
3. `python3 skillmesh.py scan` (replays events, builds symlinks)

### 6. Install daemon (auto periodic scan)

```bash
python3 skillmesh.py install_daemon
```

Uses launchd on macOS, systemd user unit on Linux.

## Commands

| Command | Purpose |
| --- | --- |
| `init` | Generate config template + host UUID + hub skeleton |
| `scan` | discover → plan → validate → execute + apply |
| `apply` | Rebuild symlinks only (no hub changes) |
| `adopt` | First-time import of existing skills |
| `status` | Show current state |
| `invariants` | Check invariants |
| `detach` / `attach` | Pause / resume a skill's distribution |
| `uninstall` / `forget` | Uninstall (recoverable) / restore |
| `purge --yes` | Permanently delete (requires confirmation) |
| `gc` | Garbage collect unreferenced blobs |
| `compact` | Fold events into snapshot |
| `backup` / `rollback` | Backup / restore |
| `install_daemon` / `uninstall_daemon` | Install / uninstall daemon |

All write commands support `--dry-run`; `purge` requires `--yes`.

## Documentation

- [PRD](docs/PRD.md) - full requirements doc (Chinese)
- [Architecture](docs/ARCHITECTURE.md) - event log + CAS + replay detailed design (Chinese)
- [iCloud deployment](docs/ICLOUD_SYNC.md) - iCloud multi-machine guide (TODO)
- [Domestic cloud drives](docs/CN_CLOUD_DRIVES.md) - Nutstore/Baidu/Quark/Aliyun (TODO)
- [Linux deployment](docs/LINUX_GUIDE.md) - systemd config (TODO)
- [Competitor comparison](docs/COMPETITORS.md) - detailed (TODO)

## Design Principles

1. **Zero hardcoding**: paths, usernames, agent names all config-driven
2. **Peer architecture**: each machine only writes its own `events/<event_dir>/`; no central writer
3. **Single source of truth**: snapshot + events + blobs sync cross-machine; manifest is derived, rebuildable
4. **Fail-closed**: corrupt snapshot/event never silently overwritten; requires manual intervention
5. **Zero deps**: Python3 std-lib only
6. **Auditable**: single script, directly readable/modifiable, no build toolchain

## License

MIT
