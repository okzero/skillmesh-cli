"""Windows 0.2.0 platform, distribution, locking, and daemon tests."""
import io
import json
import os
import platform
import subprocess
import tarfile
from pathlib import Path

import pytest

from skillmesh import backup, cas, distribution, platform_daemon, platform_support
from skillmesh.config import Agent
from skillmesh.host import Host


def test_windows_platform_paths(tmp_path, monkeypatch):
    monkeypatch.delenv("SKILLMESH_CONFIG_DIR", raising=False)
    monkeypatch.delenv("SKILLMESH_STATE_DIR", raising=False)
    monkeypatch.setattr(platform_support, "system_name", lambda: "Windows")
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert platform_support.config_dir() == tmp_path / "roaming" / "skillmesh"
    assert platform_support.state_dir() == tmp_path / "local" / "skillmesh"
    assert platform_support.logs_dir() == tmp_path / "local" / "skillmesh" / "logs"


def test_agent_link_mode_validation():
    Agent("x", "~/x", ["work"], link_mode="auto").validate()
    with pytest.raises(Exception, match="link_mode"):
        Agent("x", "~/x", ["work"], link_mode="invalid").validate()


@pytest.mark.parametrize(
    "name",
    [
        "CON", "nul.txt", "COM1", "file:stream", "bad?.md",
        "bad\\name", "trailing.", "trailing ", ".", "..", "bad\x01name",
    ],
)
def test_windows_reserved_skill_names_rejected(name):
    assert not platform_support.is_safe_portable_name(name)


def test_windows_portable_hidden_and_normal_names_allowed():
    for name in ("SKILL.md", ".cursorrules", "my-skill_1.0"):
        assert platform_support.is_safe_portable_name(name)


