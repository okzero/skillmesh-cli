"""CAS (Content-Addressed Storage) for skill blobs.

Layout:
    blobs/<content_hash>/
        .meta.json
        SKILL.md  (or whatever files the skill contains)

content_hash: merkle root of normalized skill directory (see §3.2).
blob_hash: sha256 of the blob directory itself (for integrity check).

See docs/ARCHITECTURE.md §3.
"""
import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


EXCLUDED_FILES = {".DS_Store", "Thumbs.db", "__pycache__", ".skillmesh.lock",
                  ".skillmesh-owned"}
EXCLUDED_SUFFIXES = (".pyc",)
BLOB_VERSION = "skillmesh-blob-v1"


@dataclass
class BlobMeta:
    content_hash: str
    blob_hash: str
    skill_name: str
    format: str
    version: str
    created_at: int
    created_by_host: str

    def to_dict(self) -> dict:
        return self.__dict__

    @classmethod
    def from_dict(cls, data: dict) -> "BlobMeta":
        return cls(**data)


def compute_content_hash(
    skill_dir: Path,
    skill_name: str,
    format_name: str,
    version: str,
) -> str:
    """Compute canonical merkle hash of a skill directory.

    See docs/ARCHITECTURE.md §3.2 for normalization rules:
    - Files sorted by relative path (POSIX sort)
    - Content UTF-8 NFC normalized, CRLF -> LF
    - mtime/perms/owner excluded
    - Excluded files: .DS_Store, Thumbs.db, __pycache__, *.pyc
    """
    h = hashlib.sha256()
    h.update(f"{BLOB_VERSION}\n".encode("utf-8"))
    h.update(f"name={skill_name}\n".encode("utf-8"))
    h.update(f"format={format_name}\n".encode("utf-8"))
    h.update(f"version={version}\n".encode("utf-8"))
    h.update(b"files:\n")

    files = _list_normalized_files(skill_dir)
    for rel_path in files:
        full = skill_dir / rel_path
        content = _read_normalized(full)
        file_hash = hashlib.sha256(content).hexdigest()
        size = len(content)
        h.update(f"{rel_path}\n".encode("utf-8"))
        h.update(f"  {file_hash}\n".encode("utf-8"))
        h.update(f"  {size}\n".encode("utf-8"))

    return f"sha256:{h.hexdigest()}"


def compute_blob_hash(blob_dir: Path) -> str:
    """Compute hash of the blob directory itself (for integrity check).

    Different from content_hash: this includes .meta.json and uses
    a different concatenation scheme. Used for backup/restore verification.
    """
    h = hashlib.sha256()
    files = sorted(p for p in blob_dir.rglob("*") if p.is_file())
    for f in files:
        rel = f.relative_to(blob_dir).as_posix()
        h.update(f"{rel}\n".encode("utf-8"))
        h.update(f"{f.stat().st_size}\n".encode("utf-8"))
        with open(f, "rb") as fp:
            for chunk in iter(lambda: fp.read(65536), b""):
                h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def write_blob(
    skill_dir: Path,
    skill_name: str,
    format_name: str,
    version: str,
    blobs_dir: Path,
    host_id: str,
) -> str:
    """Atomically write a skill directory to CAS. Returns content_hash.

    Idempotent: if blob already exists, returns immediately.
    """
    content_hash = compute_content_hash(skill_dir, skill_name, format_name, version)
    blob_dir = blobs_dir / content_hash
    if blob_dir.exists():
        return content_hash  # CAS hit

    blobs_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = blobs_dir / f".tmp.{uuid.uuid4().hex}"
    try:
        _copy_normalized(skill_dir, tmp_dir)
        blob_hash = compute_blob_hash(tmp_dir)
        meta = BlobMeta(
            content_hash=content_hash,
            blob_hash=blob_hash,
            skill_name=skill_name,
            format=format_name,
            version=version,
            created_at=_now_ns(),
            created_by_host=host_id,
        )
        (tmp_dir / ".meta.json").write_text(
            json.dumps(meta.to_dict(), sort_keys=True)
        )
        os.rename(tmp_dir, blob_dir)
        return content_hash
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def materialize(content_hash: str, skill_name: str, skills_dir: Path,
                blobs_dir: Path) -> Path:
    """Restore blob to skills/<name>/ (working view).

    Overwrites existing symlink or empty dir at target.
    """
    blob_dir = blobs_dir / content_hash
    if not blob_dir.exists():
        raise FileNotFoundError(f"blob not found: {content_hash}")

    skills_dir.mkdir(parents=True, exist_ok=True)
    target = skills_dir / skill_name
    if target.is_symlink() or target.exists():
        _safe_remove(target)
    _copy_normalized(blob_dir, target, exclude_meta=True)
    # Write ownership marker so future _safe_remove knows it's skillmesh-owned
    (target / ".skillmesh-owned").write_text("skillmesh\n")
    return target


