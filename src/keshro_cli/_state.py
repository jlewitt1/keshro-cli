"""Shared state, constants, and low-level utilities used across the CLI."""

import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum

from .config import load_auth


# ---------------------------------------------------------------------------
# ANSI color constants
# ---------------------------------------------------------------------------

RESET = "\033[0m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"


# ---------------------------------------------------------------------------
# Task status enum
# ---------------------------------------------------------------------------


class TaskStatus(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# Global state -- populated by the app callback before any subcommand runs
# ---------------------------------------------------------------------------


@dataclass
class _State:
    api_url: str = ""
    token: str | None = None
    json: bool = False


_state = _State()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enable_line_buffered_output() -> None:
    for _stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(_stream, "reconfigure"):
                _stream.reconfigure(line_buffering=True)
        except Exception:
            pass


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _stdout_is_tty() -> bool:
    return sys.stdout.isatty()


def _sanitize_json_payload(value):
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8").strip()
    if isinstance(value, dict):
        return {
            _sanitize_json_payload(key): _sanitize_json_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_json_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_json_payload(item) for item in value)
    return value


class _Spinner:
    """Context manager that shows an animated spinner with elapsed time."""

    def __init__(self, message: str):
        self._message = message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        if _state.json or not _stdout_is_tty():
            print(self._message)
            return self
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)

    def _spin(self):
        frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        i = 0
        start = time.time()
        while not self._stop.is_set():
            elapsed = int(time.time() - start)
            elapsed_label = f"{elapsed}s"
            terminal_width = shutil.get_terminal_size(fallback=(80, 24)).columns
            available = max(20, terminal_width - len(elapsed_label) - 8)
            message = self._message
            if len(message) > available:
                message = message[: max(1, available - 1)] + "…"
            print(
                f"\r  {CYAN}{frames[i % len(frames)]}{RESET} {message} {DIM}{elapsed_label}{RESET}",
                end="",
                flush=True,
            )
            self._stop.wait(0.1)
            i += 1
        clear_width = max(
            len(self._message) + 20, shutil.get_terminal_size(fallback=(80, 24)).columns
        )
        print("\r" + " " * clear_width + "\r", end="", flush=True)


# ---------------------------------------------------------------------------
# Environment / coding-agent detection
# ---------------------------------------------------------------------------


def _coding_agent_name() -> str | None:
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "Claude Code"
    if (
        os.environ.get("CODEX_SANDBOX")
        or os.environ.get("CODEX_HOME")
        or os.environ.get("CODEX_THREAD_ID")
        or os.environ.get("CODEX_CI")
    ):
        return "Codex"
    if os.environ.get("CURSOR_TRACE_ID") or os.environ.get("CURSOR_SESSION_ID"):
        return "Cursor"
    return None


def _inside_coding_agent() -> bool:
    return _coding_agent_name() is not None


def _default_agent_preference() -> str:
    value = _clean(load_auth().get("default_agent")).lower()
    return value if value in {"auto", "claude", "codex"} else "auto"


def _current_coding_agent_preference() -> str | None:
    agent_name = (_coding_agent_name() or "").lower()
    if "claude" in agent_name:
        return "claude"
    if "codex" in agent_name:
        return "codex"
    return None
