"""Tests for the keshro daemon (Phase 0.1)."""

import asyncio
import json
import os
import signal
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from keshro_cli.daemon import (
    DaemonState,
    KeshroDaemon,
    Observation,
    _find_current_task,
    _match_task_id_in_commit,
    _repo_root,
    _socket_path,
    is_daemon_running,
    stop_daemon,
)
from keshro_cli.watcher import GitEvent, GitWatcher


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_repo(tmp_path):
    """Create a minimal git repo structure."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    refs = git_dir / "refs" / "heads"
    refs.mkdir(parents=True)
    return tmp_path


@pytest.fixture
def sample_plan():
    return {
        "id": "plan-1",
        "title": "Test Plan",
        "plan_steps": [
            {"id": "task-1", "title": "Setup auth", "status": "done", "depends_on": []},
            {"id": "task-2", "title": "Add API", "status": "in_progress", "depends_on": ["task-1"]},
            {"id": "task-3", "title": "Write tests", "status": "todo", "depends_on": ["task-2"]},
            {"id": "task-4", "title": "Update docs", "status": "todo", "depends_on": []},
        ],
    }


@pytest.fixture
def daemon(tmp_repo, sample_plan):
    d = KeshroDaemon(plan_id="plan-1")
    d.state.repo_root = tmp_repo
    d.state.plan = sample_plan
    d.state.plan_id = "plan-1"
    return d


# ---------------------------------------------------------------------------
# Unit tests: task matching
# ---------------------------------------------------------------------------


class TestMatchTaskId:
    def test_exact_match_in_commit(self, sample_plan):
        assert _match_task_id_in_commit("task-2: fix the API endpoint", sample_plan) == "task-2"

    def test_no_match(self, sample_plan):
        assert _match_task_id_in_commit("fix some stuff", sample_plan) is None

    def test_no_plan(self):
        assert _match_task_id_in_commit("task-1: something", None) is None

    def test_multiple_task_ids_returns_first(self, sample_plan):
        # If commit mentions multiple task IDs, returns the first match
        result = _match_task_id_in_commit("task-1 and task-2 done", sample_plan)
        assert result == "task-1"


class TestFindCurrentTask:
    def test_finds_in_progress(self, sample_plan):
        assert _find_current_task(sample_plan) == "task-2"

    def test_finds_next_actionable_when_none_in_progress(self):
        plan = {
            "plan_steps": [
                {"id": "task-1", "status": "done", "depends_on": []},
                {"id": "task-2", "status": "todo", "depends_on": ["task-1"]},
            ]
        }
        assert _find_current_task(plan) == "task-2"

    def test_respects_dependencies(self):
        plan = {
            "plan_steps": [
                {"id": "task-1", "status": "todo", "depends_on": []},
                {"id": "task-2", "status": "todo", "depends_on": ["task-1"]},
            ]
        }
        # task-1 has no deps and is todo, so it's next
        assert _find_current_task(plan) == "task-1"

    def test_no_plan(self):
        assert _find_current_task(None) is None

    def test_all_done(self):
        plan = {"plan_steps": [{"id": "task-1", "status": "done", "depends_on": []}]}
        assert _find_current_task(plan) is None


# ---------------------------------------------------------------------------
# Unit tests: socket path
# ---------------------------------------------------------------------------


class TestSocketPath:
    def test_deterministic(self, tmp_path):
        path1 = _socket_path(tmp_path)
        path2 = _socket_path(tmp_path)
        assert path1 == path2

    def test_different_repos(self, tmp_path):
        path1 = _socket_path(tmp_path / "repo1")
        path2 = _socket_path(tmp_path / "repo2")
        assert path1 != path2

    def test_in_keshro_dir(self, tmp_path):
        path = _socket_path(tmp_path)
        assert path.parent == Path.home() / ".keshro"
        assert path.suffix == ".sock"


# ---------------------------------------------------------------------------
# Unit tests: confidence gate
# ---------------------------------------------------------------------------


class TestConfidenceGate:
    def test_high_confidence_commit_auto_acts(self, daemon):
        daemon.state.current_task_id = "task-2"
        event = GitEvent(
            event_type="commit",
            timestamp=time.monotonic(),
            data={"sha": "abc123", "message": "task-2: implement the API"},
        )
        daemon._handle_commit(event)

        # Should have queued an auto-completion event
        assert len(daemon.state.event_queue) == 1
        queued = daemon.state.event_queue[0]
        assert queued["task_id"] == "task-2"
        assert queued["event"] == "done"

    def test_low_confidence_commit_observes_only(self, daemon):
        daemon.state.current_task_id = "task-2"
        event = GitEvent(
            event_type="commit",
            timestamp=time.monotonic(),
            data={"sha": "abc123", "message": "refactor auth module"},
        )
        daemon._handle_commit(event)

        # Should NOT have queued an event
        assert len(daemon.state.event_queue) == 0
        # But should have an observation
        assert len(daemon.state.observations) == 1
        assert daemon.state.observations[0].signal_type == "possible_completion"

    def test_commit_without_active_task_is_silent(self, daemon):
        daemon.state.current_task_id = None
        event = GitEvent(
            event_type="commit",
            timestamp=time.monotonic(),
            data={"sha": "abc123", "message": "random commit"},
        )
        daemon._handle_commit(event)

        assert len(daemon.state.event_queue) == 0
        assert len(daemon.state.observations) == 0


# ---------------------------------------------------------------------------
# Unit tests: context writer
# ---------------------------------------------------------------------------


class TestContextWriter:
    def test_writes_context_file(self, daemon, tmp_repo):
        daemon.state.current_task_id = "task-2"
        daemon._write_context()

        context_file = tmp_repo / ".claude" / "keshro-context.md"
        assert context_file.exists()
        content = context_file.read_text()
        assert "Keshro Execution Context" in content
        assert "task-2" in content
        assert "Add API" in content

    def test_writes_observe_only(self, daemon, tmp_repo):
        daemon.state.observe_only = True
        daemon._write_context()

        content = (tmp_repo / ".claude" / "keshro-context.md").read_text()
        assert "Observe-only" in content

    def test_creates_claude_dir(self, daemon, tmp_repo):
        # Remove .claude if it exists
        claude_dir = tmp_repo / ".claude"
        if claude_dir.exists():
            import shutil
            shutil.rmtree(claude_dir)

        daemon._write_context()
        assert (tmp_repo / ".claude").is_dir()

    def test_includes_observations(self, daemon, tmp_repo):
        daemon.state.observations.append(
            Observation(
                timestamp="2026-03-25T12:00:00Z",
                signal_type="possible_completion",
                description="Commit during active task",
            )
        )
        daemon._write_context()

        content = (tmp_repo / ".claude" / "keshro-context.md").read_text()
        assert "Pending Observations" in content
        assert "possible_completion" in content


# ---------------------------------------------------------------------------
# Unit tests: auto-setup
# ---------------------------------------------------------------------------


class TestAutoSetup:
    def test_creates_gitignore_entry(self, daemon, tmp_repo):
        gitignore = tmp_repo / ".gitignore"
        gitignore.write_text("node_modules/\n")

        daemon.setup_repo()

        content = gitignore.read_text()
        assert "keshro-context.md" in content
        assert "node_modules/" in content  # Preserved

    def test_creates_gitignore_if_missing(self, daemon, tmp_repo):
        daemon.setup_repo()

        gitignore = tmp_repo / ".gitignore"
        assert gitignore.exists()
        assert "keshro-context.md" in gitignore.read_text()

    def test_idempotent(self, daemon, tmp_repo):
        gitignore = tmp_repo / ".gitignore"
        gitignore.write_text(".claude/keshro-context.md\n")

        daemon.setup_repo()

        # Should not duplicate the entry
        content = gitignore.read_text()
        assert content.count("keshro-context.md") == 1


# ---------------------------------------------------------------------------
# Unit tests: plan auto-detection
# ---------------------------------------------------------------------------


class TestPlanAutoDetect:
    def test_from_saved_config(self, daemon, tmp_repo):
        with patch("keshro_cli.daemon.load_auth", return_value={"default_plan_id": "plan-99"}):
            result = daemon._auto_detect_plan_id()
        assert result == "plan-99"

    def test_from_keshro_file(self, daemon, tmp_repo):
        keshro_config = tmp_repo / ".keshro"
        keshro_config.write_text(json.dumps({"plan_id": "plan-from-file"}))

        with patch("keshro_cli.daemon.load_auth", return_value={}):
            result = daemon._auto_detect_plan_id()
        assert result == "plan-from-file"

    def test_no_config_returns_none(self, daemon, tmp_repo):
        with patch("keshro_cli.daemon.load_auth", return_value={}):
            result = daemon._auto_detect_plan_id()
        assert result is None


# ---------------------------------------------------------------------------
# Unit tests: branch switch
# ---------------------------------------------------------------------------


class TestBranchSwitch:
    def test_branch_switch_re_detects_plan(self, daemon):
        with patch.object(daemon, "_auto_detect_plan_id", return_value="plan-new"):
            event = GitEvent(
                event_type="branch_switch",
                timestamp=time.monotonic(),
                data={"branch": "feature/new-thing"},
            )
            daemon._handle_branch_switch(event)

        assert daemon.state.plan_id == "plan-new"
        assert daemon.state.plan is None  # Will be fetched on heartbeat


# ---------------------------------------------------------------------------
# Unit tests: status
# ---------------------------------------------------------------------------


class TestDaemonStatus:
    def test_status_dict(self, daemon):
        daemon.state.running = True
        daemon.state.events_processed = 5
        daemon.state.tasks_completed = 2
        daemon.state.observations.append(
            Observation("2026-01-01", "test", "test obs")
        )

        status = daemon.get_status()
        assert status["running"] is True
        assert status["events_processed"] == 5
        assert status["tasks_completed"] == 2
        assert status["pending_observations"] == 1


# ---------------------------------------------------------------------------
# Unit tests: queue overflow
# ---------------------------------------------------------------------------


class TestQueueOverflow:
    def test_overflow_drops_oldest(self, daemon):
        for i in range(MAX_QUEUE_SIZE + 10):
            daemon.state.event_queue.append({"task_id": f"task-{i}", "event": "note"})

        # deque with maxlen auto-drops oldest
        assert len(daemon.state.event_queue) == MAX_QUEUE_SIZE
        # Oldest should be gone, newest should be present
        assert daemon.state.event_queue[-1]["task_id"] == f"task-{MAX_QUEUE_SIZE + 9}"


MAX_QUEUE_SIZE = 1000  # Match daemon.py
