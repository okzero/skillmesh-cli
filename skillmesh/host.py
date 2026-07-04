"""Host UUID management.

Each machine has a stable host_id (UUID) generated on first run.
hostname is mutable, host_id is not.

Layout: ~/.config/skillmesh/host.json
    {
      "host_id": "abcd1234-...",
      "host_display_name": "mac-a.local",
      "seq": 42,
      "lamport": 100,
      "created_at": 1720096800123456789
    }

event_dir naming: events/<hostname>-<uuid8>/  (hostname for readability,
uuid8 for uniqueness).

See docs/ARCHITECTURE.md §10.
"""
import json
import os
import socket
import time
import uuid
from pathlib import Path
from typing import Optional


HOST_FILE_ENV = "SKILLMESH_HOST_FILE"
HOST_ID_ENV = "SKILLMESH_HOST_ID"


def _default_host_file() -> Path:
    """Compute default host.json path based on current HOME (test-friendly)."""
    import os
    return Path(os.path.expanduser("~/.config/skillmesh/host.json"))


class Host:
    """Stable per-machine identity."""

    def __init__(
        self,
        host_id: str,
        display_name: str,
        seq: int = 0,
        lamport: int = 0,
        created_at: int = 0,
    ):
        self.host_id = host_id
        self.display_name = display_name
        self.seq = seq
        self.lamport = lamport
        self.created_at = created_at or time.time_ns()

    @property
    def uuid8(self) -> str:
        """First 8 chars of host_id, for event_dir naming."""
        return self.host_id.replace("-", "")[:8]

    @property
    def event_dir(self) -> str:
        """Directory name under events/: <hostname>-<uuid8>."""
        return f"{self.display_name}-{self.uuid8}"

    def to_dict(self) -> dict:
        return {
            "host_id": self.host_id,
            "host_display_name": self.display_name,
            "seq": self.seq,
            "lamport": self.lamport,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Host":
        return cls(
            host_id=data["host_id"],
            display_name=data["host_display_name"],
            seq=data.get("seq", 0),
            lamport=data.get("lamport", 0),
            created_at=data.get("created_at", 0),
        )

    def next_seq(self) -> int:
        """Atomically increment and persist seq."""
        self.seq += 1
        self._persist()
        return self.seq

    def tick_lamport(self) -> int:
        """Local Lamport tick."""
        self.lamport += 1
        self._persist()
        return self.lamport

    def observe_lamport(self, remote: int) -> int:
        """Observe remote lamport, update local."""
        self.lamport = max(self.lamport, remote) + 1
        self._persist()
        return self.lamport

    def _persist(self, path: Optional[Path] = None) -> None:
        target = path or _resolve_host_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), sort_keys=True))
        os.rename(tmp, target)


def _resolve_host_file() -> Path:
    if HOST_FILE_ENV in os.environ:
        return Path(os.environ[HOST_FILE_ENV])
    return _default_host_file()


def load_or_create_host() -> Host:
    """Load existing host.json or create new on first run.

    SKILLMESH_HOST_ID env var overrides host_id (for testing / migration).
    """
    if env_id := os.environ.get(HOST_ID_ENV):
        return Host(
            host_id=env_id,
            display_name=socket.gethostname(),
        )

    path = _resolve_host_file()
    if path.exists():
        try:
            return Host.from_dict(json.loads(path.read_text()))
        except (json.JSONDecodeError, KeyError):
            # Corrupt host.json - do NOT silently recreate (would conflict
            # with existing events/<event_dir>/ on other machines).
            raise RuntimeError(
                f"host.json corrupt at {path}. "
                f"Resolve manually or set {HOST_ID_ENV} env var."
            )

    host = Host(
        host_id=str(uuid.uuid4()),
        display_name=socket.gethostname(),
        created_at=time.time_ns(),
    )
    host._persist(path)
    return host
