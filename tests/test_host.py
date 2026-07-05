"""Test host UUID generation, persistence, and Lamport clock.

Covers T30, T36.
"""
import json
from pathlib import Path

import pytest

from skillmesh import host as host_mod


def test_host_uuid_generated_on_first_run(isolated_env):
    """T30: first run generates host.json with stable UUID."""
    host = host_mod.load_or_create_host()
    assert host.host_id  # non-empty
    assert len(host.host_id) == 36  # UUID v4 format with hyphens
    assert host.display_name  # non-empty


def test_host_uuid_stable_across_runs(isolated_env):
    """T30: second run loads same UUID, doesn't regenerate."""
    host1 = host_mod.load_or_create_host()
    host2 = host_mod.load_or_create_host()
    assert host1.host_id == host2.host_id


def test_host_uuid8_for_event_dir(isolated_env):
    """event_dir uses hostname-uuid8 format."""
    host = host_mod.load_or_create_host()
    uuid8 = host.uuid8
    assert len(uuid8) == 8
    event_dir = host.event_dir
    assert event_dir.startswith(host.display_name + "-")
    assert event_dir.endswith("-" + uuid8)


def test_event_dir_sanitizes_nonportable_hostname():
    host = host_mod.Host(
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        ".CON:work/desk* ",
    )
    assert host.display_name == ".CON:work/desk* "
    assert host.event_dir == "CON-work-desk-aaaaaaaa"
    assert not any(char in host.event_dir for char in '<>:"/\\|?*')


def test_hostname_change_keeps_uuid(isolated_env, monkeypatch):
    """T36: hostname can change but host_id stays stable."""
    host = host_mod.load_or_create_host()
    original_id = host.host_id

    # Simulate hostname change by editing host.json display_name only
    host_file = host_mod._resolve_host_file()
    data = json.loads(host_file.read_text())
    data["host_display_name"] = "new-hostname.local"
    host_file.write_text(json.dumps(data))

    host2 = host_mod.load_or_create_host()
    assert host2.host_id == original_id  # unchanged
    assert host2.display_name == "new-hostname.local"


def test_lamport_tick_increments(isolated_env):
    host = host_mod.load_or_create_host()
    a = host.tick_lamport()
    b = host.tick_lamport()
    assert b == a + 1


def test_lamport_observe_updates_local(isolated_env):
    host = host_mod.load_or_create_host()
    host.tick_lamport()  # local = 1
    host.tick_lamport()  # local = 2
    new = host.observe_lamport(10)  # max(2, 10) + 1 = 11
    assert new == 11


def test_seq_increments_and_persists(isolated_env):
    host = host_mod.load_or_create_host()
    s1 = host.next_seq()
    s2 = host.next_seq()
    assert s2 == s1 + 1
    # Verify persisted
    host2 = host_mod.load_or_create_host()
    assert host2.seq == s2


def test_env_var_overrides_host_id(isolated_env, monkeypatch):
    """SKILLMESH_HOST_ID env var overrides host_id (for testing)."""
    monkeypatch.setenv("SKILLMESH_HOST_ID", "test-override-id")
    host = host_mod.load_or_create_host()
    assert host.host_id == "test-override-id"


def test_corrupt_host_json_does_not_silently_recreate(isolated_env):
    """Corrupt host.json raises error instead of silently generating new UUID
    (which would conflict with existing events on other machines)."""
    host_file = host_mod._resolve_host_file()
    host_file.write_text("{ invalid json")

    with pytest.raises(RuntimeError, match="corrupt"):
        host_mod.load_or_create_host()
