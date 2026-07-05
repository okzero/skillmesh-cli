"""Cross-platform paths, locking, and atomic replacement primitives."""
from __future__ import annotations

import os
import platform
import time
from pathlib import Path
from typing import IO, Optional


WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
WINDOWS_INVALID_NAME_CHARS = set('<>:"/\\|?*')


class LockBusy(RuntimeError):
    """Raised when a non-blocking process lock is already held."""


def system_name() -> str:
    return platform.system()


def config_dir() -> Path:
    override = os.environ.get("SKILLMESH_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if system_name() == "Windows":
        return Path(os.environ.get("APPDATA", "~/.config")).expanduser() / "skillmesh"
    return Path("~/.config/skillmesh").expanduser()


def state_dir() -> Path:
    override = os.environ.get("SKILLMESH_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if system_name() == "Windows":
        base = os.environ.get("LOCALAPPDATA", "~/.local/state")
        return Path(base).expanduser() / "skillmesh"
    if system_name() == "Darwin":
        return Path("~/Library/Application Support/skillmesh").expanduser()
    return Path("~/.local/state/skillmesh").expanduser()


def logs_dir() -> Path:
    if system_name() == "Darwin":
        return Path("~/Library/Logs/skillmesh").expanduser()
    return state_dir() / "logs"


def default_backup_dir() -> Path:
    return state_dir() / "backups"


def is_safe_portable_name(name: str) -> bool:
    """Reject names that cannot safely round-trip across supported systems."""
    if (not name or name in {".", ".."} or name.endswith((" ", "."))
            or any(char in WINDOWS_INVALID_NAME_CHARS for char in name)
            or any(ord(char) < 32 for char in name)):
        return False
    return name.split(".", 1)[0].upper() not in WINDOWS_RESERVED_NAMES


def atomic_replace(source: Path, target: Path, retries: int = 5) -> None:
    """Replace target atomically, retrying transient Windows sharing errors."""
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(retries + 1):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            transient = system_name() == "Windows" and (
                isinstance(exc, PermissionError)
                or getattr(exc, "winerror", None) in {5, 32, 33}
            )
            if not transient or attempt == retries:
                raise
            time.sleep(0.05 * (2 ** attempt))


class FileLock:
    """A one-byte advisory lock implemented with fcntl or msvcrt."""

    def __init__(self, path: Path, blocking: bool = True):
        self.path = path
        self.blocking = blocking
        self._stream: Optional[IO[bytes]] = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if system_name() == "Windows":
                import msvcrt
                mode = (msvcrt.LK_LOCK if self.blocking  # type: ignore[attr-defined]
                        else msvcrt.LK_NBLCK)  # type: ignore[attr-defined]
                msvcrt.locking(stream.fileno(), mode, 1)  # type: ignore[attr-defined]
            else:
                import fcntl
                flags = fcntl.LOCK_EX
                if not self.blocking:
                    flags |= fcntl.LOCK_NB
                fcntl.flock(stream.fileno(), flags)
        except (BlockingIOError, OSError) as exc:
            stream.close()
            raise LockBusy(f"lock already held: {self.path}") from exc
        self._stream = stream
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            stream.seek(0)
            if system_name() == "Windows":
                import msvcrt
                msvcrt.locking(  # type: ignore[attr-defined]
                    stream.fileno(), msvcrt.LK_UNLCK, 1  # type: ignore[attr-defined]
                )
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            self._stream = None
