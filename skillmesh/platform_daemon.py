"""Platform daemon: launchd, systemd, and Windows Task Scheduler.

Generates:
- macOS: ~/Library/LaunchAgents/com.skillmesh.watch.plist
- Linux: ~/.config/systemd/user/skillmesh.service + skillmesh.timer

Logs go to:
- macOS: ~/Library/Logs/skillmesh/daemon.log + daemon.err
- Linux: ~/.local/state/skillmesh/logs/daemon.log + daemon.err

See docs/PRD.md §9.7, docs/ARCHITECTURE.md §14.
"""
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .platform_support import FileLock, logs_dir as platform_logs_dir, state_dir


class DaemonError(Exception):
    pass


def install_daemon(script_path: Path, interval: int = 60) -> str:
    """Install platform-appropriate daemon. Returns service identifier."""
    system = platform.system()
    if system == "Darwin":
        return _install_launchd(script_path, interval)
    elif system == "Linux":
        return _install_systemd(script_path, interval)
    elif system == "Windows":
        return _install_windows_task(script_path, interval)
    else:
        raise DaemonError(
            f"unsupported platform: {system}. "
            f"Supported platforms: macOS, Linux, Windows."
        )


def uninstall_daemon() -> None:
    system = platform.system()
    if system == "Darwin":
        _uninstall_launchd()
    elif system == "Linux":
        _uninstall_systemd()
    elif system == "Windows":
        _uninstall_windows_task()
    else:
        raise DaemonError(f"unsupported platform: {system}")


def logs_dir() -> Path:
    """Platform-specific logs directory."""
    return platform_logs_dir()


# ============================ launchd ============================

PLIST_LABEL = "com.skillmesh.watch"


def _install_launchd(script_path: Path, interval: int) -> str:
    plist_dir = Path("~/Library/LaunchAgents").expanduser()
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / f"{PLIST_LABEL}.plist"

    log_dir = logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    python_bin = sys.executable or "/usr/bin/python3"
    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{PLIST_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python_bin}</string>
    <string>{script_path}</string>
    <string>daemon-run</string>
  </array>
  <key>StartInterval</key>
  <integer>{interval}</integer>
  <key>StandardOutPath</key>
  <string>{log_dir}/daemon.log</string>
  <key>StandardErrorPath</key>
  <string>{log_dir}/daemon.err</string>
  <key>RunAtLoad</key>
  <true/>
</dict>
</plist>
"""
    plist_path.write_text(plist_content)

    # Unload if already loaded, then load
    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        capture_output=True,
    )
    result = subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True,
    )
    if result.returncode != 0:
        raise DaemonError(
            f"launchctl load failed: {result.stderr.decode()}"
        )
    return PLIST_LABEL


def _uninstall_launchd() -> None:
    plist_dir = Path("~/Library/LaunchAgents").expanduser()
    plist_path = plist_dir / f"{PLIST_LABEL}.plist"
    if plist_path.exists():
        subprocess.run(
            ["launchctl", "unload", str(plist_path)],
            capture_output=True,
        )
        plist_path.unlink()


# ============================ systemd ============================

SERVICE_NAME = "skillmesh"


def _install_systemd(script_path: Path, interval: int) -> str:
    unit_dir = Path("~/.config/systemd/user").expanduser()
    unit_dir.mkdir(parents=True, exist_ok=True)

    log_dir = logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    python_bin = sys.executable or "/usr/bin/python3"

    service_path = unit_dir / f"{SERVICE_NAME}.service"
    service_content = f"""[Unit]
Description=Skillmesh watch daemon

[Service]
Type=oneshot
ExecStart={python_bin} {script_path} daemon-run
StandardOutput=append:{log_dir}/daemon.log
StandardError=append:{log_dir}/daemon.err
"""
    service_path.write_text(service_content)

    timer_path = unit_dir / f"{SERVICE_NAME}.timer"
    timer_content = f"""[Unit]
Description=Run Skillmesh scan periodically

[Timer]
OnBootSec=1min
OnUnitActiveSec={interval}s
Unit={SERVICE_NAME}.service

[Install]
WantedBy=timers.target
"""
    timer_path.write_text(timer_content)

    # Reload and enable
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        check=True,
    )
    subprocess.run(
        ["systemctl", "--user", "enable", f"{SERVICE_NAME}.timer"],
        check=True,
    )
    subprocess.run(
        ["systemctl", "--user", "start", f"{SERVICE_NAME}.timer"],
        check=True,
    )
    return SERVICE_NAME


def _uninstall_systemd() -> None:
    unit_dir = Path("~/.config/systemd/user").expanduser()
    subprocess.run(
        ["systemctl", "--user", "stop", f"{SERVICE_NAME}.timer"],
        capture_output=True,
    )
    subprocess.run(
        ["systemctl", "--user", "disable", f"{SERVICE_NAME}.timer"],
        capture_output=True,
    )
    for ext in (".service", ".timer"):
        p = unit_dir / f"{SERVICE_NAME}{ext}"
        if p.exists():
            p.unlink()
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True,
    )


# ============================ Windows Task Scheduler ============================

WINDOWS_TASK_NAME = "SkillmeshWatch"


def _install_windows_task(script_path: Path, interval: int) -> str:
    if interval < 60 or interval % 60 != 0:
        raise DaemonError(
            "Windows Task Scheduler interval must be a whole number of minutes"
        )
    task_command = subprocess.list2cmdline(
        [sys.executable, str(script_path), "daemon-run"]
    )
    result = subprocess.run(
        ["schtasks.exe", "/Create", "/TN", WINDOWS_TASK_NAME,
         "/TR", task_command, "/SC", "MINUTE", "/MO", str(interval // 60),
         "/RL", "LIMITED", "/F"], capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise DaemonError(f"Task Scheduler create failed: {result.stderr.strip()}")
    subprocess.run(
        ["schtasks.exe", "/Run", "/TN", WINDOWS_TASK_NAME], capture_output=True,
    )
    return WINDOWS_TASK_NAME


def _uninstall_windows_task() -> None:
    subprocess.run(
        ["schtasks.exe", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"],
        capture_output=True,
    )


def acquire_lock() -> FileLock:
    """Return a portable non-blocking daemon lock context manager."""
    return FileLock(state_dir() / "daemon.lock", blocking=False)
