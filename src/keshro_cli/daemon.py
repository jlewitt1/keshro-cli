"""Keshro background daemon — watches git activity and reports to the API.

Phase 0.1: Git watcher + auto-task-tracking with conservative confidence gate.

Architecture:
┌──────────────────────────────────────────────────┐
│  Daemon (asyncio event loop)                      │
│  ├── GitWatcher (watchdog) ─── detects commits    │
│  ├── APIReporter (httpx)  ─── sends task events   │
│  ├── ContextWriter        ─── writes context file │
│  └── PIDManager           ─── lifecycle mgmt      │
│                                                    │
│  Event flow:                                       │
│  git commit → match task → confidence gate         │
│    HIGH → auto-act (task-event to API)             │
│    LOW  → observe (log, surface in status)         │
└──────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .client import get_api_url, get_token, make_async_client
from .config import load_auth
from .hooks import (
    HookSocketServer,
    detect_keshro_command,
    extract_file_paths,
    install_hooks,
    uninstall_hooks,
)
from .watcher import GitWatcher, GitEvent


def _extract_topical_context(
    target_task: dict, all_steps: list[dict], max_entries: int = 5, max_chars: int = 500
) -> list[str]:
    """Find learnings from completed tasks that share tags with the target task."""
    target_tags = {t.lower() for t in (target_task.get("tags") or [])}
    if not target_tags:
        return []

    scored: list[tuple[int, str, str]] = []
    target_id = target_task.get("id")
    for step in all_steps:
        if step.get("status") not in ("done", "completed") or step.get("id") == target_id:
            continue
        notes = (step.get("notes") or "").strip()
        if not notes:
            continue
        step_tags = {t.lower() for t in (step.get("tags") or [])}
        shared = target_tags & step_tags
        if not shared:
            continue

        content = notes[:max_chars] + ("..." if len(notes) > max_chars else "")
        title = step.get("title") or "Untitled"
        shared_str = ", ".join(sorted(shared))
        scored.append((len(shared), title, content, shared_str))

    if not scored:
        return []

    scored.sort(key=lambda x: x[0], reverse=True)
    result = []
    for _score, title, content, shared_str in scored[:max_entries]:
        result.append(f'- From "{title}" (shared: {shared_str}):')
        for line in content.split("\n"):
            result.append(f"  {line}")
    return result

logger = logging.getLogger("keshro.daemon")

KESHRO_DIR = Path.home() / ".keshro"
LOG_FILE = KESHRO_DIR / "daemon.log"
MAX_QUEUE_SIZE = 1000
MAX_OBSERVATIONS = 500
HEARTBEAT_INTERVAL = 30  # seconds


def _repo_hash(repo_root: Path) -> str:
    return hashlib.sha256(str(repo_root).encode()).hexdigest()[:12]


def _pid_file(repo_root: Path) -> Path:
    """Per-repo PID file: ~/.keshro/daemon-{hash}.pid"""
    return KESHRO_DIR / f"daemon-{_repo_hash(repo_root)}.pid"


def _heartbeat_file(repo_root: Path) -> Path:
    return KESHRO_DIR / f"daemon-{_repo_hash(repo_root)}.heartbeat"


def _status_file(repo_root: Path) -> Path:
    return KESHRO_DIR / f"daemon-{_repo_hash(repo_root)}-status.json"


@dataclass
class Observation:
    """A low-confidence signal the daemon observed but didn't act on."""

    timestamp: str
    signal_type: str  # "possible_completion", "possible_learning", "possible_conflict"
    description: str
    task_id: str | None = None


@dataclass
class DaemonState:
    """In-memory daemon state."""

    plan_id: str | None = None
    plan: dict | None = None
    current_task_id: str | None = None
    repo_root: Path | None = None
    running: bool = False
    observe_only: bool = False  # True when no plan found
    event_queue: deque = field(default_factory=lambda: deque(maxlen=MAX_QUEUE_SIZE))
    observations: deque = field(default_factory=lambda: deque(maxlen=MAX_OBSERVATIONS))
    files_in_progress: dict[str, str] = field(default_factory=dict)  # file -> task_id
    files_touched: list[str] = field(default_factory=list)  # files edited in current task
    task_start_time: float = 0.0  # monotonic time when current task started
    hook_completed_tasks: set = field(default_factory=set)  # task IDs completed via hook (dedup)
    started_at: str = ""
    last_heartbeat: str = ""
    events_processed: int = 0
    tasks_completed: int = 0
    hook_events_received: int = 0


