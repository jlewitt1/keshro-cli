"""Git event watcher using watchdog.

Monitors the .git directory for changes that indicate commits, branch switches,
and other git operations. Debounces rapid events.

Event flow:
  .git/HEAD changes → branch switch detected
  .git/refs/heads/* changes → commit detected (parse git log)
  .git/COMMIT_EDITMSG changes → commit message available
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False

logger = logging.getLogger("keshro.daemon")


@dataclass
class GitEvent:
    """A git event detected by the watcher."""

    event_type: str  # "commit", "branch_switch"
    timestamp: float
    data: dict  # event-specific data


class _GitEventHandler(FileSystemEventHandler if HAS_WATCHDOG else object):
    """Watchdog handler that detects git operations from .git/ changes."""

    def __init__(self, repo_root: Path, callback: Callable[[GitEvent], None]):
        self._repo_root = repo_root
        self._callback = callback
        self._debounce_window = 2.0  # seconds
        self._last_commit_time = 0.0
        self._last_branch = self._current_branch()
        self._lock = threading.Lock()

    def on_modified(self, event: FileSystemEvent) -> None:
        path = Path(event.src_path)
        rel = str(path.relative_to(self._repo_root))

        if rel == ".git/HEAD":
            self._check_branch_switch()
        elif rel.startswith(".git/refs/heads/"):
            self._check_commit()
        elif rel == ".git/COMMIT_EDITMSG":
            # Commit message file written — commit is in progress
            pass

    def on_created(self, event: FileSystemEvent) -> None:
        path = Path(event.src_path)
        rel = str(path.relative_to(self._repo_root))

        if rel.startswith(".git/refs/heads/"):
            self._check_commit()

    def _check_commit(self) -> None:
        """Detect a new commit, with debounce."""
        now = time.monotonic()
        with self._lock:
            if now - self._last_commit_time < self._debounce_window:
                return
            self._last_commit_time = now

        # Get the latest commit message
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%H%n%s"],
                capture_output=True,
                text=True,
                cwd=self._repo_root,
                timeout=5,
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split("\n", 1)
                sha = lines[0] if lines else ""
                message = lines[1] if len(lines) > 1 else ""
                event = GitEvent(
                    event_type="commit",
                    timestamp=now,
                    data={"sha": sha, "message": message},
                )
                logger.debug(f"Commit detected: {sha[:8]} {message[:60]}")
                self._callback(event)
        except Exception as e:
            logger.debug(f"Failed to read commit: {e}")

    def _check_branch_switch(self) -> None:
        """Detect a branch switch from HEAD change."""
        new_branch = self._current_branch()
        if new_branch and new_branch != self._last_branch:
            self._last_branch = new_branch
            event = GitEvent(
                event_type="branch_switch",
                timestamp=time.monotonic(),
                data={"branch": new_branch},
            )
            logger.debug(f"Branch switch: {new_branch}")
            self._callback(event)

    def _current_branch(self) -> str:
        """Read the current branch name."""
        try:
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                capture_output=True,
                text=True,
                cwd=self._repo_root,
                timeout=5,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            return ""


class GitWatcher:
    """Watches a git repo for commits and branch switches.

    Uses watchdog to monitor .git/ directory changes. Debounces rapid events
    (e.g., during rebase) with a 2-second window.
    """

    def __init__(self, repo_root: Path, callback: Callable[[GitEvent], None]):
        if not HAS_WATCHDOG:
            raise ImportError(
                "watchdog is required for keshro watch.\n"
                "Install it: pip install watchdog"
            )
        self._repo_root = repo_root
        self._handler = _GitEventHandler(repo_root, callback)
        self._observer = Observer()
        self._observer.daemon = True

    def start(self) -> None:
        """Start watching .git/ directory."""
        git_dir = self._repo_root / ".git"
        if not git_dir.is_dir():
            raise FileNotFoundError(f"Not a git repository: {self._repo_root}")
        self._observer.schedule(self._handler, str(git_dir), recursive=True)
        self._observer.start()
        logger.info(f"Git watcher started: {self._repo_root}")

    def stop(self) -> None:
        """Stop the watcher."""
        self._observer.stop()
        self._observer.join(timeout=5)
        logger.info("Git watcher stopped")
