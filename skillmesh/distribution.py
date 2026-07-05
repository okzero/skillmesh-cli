"""Safe cross-platform Agent target distribution."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from .platform_support import FileLock, atomic_replace, state_dir


LINK_MODES = {"auto", "symlink", "junction", "copy"}


class DistributionError(RuntimeError):
    pass


@dataclass
class DistributionState:
    mode: str = "missing"
    exists: bool = False
    correct: bool = False
    local_modified: bool = False


def _registry_path() -> Path:
    return state_dir() / "managed-targets.json"


def _key(path: Path) -> str:
    return os.path.normcase(str(path.absolute()))


def _load_registry() -> Dict[str, dict]:
    path = _registry_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DistributionError(f"managed target registry is corrupt: {exc}") from exc
    if not isinstance(value, dict):
        raise DistributionError("managed target registry must be a JSON object")
    return value


def _save_registry(registry: Dict[str, dict]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(
            json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8"
        )
        atomic_replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _content_hash(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.read_bytes())
        return digest.hexdigest()
    if not path.exists():
        return ""
    for item in sorted(path.rglob("*"), key=lambda value: value.as_posix()):
        relative = item.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if item.is_file():
            digest.update(item.read_bytes())
    return digest.hexdigest()


def is_junction(path: Path) -> bool:
    if platform.system() != "Windows" or path.is_symlink():
        return False
    native = getattr(os.path, "isjunction", None)
    if native is not None:
        return bool(native(path))
    try:
        import ctypes
        attributes = ctypes.windll.kernel32.GetFileAttributesW(  # type: ignore[attr-defined]
            str(path)
        )
        return attributes != -1 and bool(attributes & 0x400)
    except (AttributeError, OSError):
        return False


def inspect(target: Path, source: Optional[Path] = None) -> DistributionState:
    if target.is_symlink():
        correct = source is None or target.resolve() == source.resolve()
        return DistributionState("symlink", True, correct)
    if is_junction(target):
        correct = source is None or target.resolve() == source.resolve()
        return DistributionState("junction", True, correct)
    if target.exists():
        record = _load_registry().get(_key(target))
        if not record or record.get("mode") != "copy":
            return DistributionState("unmanaged", True, False)
        current_hash = _content_hash(target)
        modified = current_hash != record.get("applied_hash")
        source_matches = source is None or record.get("source") == str(source.absolute())
        return DistributionState("copy", True, source_matches and not modified, modified)
    return DistributionState()


def create(source: Path, target: Path, requested_mode: str = "auto") -> str:
    if requested_mode not in LINK_MODES:
        raise DistributionError(f"invalid link mode: {requested_mode}")
    source = source.absolute()
    target = target.absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = inspect(target, source)
    if existing.correct and existing.mode != "copy":
        return existing.mode
    if existing.mode == "copy" and not existing.local_modified:
        # Keep a previously selected fallback stable and refresh it
        # transactionally instead of retrying a privileged symlink each scan.
        if requested_mode in {"auto", "copy"}:
            return _create_copy(source, target)
    if existing.exists:
        remove(target)

    if platform.system() != "Windows":
        if requested_mode in {"junction", "copy"}:
            if requested_mode == "copy":
                return _create_copy(source, target)
            raise DistributionError("junction mode is only supported on Windows")
        os.symlink(source, target, target_is_directory=source.is_dir())
        return "symlink"

    mode = requested_mode
    if mode == "auto":
        mode = "junction" if source.is_dir() else "symlink"
    if mode == "junction":
        if not source.is_dir():
            raise DistributionError("junction mode requires a directory source")
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(target), str(source)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return "junction"
        if requested_mode != "auto":
            raise DistributionError(f"junction creation failed: {result.stderr.strip()}")
        return _create_copy(source, target)
    if mode == "copy":
        return _create_copy(source, target)
    try:
        os.symlink(source, target, target_is_directory=source.is_dir())
        return "symlink"
    except OSError as exc:
        if requested_mode == "auto" and getattr(exc, "winerror", None) in {5, 1314}:
            return _create_copy(source, target)
        raise DistributionError(f"symlink creation failed: {exc}") from exc


def _create_copy(source: Path, target: Path) -> str:
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    backup = target.with_name(f".{target.name}.{uuid.uuid4().hex}.bak")
    with FileLock(_registry_path().with_suffix(".lock")):
        registry = _load_registry()
        try:
            if source.is_dir():
                shutil.copytree(source, temp)
            else:
                shutil.copy2(source, temp)
            if target.exists():
                atomic_replace(target, backup)
            atomic_replace(temp, target)
            registry[_key(target)] = {
                "mode": "copy",
                "source": str(source.absolute()),
                "applied_hash": _content_hash(target),
            }
            _save_registry(registry)
        except Exception:
            _remove_copy_path(temp)
            if backup.exists():
                _remove_copy_path(target)
                atomic_replace(backup, target)
            elif target.exists():
                _remove_copy_path(target)
            raise
        _remove_copy_path(backup)
    return "copy"


def _remove_copy_path(path: Path) -> None:
    """Remove a known transaction artifact without consulting ownership."""
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink()


def remove(target: Path) -> None:
    target = target.absolute()
    if target.is_symlink():
        target.unlink()
        return
    if is_junction(target):
        os.rmdir(target)
        return
    if not target.exists():
        return
    with FileLock(_registry_path().with_suffix(".lock")):
        registry = _load_registry()
        record = registry.get(_key(target))
        if not record or record.get("mode") != "copy":
            raise DistributionError(f"refusing to remove unmanaged target: {target}")
        if _content_hash(target) != record.get("applied_hash"):
            raise DistributionError(f"refusing to remove locally modified copy: {target}")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        del registry[_key(target)]
        _save_registry(registry)