def _repo_root() -> Path | None:
    """Find the git repo root from cwd."""
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except Exception:
        pass
    return None


def _socket_path(repo_root: Path) -> Path:
    """Per-repo socket path: ~/.keshro/daemon-{hash}.sock"""
    return KESHRO_DIR / f"daemon-{_repo_hash(repo_root)}.sock"


def _match_task_id_in_commit(message: str, plan: dict | None) -> str | None:
    """Try to match a commit message to a task ID in the plan.

    High confidence: commit message contains an exact task ID.
    """
    if not plan:
        return None
    steps = plan.get("plan_steps") or plan.get("steps") or []
    for step in steps:
        task_id = step.get("id", "")
        if task_id and task_id in message:
            return task_id
    return None


def _detect_keshro_command(event: GitEvent) -> tuple[str, str] | None:
    """Check if a git event corresponds to a keshro CLI command being run.

    Returns (command, task_id) if detected, None otherwise.
    """
    # This is a placeholder for Phase 0.2 hook integration.
    # In Phase 0.1, we only detect commits.
    return None


def _find_current_task(plan: dict | None) -> str | None:
    """Find the first in-progress or next actionable task."""
    if not plan:
        return None
    steps = plan.get("plan_steps") or plan.get("steps") or []
    # First, check for in-progress tasks
    for step in steps:
        if step.get("status") == "in_progress":
            return step.get("id")
    # Then, find next actionable (todo with deps satisfied)
    completed_ids = {s.get("id") for s in steps if s.get("status") == "done"}
    for step in steps:
        if step.get("status") != "todo":
            continue
        deps = step.get("depends_on") or []
        if all(d in completed_ids for d in deps):
            return step.get("id")
    return None


