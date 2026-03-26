"""Launchd integration for macOS daemon auto-restart.

Installs/uninstalls a launchd plist that keeps the keshro daemon running.
The plist is a LaunchAgent (per-user, no root required) that:
- Starts on login
- Restarts on crash (KeepAlive = true)
- Logs to ~/.keshro/daemon.log

Linux support (systemd) is deferred to a future PR.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("keshro.daemon")

PLIST_LABEL = "com.keshro.daemon"
LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"


def _plist_path(repo_root: Path) -> Path:
    """Per-repo plist file path."""
    import hashlib
    repo_hash = hashlib.sha256(str(repo_root).encode()).hexdigest()[:12]
    return LAUNCHD_DIR / f"com.keshro.daemon.{repo_hash}.plist"


def _plist_content(repo_root: Path) -> str:
    """Generate launchd plist XML for the daemon."""
    import hashlib
    repo_hash = hashlib.sha256(str(repo_root).encode()).hexdigest()[:12]
    label = f"{PLIST_LABEL}.{repo_hash}"
    log_file = Path.home() / ".keshro" / "daemon.log"
    python = sys.executable

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-m</string>
        <string>keshro_cli.cli</string>
        <string>watch</string>
        <string>--foreground</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{repo_root}</string>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>{log_file}</string>
    <key>StandardErrorPath</key>
    <string>{log_file}</string>
    <key>RunAtLoad</key>
    <false/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
</dict>
</plist>
"""


def is_macos() -> bool:
    return sys.platform == "darwin"


def install_launchd(repo_root: Path) -> bool:
    """Install a launchd plist for auto-restart. Returns True on success."""
    if not is_macos():
        logger.info("Launchd auto-restart is macOS only. Skipping.")
        return False

    LAUNCHD_DIR.mkdir(parents=True, exist_ok=True)
    plist = _plist_path(repo_root)
    plist.write_text(_plist_content(repo_root))

    # Load the plist
    try:
        subprocess.run(["launchctl", "load", str(plist)], check=True, capture_output=True)
        logger.info(f"Installed launchd agent: {plist.name}")
        return True
    except subprocess.CalledProcessError as e:
        logger.warning(f"Failed to load launchd plist: {e.stderr.decode().strip()}")
        return False


def uninstall_launchd(repo_root: Path) -> bool:
    """Uninstall the launchd plist. Returns True on success."""
    if not is_macos():
        return False

    plist = _plist_path(repo_root)
    if not plist.exists():
        return False

    try:
        subprocess.run(["launchctl", "unload", str(plist)], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        pass  # May already be unloaded

    plist.unlink(missing_ok=True)
    logger.info(f"Uninstalled launchd agent: {plist.name}")
    return True


def is_launchd_installed(repo_root: Path) -> bool:
    """Check if a launchd plist exists for this repo."""
    if not is_macos():
        return False
    return _plist_path(repo_root).exists()
