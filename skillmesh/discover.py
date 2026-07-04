"""Discover: scan watch.dirs for skill candidates.

Recognition rules:
- A directory is a skill if it contains any file matching config.formats[].filename
- First format match wins (order = config array order)
- Source derived from config.sources[].prefix matching skill path
- Version extracted from SKILL.md frontmatter / skill.json version field
- Skip: symlinks, exclude patterns, conflict files, placeholder files,
  already-registered skills

See docs/ARCHITECTURE.md §9.1.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .config import Config


@dataclass
class SkillCandidate:
    name: str
    path: Path
    source: str
    format: str
    version: str
    status: str = "new"  # "new", "sync-pending", "conflict", "excluded"

    @property
    def is_actionable(self) -> bool:
        return self.status == "new"


@dataclass
class DiscoverResult:
    candidates: List[SkillCandidate] = field(default_factory=list)
    skipped: List[SkillCandidate] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def discover(config: Config) -> DiscoverResult:
    """Scan watch.dirs and return skill candidates.

    Does NOT modify anything. Pure read.
    """
    result = DiscoverResult()
    seen_names = set()
    seen_paths = set()

    conflict_patterns = [re.compile(p) for p in config.conflicts.patterns]
    placeholder_suffixes = tuple(config.placeholders.suffixes)

    for watch_dir_raw in config.watch.dirs:
        watch_dir = _expand(watch_dir_raw)
        if not watch_dir.exists():
            result.warnings.append(f"watch dir missing: {watch_dir}")
            continue

        for root, dirs, files in _walk_with_exclude(watch_dir, config.watch.exclude):
            root_path = Path(root)

            # Skip symlinks (avoid scanning hub/skills/ itself)
            if root_path.is_symlink():
                continue

            # Check for conflict files in this dir
            if _has_conflict(files, conflict_patterns):
                result.warnings.append(f"conflict file in {root_path}, skipping")
                continue

            # Check for placeholder files (sync not finished)
            if _has_placeholder(files, placeholder_suffixes):
                placeholder_skill = _try_match_skill(
                    root_path, files, config, status="sync-pending"
                )
                if placeholder_skill:
                    result.skipped.append(placeholder_skill)
                continue

            # Try to match a skill format
            candidate = _try_match_skill(root_path, files, config, status="new")
            if candidate is None:
                continue

            # Skip if already registered (same path)
            real_path = str(root_path.resolve())
            if real_path in seen_paths:
                continue
            seen_paths.add(real_path)

            # Dedupe by name (keep first)
            if candidate.name in seen_names:
                result.warnings.append(
                    f"duplicate skill name {candidate.name!r} at {candidate.path}, "
                    f"skipping (already found)"
                )
                continue
            seen_names.add(candidate.name)

            result.candidates.append(candidate)

    return result


def derive_source(skill_path: Path, sources) -> str:
    """Match skill path against sources[].prefix, return label or 'unknown'."""
    skill_str = str(skill_path)
    for src in sources:
        prefix = str(_expand(src.prefix))
        if skill_str.startswith(prefix):
            return src.label
    return "unknown"


def extract_version(skill_dir: Path, format_name: str, format_filename: str) -> str:
    """Extract version from skill content.

    Strategy:
    - SKILL.md / CLAUDE.md / *.md: YAML frontmatter `version:` field
    - skill.json: top-level "version" field
    - Otherwise: empty string (no version)
    """
    target = skill_dir / format_filename
    if not target.exists():
        return ""

    if format_filename.endswith(".json"):
        try:
            import json
            data = json.loads(target.read_text())
            return str(data.get("version", ""))
        except (json.JSONDecodeError, KeyError):
            return ""

    if format_filename.endswith(".md"):
        return _extract_frontmatter_version(target)

    return ""


# ============================ helpers ============================

def _walk_with_exclude(root: Path, exclude_patterns):
    """os.walk wrapper that prunes dirs matching exclude patterns.

    Match logic: a path matches if any exclude pattern is a path SEGMENT
    of the full path (split by '/'). This prevents substring false positives
    (e.g., 'skillmesh' excludes '.../skillmesh/...' but NOT
    '.../my-skillmesh-notes/...').
    """
    import os
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune excluded dirs in-place
        kept = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            full_normalized = full.replace(os.sep, "/")
            segments = full_normalized.split("/")
            if any(ex in segments for ex in exclude_patterns):
                continue
            # Also support patterns starting with / for path-relative matches
            if any(ex.startswith("/") and ex[1:] in segments
                   for ex in exclude_patterns):
                continue
            if os.path.islink(full):
                continue
            kept.append(d)
        dirnames[:] = kept
        yield dirpath, dirnames, filenames


def _try_match_skill(root_path: Path, files: list, config: Config,
                     status: str) -> Optional[SkillCandidate]:
    """Check if root_path contains a declared format file."""
    for fmt in config.formats:
        if fmt.filename in files:
            name = root_path.name
            if not re.match(r"^[a-zA-Z0-9._-]+$", name):
                return None  # invalid name, skip
            source = derive_source(root_path, config.sources)
            version = extract_version(root_path, fmt.name, fmt.filename)
            return SkillCandidate(
                name=name,
                path=root_path,
                source=source,
                format=fmt.name,
                version=version,
                status=status,
            )
    return None


def _has_conflict(files: list, patterns) -> bool:
    for f in files:
        for pat in patterns:
            if pat.search(f):
                return True
    return False


def _has_placeholder(files: list, suffixes: tuple) -> bool:
    for f in files:
        if f.endswith(suffixes):
            return True
    return False


def _extract_frontmatter_version(md_path: Path) -> str:
    """Extract `version:` from YAML frontmatter (--- ... ---)."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end < 0:
        return ""
    frontmatter = text[3:end]
    for line in frontmatter.splitlines():
        line = line.strip()
        if line.lower().startswith("version:"):
            return line.split(":", 1)[1].strip().strip('"\'')
    return ""


def _expand(raw: str) -> Path:
    import os
    return Path(os.path.expandvars(raw)).expanduser()
