# Contributing to skillmesh

Thanks for your interest in contributing! This doc covers the basics.

## Code of conduct

Be respectful. Be constructive. Assume good intent.

## Project scope

skillmesh is a **multi-agent, multi-machine skill sync tool**. It:

- Distributes skills (symlinks) to AI agents (codex, claude code, cursor, ...)
- Syncs across machines via any cloud drive (iCloud/Dropbox/Syncthing/坚果云/百度网盘/夸克/阿里云盘/git)
- Provides complete lifecycle (detach/attach/uninstall/forget/purge/gc) with peer-write consistency

It does NOT:

- Edit skill content
- Provide a skill marketplace
- Replace dotfiles managers (stow/chezmoi)
- Sync anything other than AI agent skills

If your feature fits this scope, read on. If not, consider opening a discussion first.

## Setup

```bash
git clone git@github.com:okzero/skillmesh-cli.git
cd skillmesh-cli
python3 -m pytest tests/  # should all pass
```

Requirements: Python 3.9+, no third-party deps.

## Code style

- **Python std-lib only** — do not add `pyyaml`, `tomli`, etc. The zero-dependency rule is a hard constraint (see `docs/ARCHITECTURE.md`).
- **Modular package** — each module in `skillmesh/` has a single responsibility. Don't bloat modules past ~500 lines.
- **Type hints** — preferred for public functions.
- **Docstrings** — module-level + non-trivial functions. Reference `docs/ARCHITECTURE.md` sections where relevant.
- **No emojis** in source code or commit messages.

## Before opening a PR

1. **Open an issue first** for any non-trivial change (architecture, new command, behavior change). Discuss before coding.
2. **Tests are mandatory** for any behavior change:
   - Bug fix → regression test that fails before fix, passes after.
   - New feature → test the new behavior + edge cases.
3. **Run the full test suite**:
   ```bash
   python3 -m pytest tests/ -v
   ```
4. **Check invariants** on a real hub if you can:
   ```bash
   python3 skillmesh.py invariants
   ```

## Commit style

- Imperative mood: "add gc cross-machine protection" (not "added" / "adds").
- Reference issues: "fix #42 — race condition in compact".
- One logical change per commit.

## Architecture reference

Before touching core code, read:

- `docs/ARCHITECTURE.md` — event log, CAS, replay, lifecycle, pipeline.
- `skillmesh/manifest.py` — conflict resolution rules (F6.4).
- `skillmesh/lifecycle.py` — gc three-phase cross-machine protection.
- `skillmesh/pipeline.py` — `reconcile_skills()` is the single source of truth for `skills/` working view.

## Areas that need help

- **Real-world testing** on iCloud / Dropbox / 国产网盘 — confirm placeholder/conflict patterns in `config.example.toml` (currently marked "to be verified").
- **Linux systemd testing** on different distros (Ubuntu/Arch/Fedora).
- **Translations** — CLI output is English-only; happy to add i18n if contributors step up.
- **Documentation** — examples, recipes, video walkthroughs.

## Security

Found a security issue? **Do not open a public issue.** Email the maintainer directly via GitHub.

## License

By contributing, you agree your contributions are licensed under the project's MIT License.
