"""Event log: append-only, per-host subdirectory, Lamport-ordered.

Layout:
    events/<event_dir>/<lamport>-<seq>-<checksum>.json

event_dir = <hostname>-<uuid8>  (hostname for readability, uuid8 for uniqueness)

Each event contains:
    id (uuid), host (host_id), host_display_name (hostname),
    ts (ns), seq, lamport, op, skill, prev_lamport, schema_version

See docs/ARCHITECTURE.md §4.
"""
import hashlib
import json
import os
import time
import uuid as uuid_lib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional


SCHEMA_VERSION = 1
VALID_OPS = {
    "add", "update", "detach", "attach",
    "uninstall", "forget", "purge",
    "gc_prepare", "gc",
}


@dataclass
class SkillEntry:
    name: str
    source: str
    in_hub: bool = True
    format: str = ""
    version: str = ""
    blob_hash: str = ""
    content_hash: str = ""
    target_override: Optional[List[str]] = None

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "source": self.source,
            "in_hub": self.in_hub,
            "format": self.format,
            "version": self.version,
            "blob_hash": self.blob_hash,
            "content_hash": self.content_hash,
        }
        if self.target_override is not None:
            d["target_override"] = self.target_override
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "SkillEntry":
        return cls(
            name=data["name"],
            source=data["source"],
            in_hub=data.get("in_hub", True),
            format=data.get("format", ""),
            version=data.get("version", ""),
            blob_hash=data.get("blob_hash", ""),
            content_hash=data.get("content_hash", ""),
            target_override=data.get("target_override"),
        )


@dataclass
class Event:
    id: str
    host: str                # host_id (UUID)
    host_display_name: str   # hostname (for display only)
    ts: int
    seq: int
    lamport: int
    op: str
    skill: SkillEntry
    prev_lamport: int = 0
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "host": self.host,
            "host_display_name": self.host_display_name,
            "ts": self.ts,
            "seq": self.seq,
            "lamport": self.lamport,
            "op": self.op,
            "skill": self.skill.to_dict(),
            "prev_lamport": self.prev_lamport,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Event":
        _validate_schema(data)
        return cls(
            id=data["id"],
            host=data["host"],
            host_display_name=data["host_display_name"],
            ts=data["ts"],
            seq=data["seq"],
            lamport=data["lamport"],
            op=data["op"],
            skill=SkillEntry.from_dict(data["skill"]),
            prev_lamport=data.get("prev_lamport", 0),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )

    @property
    def filename(self) -> str:
        """Stable filename: <lamport>-<seq>-<checksum>.json"""
        content = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        return f"{self.lamport}-{self.seq}-{checksum}.json"


class EventLog:
    """Per-host event log writer + global reader."""

    def __init__(self, events_dir: Path, host):
        self.events_dir = events_dir
        self.host = host
        self.host_dir = events_dir / host.event_dir

    def write(self, op: str, skill: SkillEntry) -> Event:
        """Atomically append an event. Updates host.seq and host.lamport."""
        if op not in VALID_OPS:
            raise ValueError(f"invalid op: {op} (must be one of {VALID_OPS})")

        prev_lamport = self.host.lamport
        seq = self.host.next_seq()
        lamport = self.host.tick_lamport()

        event = Event(
            id=str(uuid_lib.uuid4()),
            host=self.host.host_id,
            host_display_name=self.host.display_name,
            ts=time.time_ns(),
            seq=seq,
            lamport=lamport,
            op=op,
            skill=skill,
            prev_lamport=prev_lamport,
        )

        self.host_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.host_dir / f".tmp.{event.filename}"
        tmp.write_text(
            json.dumps(event.to_dict(), sort_keys=True, ensure_ascii=False)
        )
        final = self.host_dir / event.filename
        os.rename(tmp, final)  # atomic
        return event

    def read_all(self) -> List[Event]:
        """Read all events across all host subdirs.

        Returns sorted by (lamport, host_id, seq, id) for deterministic replay.
        Corrupt events are moved to .corrupt/ subdir and skipped (with warning).
        """
        events = []
        if not self.events_dir.exists():
            return events

        for host_subdir in self.events_dir.iterdir():
            if not host_subdir.is_dir():
                continue
            if host_subdir.name.startswith("."):
                continue
            for f in sorted(host_subdir.glob("*.json")):
                try:
                    data = json.loads(f.read_text())
                    events.append(Event.from_dict(data))
                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    _quarantine_corrupt(f, host_subdir, e)

        events.sort(key=lambda e: (e.lamport, e.host, e.seq, e.id))
        return events

    def read_all_filtered(self, exclude_ids: set) -> List[Event]:
        """Read events, excluding those whose id is in exclude_ids.

        Used by replay to skip events already folded into snapshot.
        """
        return [e for e in self.read_all() if e.id not in exclude_ids]


def _validate_schema(data: dict) -> None:
    """Validate event schema. Raises ValueError on mismatch."""
    required = {
        "id", "host", "host_display_name", "ts", "seq",
        "lamport", "op", "skill",
    }
    missing = required - set(data.keys())
    if missing:
        raise ValueError(f"event missing fields: {missing}")
    if not isinstance(data["seq"], int) or data["seq"] <= 0:
        raise ValueError(f"invalid seq: {data['seq']}")
    if not isinstance(data["lamport"], int) or data["lamport"] <= 0:
        raise ValueError(f"invalid lamport: {data['lamport']}")
    if data["op"] not in VALID_OPS:
        raise ValueError(f"invalid op: {data['op']}")
    if not isinstance(data["skill"], dict):
        raise ValueError("skill must be a dict")


def _quarantine_corrupt(file: Path, host_subdir: Path, err: Exception) -> None:
    """Move corrupt event file to .corrupt/ subdir, do not delete."""
    import sys
    corrupt_dir = host_subdir / ".corrupt"
    corrupt_dir.mkdir(exist_ok=True)
    target = corrupt_dir / file.name
    try:
        os.rename(file, target)
    except OSError:
        pass  # already moved?
    print(
        f"warn: skip corrupt event {file.name}: {err}",
        file=sys.stderr,
    )