class KeshroDaemon:
    """Background daemon that watches git and reports to the Keshro API."""

    def __init__(
        self,
        plan_id: str | None = None,
        poll_interval: float = 1.0,
        api_url: str | None = None,
        token: str | None = None,
        enable_git_watcher: bool = True,
    ):
        self.state = DaemonState()
        self.state.plan_id = plan_id
        self._poll_interval = poll_interval
        self._api_url = api_url
        self._token = token
        self._enable_git_watcher = enable_git_watcher
        self._watcher: GitWatcher | None = None
        self._hook_server: HookSocketServer | None = None
        self._shutdown_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the daemon: authenticate, resolve plan, begin watching."""
        KESHRO_DIR.mkdir(parents=True, exist_ok=True)
        self._setup_logging()

        # Check auth
        token = get_token(self._token)
        if not token:
            logger.error("Not authenticated. Run: keshro auth login")
            raise SystemExit(
                "Not logged in to Keshro.\n"
                "Run: keshro auth login\n"
            )

        # Find repo root
        repo_root = _repo_root()
        if not repo_root:
            logger.error("Not in a git repository")
            raise SystemExit("Not in a git repository. Run keshro watch from a git repo.")
        self.state.repo_root = repo_root

        # Check for stale daemon
        sock_path = _socket_path(repo_root)
        if self._check_stale_daemon(sock_path):
            logger.info("Cleaned up stale daemon")

        # Check if another daemon is running for this repo
        pid_path = _pid_file(repo_root)
        if pid_path.exists():
            try:
                old_pid = int(pid_path.read_text().strip())
                os.kill(old_pid, 0)  # Check if process exists
                logger.error(f"Daemon already running (PID {old_pid})")
                raise SystemExit(
                    f"Daemon already running for this repo (PID {old_pid}).\n"
                    f"Run: keshro watch stop"
                )
            except PermissionError:
                # Process exists but owned by another user — treat as alive
                logger.error(f"Daemon PID {old_pid} exists but owned by another user")
                raise SystemExit(
                    f"Daemon already running for this repo (PID {old_pid}, different user).\n"
                    f"Run: keshro watch stop"
                )
            except (ProcessLookupError, ValueError):
                # Stale PID file
                pid_path.unlink(missing_ok=True)

        # Write PID
        pid_path.write_text(str(os.getpid()))

        # Resolve plan
        if not self.state.plan_id:
            self.state.plan_id = self._auto_detect_plan_id()

        if self.state.plan_id:
            try:
                async with make_async_client(self._api_url, self._token) as client:
                    resp = await client.get(f"/v1/plans/{self.state.plan_id}")
                    resp.raise_for_status()
                    self.state.plan = resp.json()
                    self.state.current_task_id = _find_current_task(self.state.plan)
                    logger.info(
                        f"Watching plan {self.state.plan_id} "
                        f"(current task: {self.state.current_task_id or 'none'})"
                    )
            except Exception as e:
                logger.warning(f"Could not fetch plan: {e}. Running in observe-only mode.")
                self.state.observe_only = True
        else:
            logger.info("No plan found for this repo. Running in observe-only mode.")
            self.state.observe_only = True

        self.state.running = True
        self.state.started_at = datetime.now(timezone.utc).isoformat()

        # Primary: Hook socket server + Claude Code hooks
        sock_path = _socket_path(repo_root)
        self._hook_server = HookSocketServer(sock_path, self._on_hook_event)
        await self._hook_server.start()
        install_hooks(repo_root, sock_path)

        # Secondary: Git watcher (optional fallback for commits outside Claude Code)
        if self._enable_git_watcher:
            try:
                self._watcher = GitWatcher(repo_root, self._on_git_event)
                self._watcher.start()
            except Exception as e:
                logger.warning(f"Git watcher unavailable: {e}. Hooks are still active.")

        # Track task start time
        if self.state.current_task_id:
            self.state.task_start_time = time.monotonic()

        # Write initial context file
        self._write_context()

        logger.info(f"Daemon started (PID {os.getpid()}, repo: {repo_root.name})")
        self._write_status_file()

        # Register signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: self._shutdown_event.set())

        # Run event loop
        try:
            await self._run()
        finally:
            await self._cleanup()

    async def stop(self) -> None:
        """Signal the daemon to stop."""
        self._shutdown_event.set()

    async def _cleanup(self) -> None:
        """Cleanup on shutdown."""
        self.state.running = False
        if self._watcher:
            self._watcher.stop()
        if self._hook_server:
            await self._hook_server.stop()

        # Flush remaining events
        await self._flush_queue()

        # Uninstall hooks and cleanup files
        repo_root = self.state.repo_root
        if repo_root:
            uninstall_hooks(repo_root)
            _pid_file(repo_root).unlink(missing_ok=True)
            _status_file(repo_root).unlink(missing_ok=True)
        logger.info("Daemon stopped")

    # ------------------------------------------------------------------
    # Event loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        """Main daemon loop: process events + periodic heartbeat."""
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        queue_task = asyncio.create_task(self._process_queue_loop())

        try:
            await self._shutdown_event.wait()
        finally:
            heartbeat_task.cancel()
            queue_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            try:
                await queue_task
            except asyncio.CancelledError:
                pass

    async def _heartbeat_loop(self) -> None:
        """Send heartbeat + re-fetch plan every HEARTBEAT_INTERVAL seconds."""
        while not self._shutdown_event.is_set():
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            await self._send_heartbeat()

    async def _process_queue_loop(self) -> None:
        """Process queued events."""
        while not self._shutdown_event.is_set():
            await asyncio.sleep(0.5)
            await self._flush_queue()

    async def _flush_queue(self) -> None:
        """Send all queued events to the API."""
        while self.state.event_queue:
            event = self.state.event_queue.popleft()
            try:
                await self._send_event(event)
                self.state.events_processed += 1
            except Exception as e:
                # Put it back for retry — warn if queue is full (appendleft drops newest)
                if len(self.state.event_queue) >= MAX_QUEUE_SIZE:
                    logger.warning("Event queue full during retry — newest event will be dropped")
                self.state.event_queue.appendleft(event)
                logger.debug(f"Event send failed, will retry: {e}")
                break

    async def _send_heartbeat(self) -> None:
        """Heartbeat: report liveness + re-fetch plan state."""
        if self.state.observe_only or not self.state.plan_id:
            return
        try:
            async with make_async_client(self._api_url, self._token) as client:
                # Send heartbeat
                await client.post(
                    f"/v1/agent/plans/{self.state.plan_id}/heartbeat",
                    json={"commit_sha": "", "changed_files": []},
                )
                # Re-fetch plan to detect server-side changes
                resp = await client.get(f"/v1/plans/{self.state.plan_id}")
                if resp.status_code == 200:
                    self.state.plan = resp.json()
                    self.state.current_task_id = _find_current_task(self.state.plan)
                    self._write_context()

            self.state.last_heartbeat = datetime.now(timezone.utc).isoformat()
            if self.state.repo_root:
                _heartbeat_file(self.state.repo_root).write_text(self.state.last_heartbeat)
            self._write_status_file()
        except Exception as e:
            logger.debug(f"Heartbeat failed: {e}")

    async def _send_event(self, event: dict) -> None:
        """Send a task event to the API."""
        if self.state.observe_only or not self.state.plan_id:
            return
        try:
            async with make_async_client(self._api_url, self._token) as client:
                resp = await client.post(
                    f"/v1/agent/plans/{self.state.plan_id}/task-event",
                    json=event,
                )
                resp.raise_for_status()
        except Exception as e:
            logger.debug(f"Task event failed: {e}")
            raise

    # ------------------------------------------------------------------
    # Git event handling
    # ------------------------------------------------------------------

    def _on_git_event(self, event: GitEvent) -> None:
        """Handle a git event from the watcher. Runs in watcher thread."""
        if event.event_type == "commit":
            self._handle_commit(event)
        elif event.event_type == "branch_switch":
            self._handle_branch_switch(event)

    def _handle_commit(self, event: GitEvent) -> None:
        """Process a git commit event through the confidence gate."""
        message = event.data.get("message", "")
        task_id = _match_task_id_in_commit(message, self.state.plan)

        if task_id:
            # Skip if hook already completed this task (avoid duplicate events)
            if task_id in self.state.hook_completed_tasks:
                logger.debug(f"Skipping commit for {task_id} — already completed via hook")
                return
            # HIGH confidence: commit message contains task ID
            logger.info(f"High confidence: commit matches task {task_id}")
            self.state.event_queue.append({
                "task_id": task_id,
                "event": "done",
                "note": f"[daemon] Auto-completed: commit matched task ID in message",
            })
            self.state.tasks_completed += 1
            self._write_context()
        else:
            # LOW confidence: commit exists but can't match to task
            if self.state.current_task_id:
                obs = Observation(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    signal_type="possible_completion",
                    description=f"Commit detected while task {self.state.current_task_id} is active: {message[:80]}",
                    task_id=self.state.current_task_id,
                )
                self.state.observations.append(obs)
                logger.info(f"Low confidence: commit during active task {self.state.current_task_id}")

    def _handle_branch_switch(self, event: GitEvent) -> None:
        """Re-resolve plan when branch changes."""
        new_branch = event.data.get("branch", "")
        logger.info(f"Branch switch detected: {new_branch}")
        # Re-detect plan on next heartbeat
        new_plan_id = self._auto_detect_plan_id()
        if new_plan_id and new_plan_id != self.state.plan_id:
            self.state.plan_id = new_plan_id
            self.state.plan = None  # Will be fetched on next heartbeat
            self.state.observe_only = False
            logger.info(f"Switched to plan {new_plan_id}")

    # ------------------------------------------------------------------
    # Hook event handling (Phase 0.2)
    # ------------------------------------------------------------------

    def _on_hook_event(self, event: dict) -> None:
        """Handle a Claude Code hook event received via the Unix socket.

        This is the primary signal source. The daemon sees every tool call:
        - Write/Edit → track which files the agent is touching
        - Bash → detect keshro commands, test runs, build commands
        - All → count activity for quality metrics
        """
        self.state.hook_events_received += 1
        tool_name = event.get("tool_name", "")

        # Track file edits
        files = extract_file_paths(event)
        for f in files:
            self.state.files_touched.append(f)
            if self.state.current_task_id:
                self.state.files_in_progress[f] = self.state.current_task_id

        # Detect keshro CLI commands run by the agent
        keshro_cmd = detect_keshro_command(event)
        if keshro_cmd:
            cmd_type, task_id = keshro_cmd
            if cmd_type == "task_done":
                # HIGH confidence: agent explicitly marked task done
                logger.info(f"High confidence (hook): agent ran keshro task done {task_id}")
                self.state.event_queue.append({
                    "task_id": task_id,
                    "event": "done",
                    "note": f"[daemon] Auto-detected: agent ran keshro task done. Files touched: {len(set(self.state.files_touched))}",
                })
                self.state.hook_completed_tasks.add(task_id)
                self._record_task_metrics(task_id)
                self.state.tasks_completed += 1
                self._write_context()
            elif cmd_type == "task_note":
                logger.info(f"Hook: agent ran keshro task note {task_id}")

        # Track bash commands for quality signals (test runs, lint, build)
        bash_cmd = event.get("tool_input", {}).get("command", "") if tool_name == "Bash" else ""
        if bash_cmd:
            exit_code = event.get("tool_response", {}).get("exit_code")
            self._track_bash_signal(bash_cmd, exit_code)

    def _track_bash_signal(self, command: str, exit_code: int | None) -> None:
        """Track bash commands for execution quality signals."""
        cmd_lower = command.lower()
        # Detect test/lint/build commands
        test_patterns = ["pytest", "npm test", "yarn test", "jest", "vitest", "cargo test", "go test", "rspec", "make test"]
        lint_patterns = ["ruff", "eslint", "prettier", "mypy", "tsc", "cargo clippy", "flake8"]
        build_patterns = ["npm run build", "yarn build", "cargo build", "make build", "next build"]

        for pattern in test_patterns:
            if pattern in cmd_lower:
                status = "pass" if exit_code == 0 else "fail" if exit_code is not None else "unknown"
                logger.info(f"Test run detected: {pattern} → {status}")
                self.state.observations.append(Observation(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    signal_type="test_result",
                    description=f"Test run ({pattern}): {status}",
                    task_id=self.state.current_task_id,
                ))
                return

        for pattern in lint_patterns:
            if pattern in cmd_lower:
                status = "pass" if exit_code == 0 else "fail" if exit_code is not None else "unknown"
                logger.info(f"Lint detected: {pattern} → {status}")
                return

        for pattern in build_patterns:
            if pattern in cmd_lower:
                status = "pass" if exit_code == 0 else "fail" if exit_code is not None else "unknown"
                logger.info(f"Build detected: {pattern} → {status}")
                return

    def _record_task_metrics(self, task_id: str) -> None:
        """Record execution quality metrics for a completed task."""
        elapsed = 0.0
        if self.state.task_start_time > 0:
            elapsed = time.monotonic() - self.state.task_start_time
        files_count = len(set(self.state.files_touched))
        logger.info(
            f"Task {task_id} metrics: {elapsed:.0f}s elapsed, {files_count} files touched"
        )
        # Reset per-task tracking
        self.state.files_touched = []
        self.state.task_start_time = time.monotonic()

    # ------------------------------------------------------------------
    # Plan auto-detection
    # ------------------------------------------------------------------

    def _auto_detect_plan_id(self) -> str | None:
        """Auto-detect plan ID from saved config or .keshro file."""
        # 1. Check saved default
        auth = load_auth()
        plan_id = (auth.get("default_plan_id") or "").strip()
        if plan_id:
            return plan_id

        # 2. Check .keshro config file in repo root
        if self.state.repo_root:
            keshro_config = self.state.repo_root / ".keshro"
            if keshro_config.exists():
                try:
                    config = json.loads(keshro_config.read_text())
                    plan_id = (config.get("plan_id") or "").strip()
                    if plan_id:
                        return plan_id
                except Exception:
                    pass

        return None

    # ------------------------------------------------------------------
    # Context file
    # ------------------------------------------------------------------

    def _write_context(self) -> None:
        """Write .claude/keshro-context.md with current task info."""
        if not self.state.repo_root:
            return
        claude_dir = self.state.repo_root / ".claude"
        claude_dir.mkdir(exist_ok=True)
        context_file = claude_dir / "keshro-context.md"

        lines = ["# Keshro Execution Context", ""]
        lines.append(f"> Auto-generated by `keshro watch`. Do not edit manually.")
        lines.append("")

        if self.state.observe_only:
            lines.append("**Mode:** Observe-only (no plan linked)")
            lines.append("")
            lines.append("Run `keshro config set --plan-id <plan-id>` to link a plan.")
        elif self.state.plan:
            plan = self.state.plan
            title = plan.get("title") or plan.get("source_type", "Plan")
            lines.append(f"**Plan:** {title}")
            lines.append(f"**Plan ID:** {self.state.plan_id}")
            lines.append("")

            # Current task
            if self.state.current_task_id:
                task = self._find_task_by_id(self.state.current_task_id)
                if task:
                    lines.append(f"## Current Task: {task.get('title', 'Untitled')}")
                    lines.append(f"**Task ID:** {self.state.current_task_id}")
                    lines.append("")
                    desc = task.get("description", "")
                    if desc:
                        lines.append(desc[:500])
                        lines.append("")

                    # Acceptance criteria
                    criteria = task.get("acceptance_criteria", "")
                    if criteria:
                        lines.append(f"**Acceptance criteria:** {criteria}")
                        lines.append("")

                    # Related files
                    related = task.get("related_files") or []
                    if related:
                        lines.append("**Related files:** " + ", ".join(related[:10]))
                        lines.append("")

                    # Topical context — learnings from completed tasks with shared tags
                    steps = plan.get("plan_steps") or plan.get("steps") or []
                    topical = _extract_topical_context(task, steps)
                    if topical:
                        lines.append("## Relevant Learnings")
                        lines.append("*From completed tasks that share tags with this task:*")
                        lines.append("")
                        lines.extend(topical)
                        lines.append("")

            # Pending observations
            if self.state.observations:
                recent = list(self.state.observations)[-5:]
                lines.append("## Pending Observations")
                lines.append("*These were detected but not auto-acted on. Review with `keshro watch status`.*")
                lines.append("")
                for obs in recent:
                    lines.append(f"- [{obs.signal_type}] {obs.description}")
                lines.append("")

        try:
            context_file.write_text("\n".join(lines) + "\n")
        except OSError as e:
            logger.warning(f"Failed to write context file: {e}")

    def _find_task_by_id(self, task_id: str) -> dict | None:
        if not self.state.plan:
            return None
        steps = self.state.plan.get("plan_steps") or self.state.plan.get("steps") or []
        for step in steps:
            if step.get("id") == task_id:
                return step
        return None

    # ------------------------------------------------------------------
    # Auto-setup
    # ------------------------------------------------------------------

    def setup_repo(self) -> None:
        """First-run setup: create context file, update .gitignore, install hooks."""
        if not self.state.repo_root:
            return

        # Create .claude dir
        claude_dir = self.state.repo_root / ".claude"
        claude_dir.mkdir(exist_ok=True)

        # Update .gitignore with daemon-generated files
        gitignore = self.state.repo_root / ".gitignore"
        daemon_entries = [".claude/keshro-context.md", ".claude/keshro-hook.sh"]
        if gitignore.exists():
            content = gitignore.read_text()
            missing = [e for e in daemon_entries if e not in content]
            if missing:
                with open(gitignore, "a") as f:
                    if not content.endswith("\n"):
                        f.write("\n")
                    f.write("\n# Keshro daemon (auto-generated)\n")
                    for entry in missing:
                        f.write(f"{entry}\n")
                logger.info(f"Added {missing} to .gitignore")
        else:
            lines = ["# Keshro daemon (auto-generated)"] + daemon_entries
            gitignore.write_text("\n".join(lines) + "\n")
            logger.info("Created .gitignore with keshro entries")

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _check_stale_daemon(self, sock_path: Path) -> bool:
        """Check for and clean up a stale daemon socket."""
        if not sock_path.exists():
            return False
        repo_root = self.state.repo_root
        pid_path = _pid_file(repo_root) if repo_root else None
        if pid_path and pid_path.exists():
            try:
                old_pid = int(pid_path.read_text().strip())
                os.kill(old_pid, 0)
                return False  # Process is alive
            except PermissionError:
                return False  # Process exists but owned by another user
            except (ProcessLookupError, ValueError):
                pass
        # Stale — clean up
        sock_path.unlink(missing_ok=True)
        if pid_path:
            pid_path.unlink(missing_ok=True)
        return True

    def _setup_logging(self) -> None:
        """Configure file logging for the daemon."""
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(LOG_FILE, mode="a")
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

    def get_status(self) -> dict:
        """Return daemon status for `keshro watch status`."""
        plan_title = ""
        current_task_title = ""
        if self.state.plan:
            plan_title = self.state.plan.get("title") or ""
            if self.state.current_task_id:
                task = self._find_task_by_id(self.state.current_task_id)
                if task:
                    current_task_title = task.get("title", "")

        obs_list = list(self.state.observations)
        recent_obs = [
            {"type": o.signal_type, "description": o.description, "timestamp": o.timestamp}
            for o in obs_list[-10:]
        ]

        return {
            "running": self.state.running,
            "pid": os.getpid(),
            "plan_id": self.state.plan_id,
            "plan_title": plan_title,
            "current_task_id": self.state.current_task_id,
            "current_task_title": current_task_title,
            "observe_only": self.state.observe_only,
            "started_at": self.state.started_at,
            "last_heartbeat": self.state.last_heartbeat,
            "events_processed": self.state.events_processed,
            "tasks_completed": self.state.tasks_completed,
            "pending_observations": len(self.state.observations),
            "recent_observations": recent_obs,
            "queue_size": len(self.state.event_queue),
            "hook_events_received": self.state.hook_events_received,
            "files_touched_current_task": len(set(self.state.files_touched)),
            "repo": str(self.state.repo_root or ""),
        }

    def _write_status_file(self) -> None:
        """Persist status to disk so `keshro watch status` can read it."""
        if not self.state.repo_root:
            return
        try:
            _status_file(self.state.repo_root).write_text(json.dumps(self.get_status(), indent=2))
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Module-level helpers for CLI integration
# ---------------------------------------------------------------------------


def is_daemon_running(repo: Path | None = None) -> bool:
    """Check if a daemon is running for the given (or current) repo."""
    repo_root = repo or _repo_root()
    if not repo_root:
        return False
    pid_path = _pid_file(repo_root)
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True  # Process exists but owned by another user
    except (ProcessLookupError, ValueError, FileNotFoundError):
        return False


def stop_daemon(repo: Path | None = None) -> bool:
    """Stop the running daemon by sending SIGTERM."""
    repo_root = repo or _repo_root()
    if not repo_root:
        return False
    pid_path = _pid_file(repo_root)
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        # Wait briefly for cleanup
        for _ in range(20):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pid_path.unlink(missing_ok=True)
                return True
        return False
    except (ProcessLookupError, ValueError):
        pid_path.unlink(missing_ok=True)
        return True


def read_daemon_status(repo: Path | None = None) -> dict | None:
    """Read daemon status from the per-repo status file."""
    repo_root = repo or _repo_root()
    if not repo_root:
        return None
    if not is_daemon_running(repo_root):
        return None
    # Read rich status from per-repo status file
    sf = _status_file(repo_root)
    if sf.exists():
        try:
            return json.loads(sf.read_text())
        except Exception:
            pass
    # Fallback to basic PID info
    try:
        pid = int(_pid_file(repo_root).read_text().strip())
        last_hb = ""
        hf = _heartbeat_file(repo_root)
        if hf.exists():
            last_hb = hf.read_text().strip()
        return {
            "running": True,
            "pid": pid,
            "last_heartbeat": last_hb,
        }
    except Exception:
        return None
