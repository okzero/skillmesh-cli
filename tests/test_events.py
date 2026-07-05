"""Test event log: schema validation, atomic write, corrupt recovery.

Covers F3.2, F6.1, F6.2, F6.3, T11, T14.
"""
import json
from pathlib import Path

import pytest

from skillmesh import events
from skillmesh.events import Event, EventLog, SkillEntry
from skillmesh.host import Host


def _make_host(tmp_path):
    return Host(
        host_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        display_name="test-host",
    )


def _make_entry(name="my-skill", source="work", version="1.0.0"):
    return SkillEntry(
        name=name, source=source, version=version,
        content_hash="sha256:abc", blob_hash="sha256:def",
    )


def test_event_filename_includes_lamport_seq_checksum(tmp_path):
    host = _make_host(tmp_path)
    event_log = EventLog(tmp_path / "events", host)
    event = event_log.write("add", _make_entry())
    fname = event.filename
    parts = fname.replace(".json", "").split("-")
    assert len(parts) == 3
    assert int(parts[0]) == event.lamport
    assert int(parts[1]) == event.seq
    assert len(parts[2]) == 16  # checksum


def test_event_log_writes_to_host_subdir(tmp_path):
    """F6.1: events written to events/<event_dir>/ subdir."""
    host = _make_host(tmp_path)
    event_log = EventLog(tmp_path / "events", host)
    event_log.write("add", _make_entry())

    expected_dir = tmp_path / "events" / host.event_dir
    assert expected_dir.exists()
    files = list(expected_dir.glob("*.json"))
    assert len(files) == 1


def test_event_schema_validation_rejects_invalid(tmp_path):
    """Invalid event raises ValueError."""
    bad_events = [
        {},  # missing all fields
        {"id": "x"},  # missing fields
        {"id": "x", "host": "h", "host_display_name": "n", "ts": 1,
         "seq": 0, "lamport": 1, "op": "add", "skill": {}},  # seq <= 0
        {"id": "x", "host": "h", "host_display_name": "n", "ts": 1,
         "seq": 1, "lamport": 0, "op": "add", "skill": {}},  # lamport <= 0
        {"id": "x", "host": "h", "host_display_name": "n", "ts": 1,
         "seq": 1, "lamport": 1, "op": "invalid_op", "skill": {}},  # bad op
    ]
    for bad in bad_events:
        with pytest.raises((ValueError, KeyError)):
            Event.from_dict(bad)


def test_read_all_returns_sorted(tmp_path):
    """Events sorted by (lamport, host_id, seq, id)."""
    host = _make_host(tmp_path)
    event_log = EventLog(tmp_path / "events", host)
    event_log.write("add", _make_entry("skill-a"))
    event_log.write("add", _make_entry("skill-b"))
    event_log.write("add", _make_entry("skill-c"))

    events_list = event_log.read_all()
    assert len(events_list) == 3
    # Lamport should be increasing
    lamports = [e.lamport for e in events_list]
    assert lamports == sorted(lamports)


def test_corrupt_event_aborts_replay_without_mutation(tmp_path):
    """T14: corrupt truth-source events fail closed and remain in place."""
    host = _make_host(tmp_path)
    event_log = EventLog(tmp_path / "events", host)
    event_log.write("add", _make_entry("good-skill"))

    # Inject a corrupt event file in same host subdir
    host_subdir = tmp_path / "events" / host.event_dir
    corrupt_file = host_subdir / "999-999-badbadbad.json"
    corrupt_file.write_text("{ not valid json")

    with pytest.raises(events.EventCorruptError, match="Replay aborted"):
        event_log.read_all()
    assert corrupt_file.exists()
    assert not (host_subdir / ".corrupt").exists()


def test_event_filename_checksum_tamper_aborts_replay(tmp_path):
    host = _make_host(tmp_path)
    event_log = EventLog(tmp_path / "events", host)
    event = event_log.write("add", _make_entry("original"))
    event_file = event_log.host_dir / event.filename
    payload = json.loads(event_file.read_text())
    payload["skill"]["name"] = "tampered"
    event_file.write_text(json.dumps(payload, sort_keys=True))

    with pytest.raises(events.EventCorruptError, match="checksum mismatch"):
        event_log.read_all()
    assert event_file.exists()


def test_seq_increments_monotonically(tmp_path):
    """T11: seq within a host is unique and increasing."""
    host = _make_host(tmp_path)
    event_log = EventLog(tmp_path / "events", host)
    seqs = []
    for i in range(5):
        event = event_log.write("add", _make_entry(f"skill-{i}"))
        seqs.append(event.seq)
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 5  # all unique


def test_lamport_observed_from_remote(tmp_path):
    """F6.2: observing remote lamport updates local."""
    host = _make_host(tmp_path)
    event_log = EventLog(tmp_path / "events", host)
    # Write local event (lamport=1 after tick)
    e1 = event_log.write("add", _make_entry("a"))
    assert e1.lamport == 1

    # Simulate receiving remote event with lamport=100
    host.observe_lamport(100)  # local = max(1, 100) + 1 = 101
    e2 = event_log.write("add", _make_entry("b"))  # tick: local = 101 + 1 = 102
    assert e2.lamport == 102  # observe then tick


def test_read_all_filtered_excludes_folded(tmp_path):
    """read_all_filtered excludes events in the exclude_ids set."""
    host = _make_host(tmp_path)
    event_log = EventLog(tmp_path / "events", host)
    e1 = event_log.write("add", _make_entry("a"))
    e2 = event_log.write("add", _make_entry("b"))
    e3 = event_log.write("add", _make_entry("c"))

    filtered = event_log.read_all_filtered({e1.id, e3.id})
    assert len(filtered) == 1
    assert filtered[0].id == e2.id


def test_event_filename_checksum_stable(tmp_path):
    """Same event content produces same filename (idempotent)."""
    host = _make_host(tmp_path)
    entry = _make_entry()
    event1 = Event(
        id="fixed-id", host=host.host_id, host_display_name=host.display_name,
        ts=1000, seq=1, lamport=1, op="add", skill=entry,
    )
    event2 = Event(
        id="fixed-id", host=host.host_id, host_display_name=host.display_name,
        ts=1000, seq=1, lamport=1, op="add", skill=entry,
    )
    assert event1.filename == event2.filename


def test_two_hosts_no_seq_conflict(tmp_path):
    """Two different hosts can have same seq (different host_id)."""
    host_a = Host(host_id="aaaa0000-0000-0000-0000-000000000000", display_name="a")
    host_b = Host(host_id="bbbb0000-0000-0000-0000-000000000000", display_name="b")

    log_a = EventLog(tmp_path / "events", host_a)
    log_b = EventLog(tmp_path / "events", host_b)

    ea = log_a.write("add", _make_entry("skill-x"))
    eb = log_b.write("add", _make_entry("skill-y"))

    # Both have seq=1, but different host_id - should not conflict in replay
    assert ea.seq == 1
    assert eb.seq == 1
    assert ea.host != eb.host