def test_managed_copy_refresh_and_modified_protection(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLMESH_STATE_DIR", str(tmp_path / "state"))
    source = tmp_path / "source.txt"
    target = tmp_path / "agent" / "skill.txt"
    source.write_text("v1")
    assert distribution.create(source, target, "copy") == "copy"
    assert distribution.inspect(target, source).correct

    source.write_text("v2")
    assert distribution.create(source, target, "copy") == "copy"
    assert target.read_text() == "v2"

    target.write_text("user edit")
    state = distribution.inspect(target, source)
    assert state.local_modified
    with pytest.raises(distribution.DistributionError, match="locally modified"):
        distribution.remove(target)


def test_managed_copy_refresh_failure_restores_old_content(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLMESH_STATE_DIR", str(tmp_path / "state"))
    source = tmp_path / "source.txt"
    target = tmp_path / "target.txt"
    source.write_text("old")
    distribution.create(source, target, "copy")
    source.write_text("new")

    real_replace = distribution.atomic_replace
    calls = 0

    def fail_new_target(source_path, target_path, retries=5):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("simulated sharing violation")
        return real_replace(source_path, target_path, retries)

    monkeypatch.setattr(distribution, "atomic_replace", fail_new_target)
    with pytest.raises(PermissionError):
        distribution.create(source, target, "copy")
    assert target.read_text() == "old"


def test_managed_copy_registry_failure_rolls_back_target(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLMESH_STATE_DIR", str(tmp_path / "state"))
    source = tmp_path / "source.txt"
    target = tmp_path / "target.txt"
    source.write_text("old")
    distribution.create(source, target, "copy")
    source.write_text("new")

    def fail_registry(registry):
        raise PermissionError("simulated registry write failure")

    monkeypatch.setattr(distribution, "_save_registry", fail_registry)
    with pytest.raises(PermissionError, match="registry"):
        distribution.create(source, target, "copy")
    assert target.read_text() == "old"
    assert not list(tmp_path.glob(".*.bak"))
    assert not list(tmp_path.glob(".*.tmp"))


def test_windows_auto_file_symlink_privilege_falls_back_to_copy(
    tmp_path, monkeypatch
):
    native_system = platform_support.system_name()
    monkeypatch.setenv("SKILLMESH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(distribution.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        platform_support, "system_name", lambda: native_system
    )
    source = tmp_path / "source.txt"
    target = tmp_path / "target.txt"
    source.write_text("content")

    def denied(*args, **kwargs):
        error = OSError("privilege not held")
        error.winerror = 1314
        raise error

    monkeypatch.setattr(distribution.os, "symlink", denied)
    assert distribution.create(source, target, "auto") == "copy"
    assert target.read_text() == "content"


def test_windows_auto_directory_uses_junction(tmp_path, monkeypatch):
    monkeypatch.setattr(distribution.platform, "system", lambda: "Windows")
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "target"
    calls = []

    class Result:
        returncode = 0
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append(command)
        return Result()

    monkeypatch.setattr(distribution.subprocess, "run", fake_run)
    assert distribution.create(source, target, "auto") == "junction"
    assert calls[0][:5] == ["cmd.exe", "/d", "/c", "mklink", "/J"]


def test_broken_windows_junction_is_still_detected(tmp_path, monkeypatch):
    target = tmp_path / "broken-junction"
    monkeypatch.setattr(distribution.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        distribution.os.path, "isjunction", lambda path: path == target,
        raising=False,
    )
    assert not target.exists()
    assert distribution.is_junction(target)


def test_windows_task_minimum_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_daemon.platform, "system", lambda: "Windows")
    with pytest.raises(platform_daemon.DaemonError, match="whole number"):
        platform_daemon.install_daemon(tmp_path / "skillmesh.py", 30)


def test_windows_task_scheduler_install_command(tmp_path, monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append(command)
        return Result()

    monkeypatch.setattr(platform_daemon.platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform_daemon.subprocess, "run", fake_run)
    assert platform_daemon.install_daemon(tmp_path / "skillmesh.py", 120) == (
        "SkillmeshWatch"
    )
    assert calls[0][0:4] == ["schtasks.exe", "/Create", "/TN", "SkillmeshWatch"]
    assert "/SC" in calls[0] and "MINUTE" in calls[0]
    assert "/MO" in calls[0] and "2" in calls[0]


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows native only")
def test_native_windows_junction_roundtrip(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(target), str(source)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert distribution.is_junction(target)
    distribution.remove(target)
    assert not target.exists()


def test_host_clock_reservation_is_atomic_across_instances(tmp_path, monkeypatch):
    host_file = tmp_path / "host.json"
    monkeypatch.setenv("SKILLMESH_HOST_FILE", str(host_file))
    first = Host("host-id", "windows", created_at=123)
    first._persist(host_file)
    second = Host("host-id", "windows", created_at=123)
    assert first.reserve_event_clock()[:2] == (1, 1)
    assert second.reserve_event_clock()[:2] == (2, 2)


def test_windows_atomic_replace_retries_sharing_violation(tmp_path, monkeypatch):
    source = tmp_path / "new"
    target = tmp_path / "target"
    source.write_text("new")
    calls = 0
    real_replace = platform_support.os.replace

    def flaky_replace(src, dst):
        nonlocal calls
        calls += 1
        if calls < 3:
            error = PermissionError("busy")
            error.winerror = 32
            raise error
        return real_replace(src, dst)

    monkeypatch.setattr(platform_support, "system_name", lambda: "Windows")
    monkeypatch.setattr(platform_support.os, "replace", flaky_replace)
    monkeypatch.setattr(platform_support.time, "sleep", lambda delay: None)
    platform_support.atomic_replace(source, target)
    assert calls == 3
    assert target.read_text() == "new"


def test_cas_uses_portable_directory_and_migrates_legacy(tmp_path):
    blobs = tmp_path / "blobs"
    assert cas.blob_path(blobs, "sha256:abc", legacy_fallback=False) == (
        blobs / "sha256-abc"
    )
    if os.name == "nt":
        pytest.skip("NTFS cannot create the pre-0.2 colon directory name")
    legacy = blobs / "sha256:abc"
    legacy.mkdir(parents=True)
    assert cas.migrate_legacy_blob_names(blobs) == 1
    assert (blobs / "sha256-abc").is_dir()
    assert cas.blob_path(blobs, "sha256:abc") == blobs / "sha256-abc"


def test_windows_tar_ads_path_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "system_name", lambda: "Windows")
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("safe/file.txt:stream")
        payload = b"bad"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    with tarfile.open(archive) as tar:
        with pytest.raises(backup.BackupError, match="cross-platform"):
            backup._safe_extract(tar, tmp_path / "out")