def read_meta(content_hash: str, blobs_dir: Path) -> Optional[BlobMeta]:
    meta_path = blobs_dir / content_hash / ".meta.json"
    if not meta_path.exists():
        return None
    return BlobMeta.from_dict(json.loads(meta_path.read_text()))


def is_orphan_blob(content_hash: str, skills_dir: Path, blobs_dir: Path) -> bool:
    """Check if blob is unreferenced (no skill materialized from it)."""
    # NOTE: this is a heuristic; true orphan check requires scanning manifest.
    # Used by gc to find candidates. See lifecycle.py for full gc logic.
    blob_dir = blobs_dir / content_hash
    if not blob_dir.exists():
        return False
    meta = read_meta(content_hash, blobs_dir)
    if meta is None:
        return True
    target = skills_dir / meta.skill_name
    return not target.exists()


# ============================ helpers ============================

def _list_normalized_files(root: Path) -> List[str]:
    """List files relative to root, sorted, excluding junk."""
    result = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        name = p.name
        if name in EXCLUDED_FILES:
            continue
        if any(name.endswith(suf) for suf in EXCLUDED_SUFFIXES):
            continue
        if name.startswith(".tmp."):
            continue
        result.append(p.relative_to(root).as_posix())
    return sorted(result)


def _read_normalized(path: Path) -> bytes:
    """Read file content with UTF-8 NFC + CRLF->LF normalization."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        import unicodedata
        text = unicodedata.normalize("NFC", text)
        return text.encode("utf-8")
    except UnicodeDecodeError:
        # Binary file - hash as-is
        return raw


def _copy_normalized(src: Path, dst: Path, exclude_meta: bool = False) -> None:
    """Copy tree with normalization. dst must not exist."""
    dst.mkdir(parents=True)
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        name = p.name
        if name in EXCLUDED_FILES:
            continue
        if exclude_meta and name == ".meta.json":
            continue
        if any(name.endswith(suf) for suf in EXCLUDED_SUFFIXES):
            continue
        if name.startswith(".tmp."):
            continue
        rel = p.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = p.read_bytes().decode("utf-8")
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            import unicodedata
            text = unicodedata.normalize("NFC", text)
            target.write_bytes(text.encode("utf-8"))
        except UnicodeDecodeError:
            shutil.copy2(p, target)


def _safe_remove(target: Path) -> None:
    """Remove symlink or skillmesh-owned materialized dir at target.

    Security: only remove if it's a symlink OR an empty dir OR contains
    the .skillmesh-owned marker. Refuse to delete dirs with user content.

    See review B5.
    """
    if target.is_symlink():
        target.unlink()
        return
    if not target.exists():
        return
    if target.is_dir():
        # Check for marker, or allow if empty
        marker = target / ".skillmesh-owned"
        if marker.exists():
            shutil.rmtree(target)
            return
        try:
            # Empty dir - safe to remove
            target.rmdir()
            return
        except OSError:
            # Non-empty without marker - refuse
            raise RuntimeError(
                f"refusing to delete non-empty dir without .skillmesh-owned "
                f"marker at {target} (may contain user content)"
            )
    elif target.exists():
        # Real file (not symlink) - refuse
        raise RuntimeError(
            f"refusing to delete real file at {target} (not a skillmesh symlink)"
        )


def _now_ns() -> int:
    import time
    return time.time_ns()
