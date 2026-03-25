"""Integration tests for the keshro daemon.

Tests the full daemon lifecycle: start → receive events → API calls → context writes → stop.
Uses a fake async HTTP client to capture API calls without a real backend.
"""

import asyncio
import json
import os
import signal
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from keshro_cli.daemon import (
    HEARTBEAT_INTERVAL,
    KeshroDaemon,
    DaemonState,
    _find_current_task,
    _heartbeat_file,
    _pid_file,
    _status_file,
    is_daemon_running,
    read_daemon_status,
    stop_daemon,
)
from keshro_cli.watcher import GitEvent


# ---------------------------------------------------------------------------
# Fake async HTTP client
# ---------------------------------------------------------------------------

SAMPLE_PLAN = {
    "id": "plan-1",
    "title": "Test Plan",
    "plan_steps": [
        {"id": "task-1", "title": "Setup auth", "status": "done", "depends_on": []},
        {
            "id": "task-2",
            "title": "Add API",
            "status": "in_progress",
            "depends_on": ["task-1"],
            "description": "Build the REST API endpoints",
            "acceptance_criteria": "All endpoints return 200",
            "related_files": ["api.py", "routes.py"],
        },
        {"id": "task-3", "title": "Write tests", "status": "todo", "depends_on": ["task-2"]},
    ],
}

UPDATED_PLAN = {
    **SAMPLE_PLAN,
    "plan_steps": [
        {"id": "task-1", "title": "Setup auth", "status": "done", "depends_on": []},
        {"id": "task-2", "title": "Add API", "status": "done", "depends_on": ["task-1"]},
        {"id": "task-3", "title": "Write tests", "status": "in_progress", "depends_on": ["task-2"]},
    ],
}


class _FakeAsyncResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Async HTTP client that records calls and returns canned responses."""

    def __init__(self, plan=None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self._plan = plan or SAMPLE_PLAN

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, path, **kwargs):
        self.calls.append(("GET", path, kwargs))
        if "/v1/plans/" in path and "task-event" not in path and "heartbeat" not in path:
            return _FakeAsyncResponse(self._plan)
        return _FakeAsyncResponse({})

    async def post(self, path, **kwargs):
        self.calls.append(("POST", path, kwargs))
        if "heartbeat" in path:
            return _FakeAsyncResponse({"status": "ok"})
        if "task-event" in path:
            return _FakeAsyncResponse({"status": "ok"})
        return _FakeAsyncResponse({})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_repo(tmp_path):
    """Create a minimal git repo."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir / "refs" / "heads").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def daemon(tmp_repo):
    """Create a daemon with mocked API client."""
    d = KeshroDaemon(plan_id="plan-1", api_url="http://localhost:8000", token="test-token")
    d.state.repo_root = tmp_repo
    d.state.plan = SAMPLE_PLAN.copy()
    d.state.plan_id = "plan-1"
    d.state.current_task_id = "task-2"
    d.state.running = True
    d.state.started_at = "2026-03-25T12:00:00Z"
    return d


# ---------------------------------------------------------------------------
# Integration: full event lifecycle
# ---------------------------------------------------------------------------


class TestEventLifecycle:
    """Test: git commit → match task → queue event → send to API → update context."""

    def test_high_confidence_commit_queues_and_flushes(self, daemon, tmp_repo):
        """A commit with task ID should queue a done event and flush to API."""
        fake_client = _FakeAsyncClient()

        # Simulate a high-confidence commit
        event = GitEvent(
            event_type="commit",
            timestamp=time.monotonic(),
            data={"sha": "abc123", "message": "task-2: implement REST endpoints"},
        )
        daemon._handle_commit(event)

        # Should have queued one event
        assert len(daemon.state.event_queue) == 1
        queued = daemon.state.event_queue[0]
        assert queued["task_id"] == "task-2"
        assert queued["event"] == "done"

        # Flush the queue with mocked client
        async def _flush():
            with patch("keshro_cli.daemon.make_async_client", return_value=fake_client):
                await daemon._flush_queue()

        asyncio.run(_flush())

        # Queue should be empty now
        assert len(daemon.state.event_queue) == 0
        assert daemon.state.events_processed == 1

        # API should have received the task-event POST
        post_calls = [c for c in fake_client.calls if c[0] == "POST"]
        assert len(post_calls) == 1
        assert "task-event" in post_calls[0][1]
        body = post_calls[0][2]["json"]
        assert body["task_id"] == "task-2"
        assert body["event"] == "done"

    def test_low_confidence_commit_does_not_send_api_call(self, daemon):
        """A commit without task ID should observe but not send to API."""
        fake_client = _FakeAsyncClient()

        event = GitEvent(
            event_type="commit",
            timestamp=time.monotonic(),
            data={"sha": "def456", "message": "refactor auth module"},
        )
        daemon._handle_commit(event)

        # No queued events
        assert len(daemon.state.event_queue) == 0

        # Flush should be a no-op
        async def _flush():
            with patch("keshro_cli.daemon.make_async_client", return_value=fake_client):
                await daemon._flush_queue()

        asyncio.run(_flush())

        # No API calls
        assert len(fake_client.calls) == 0

        # But we have an observation
        assert len(daemon.state.observations) == 1
        assert daemon.state.observations[0].signal_type == "possible_completion"


class TestHeartbeatIntegration:
    """Test: heartbeat → re-fetch plan → update current task → write context."""

    def test_heartbeat_refreshes_plan_state(self, daemon, tmp_repo):
        """Heartbeat should fetch the latest plan and update context."""
        fake_client = _FakeAsyncClient(plan=UPDATED_PLAN)

        async def _heartbeat():
            with patch("keshro_cli.daemon.make_async_client", return_value=fake_client):
                await daemon._send_heartbeat()

        asyncio.run(_heartbeat())

        # Plan should be updated
        assert daemon.state.plan == UPDATED_PLAN
        # Current task should now be task-3 (task-2 is done in UPDATED_PLAN)
        assert daemon.state.current_task_id == "task-3"
        # Heartbeat timestamp should be set
        assert daemon.state.last_heartbeat != ""
        # Context file should be written
        context = (tmp_repo / ".claude" / "keshro-context.md").read_text()
        assert "task-3" in context
        assert "Write tests" in context

    def test_heartbeat_writes_status_file(self, daemon, tmp_repo):
        """Heartbeat should persist status to disk."""
        fake_client = _FakeAsyncClient()

        async def _heartbeat():
            with patch("keshro_cli.daemon.make_async_client", return_value=fake_client):
                await daemon._send_heartbeat()

        asyncio.run(_heartbeat())

        assert _status_file(tmp_repo).exists()
        status = json.loads(_status_file(tmp_repo).read_text())
        assert status["plan_id"] == "plan-1"
        assert status["current_task_id"] == "task-2"
        assert status["running"] is True

        # Cleanup
        _status_file(tmp_repo).unlink(missing_ok=True)

    def test_heartbeat_skips_in_observe_only_mode(self, daemon):
        """Heartbeat should be a no-op when no plan is linked."""
        daemon.state.observe_only = True
        fake_client = _FakeAsyncClient()

        async def _heartbeat():
            with patch("keshro_cli.daemon.make_async_client", return_value=fake_client):
                await daemon._send_heartbeat()

        asyncio.run(_heartbeat())

        # No API calls
        assert len(fake_client.calls) == 0

    def test_heartbeat_survives_network_failure(self, daemon):
        """Heartbeat should silently handle API errors."""
        async def _failing_heartbeat():
            with patch("keshro_cli.daemon.make_async_client", side_effect=Exception("network down")):
                await daemon._send_heartbeat()

        # Should not raise
        asyncio.run(_failing_heartbeat())
        assert daemon.state.last_heartbeat == ""  # Not updated on failure


class TestQueueResilience:
    """Test: queue retry on failure, event ordering."""

    def test_failed_event_stays_in_queue(self, daemon):
        """If an API call fails, the event should stay in the queue for retry."""
        daemon.state.event_queue.append({"task_id": "task-2", "event": "done"})

        class _FailingClient(_FakeAsyncClient):
            async def post(self, path, **kwargs):
                raise Exception("server error")

        failing_client = _FailingClient()

        async def _flush():
            with patch("keshro_cli.daemon.make_async_client", return_value=failing_client):
                await daemon._flush_queue()

        asyncio.run(_flush())

        # Event should still be in queue
        assert len(daemon.state.event_queue) == 1
        assert daemon.state.events_processed == 0

    def test_multiple_events_flush_in_order(self, daemon):
        """Events should be sent to API in FIFO order."""
        daemon.state.event_queue.append({"task_id": "task-1", "event": "note", "note": "first"})
        daemon.state.event_queue.append({"task_id": "task-2", "event": "done"})

        fake_client = _FakeAsyncClient()

        async def _flush():
            with patch("keshro_cli.daemon.make_async_client", return_value=fake_client):
                await daemon._flush_queue()

        asyncio.run(_flush())

        assert len(daemon.state.event_queue) == 0
        assert daemon.state.events_processed == 2

        # Verify order
        post_calls = [c for c in fake_client.calls if c[0] == "POST"]
        assert post_calls[0][2]["json"]["note"] == "first"
        assert post_calls[1][2]["json"]["task_id"] == "task-2"


class TestContextFileIntegration:
    """Test: context file content reflects daemon state accurately."""

    def test_context_includes_task_details(self, daemon, tmp_repo):
        daemon._write_context()
        content = (tmp_repo / ".claude" / "keshro-context.md").read_text()

        assert "Test Plan" in content
        assert "plan-1" in content
        assert "Add API" in content
        assert "task-2" in content
        assert "Build the REST API endpoints" in content
        assert "All endpoints return 200" in content
        assert "api.py" in content

    def test_context_updates_after_heartbeat(self, daemon, tmp_repo):
        """Context should reflect the latest plan state after heartbeat."""
        daemon._write_context()
        content_before = (tmp_repo / ".claude" / "keshro-context.md").read_text()
        assert "task-2" in content_before

        # Simulate heartbeat that updates plan
        daemon.state.plan = UPDATED_PLAN
        daemon.state.current_task_id = _find_current_task(UPDATED_PLAN)
        daemon._write_context()

        content_after = (tmp_repo / ".claude" / "keshro-context.md").read_text()
        assert "task-3" in content_after
        assert "Write tests" in content_after

    def test_context_shows_observations(self, daemon, tmp_repo):
        """Pending observations should appear in the context file."""
        from keshro_cli.daemon import Observation

        daemon.state.observations = [
            Observation("2026-03-25T12:00:00Z", "possible_completion",
                        "Commit during active task: refactor auth"),
            Observation("2026-03-25T12:05:00Z", "possible_learning",
                        "Error resolved: missing import for httpx"),
        ]
        daemon._write_context()
        content = (tmp_repo / ".claude" / "keshro-context.md").read_text()

        assert "Pending Observations" in content
        assert "possible_completion" in content
        assert "possible_learning" in content
        assert "refactor auth" in content


class TestDaemonLifecycle:
    """Test: start → run briefly → shutdown → cleanup."""

    def test_start_and_shutdown(self, tmp_repo):
        """Daemon should start, write PID, and clean up on shutdown."""
        daemon = KeshroDaemon(plan_id="plan-1", token="test-token")
        fake_client = _FakeAsyncClient()

        async def _run_briefly():
            with patch("keshro_cli.daemon.make_async_client", return_value=fake_client), \
                 patch("keshro_cli.daemon._repo_root", return_value=tmp_repo), \
                 patch.object(daemon, "_watcher", create=True) as mock_watcher:
                mock_watcher.start = MagicMock()
                mock_watcher.stop = MagicMock()

                # Start the daemon but signal shutdown immediately
                daemon.state.repo_root = tmp_repo
                daemon.state.plan = SAMPLE_PLAN
                daemon.state.plan_id = "plan-1"
                daemon.state.running = True
                daemon.state.started_at = "2026-03-25T12:00:00Z"
                daemon._setup_logging()

                _pid_file(tmp_repo).parent.mkdir(parents=True, exist_ok=True)
                _pid_file(tmp_repo).write_text(str(os.getpid()))

                # Write context
                daemon._write_context()

                # Verify PID file exists
                assert _pid_file(tmp_repo).exists()

                # Verify context file exists
                assert (tmp_repo / ".claude" / "keshro-context.md").exists()

                # Cleanup
                await daemon._cleanup()

                # PID file should be gone
                assert not _pid_file(tmp_repo).exists()
                # Status file should be gone
                assert not _status_file(tmp_repo).exists()

        asyncio.run(_run_briefly())

    def test_stale_pid_cleanup(self, tmp_repo):
        """Starting with a stale PID file should clean it up."""
        _pid_file(tmp_repo).parent.mkdir(parents=True, exist_ok=True)
        _pid_file(tmp_repo).write_text("99999999")  # Non-existent PID

        daemon = KeshroDaemon(plan_id="plan-1")
        daemon.state.repo_root = tmp_repo
        from keshro_cli.daemon import _socket_path
        sock_path = _socket_path(tmp_repo)

        # Create a stale socket file
        sock_path.parent.mkdir(parents=True, exist_ok=True)
        sock_path.touch()

        result = daemon._check_stale_daemon(sock_path)
        assert result is True
        assert not sock_path.exists()

        # Cleanup
        _pid_file(tmp_repo).unlink(missing_ok=True)


class TestStatusFileIntegration:
    """Test: status file round-trip (daemon writes, CLI reads)."""

    def test_status_round_trip(self, daemon, tmp_repo):
        """Status written by daemon should be readable by read_daemon_status."""
        daemon._write_status_file()

        assert _status_file(tmp_repo).exists()
        status = json.loads(_status_file(tmp_repo).read_text())

        assert status["plan_id"] == "plan-1"
        assert status["plan_title"] == "Test Plan"
        assert status["current_task_id"] == "task-2"
        assert status["current_task_title"] == "Add API"
        assert status["running"] is True
        assert status["events_processed"] == 0
        assert status["repo"] == str(daemon.state.repo_root)

        # Cleanup
        _status_file(tmp_repo).unlink(missing_ok=True)

    def test_status_includes_recent_observations(self, daemon, tmp_repo):
        """Status should include the last 10 observations."""
        from keshro_cli.daemon import Observation

        for i in range(15):
            daemon.state.observations.append(
                Observation(f"2026-03-25T12:{i:02d}:00Z", "test", f"obs-{i}")
            )

        daemon._write_status_file()
        status = json.loads(_status_file(tmp_repo).read_text())

        assert status["pending_observations"] == 15
        assert len(status["recent_observations"]) == 10  # Last 10 only
        assert status["recent_observations"][-1]["description"] == "obs-14"

        # Cleanup
        _status_file(tmp_repo).unlink(missing_ok=True)


class TestAutoSetupIntegration:
    """Test: first-run setup creates all necessary files."""

    def test_full_auto_setup(self, tmp_repo):
        """Auto-setup should create .claude dir, context file, and .gitignore entry."""
        daemon = KeshroDaemon(plan_id="plan-1")
        daemon.state.repo_root = tmp_repo
        daemon.state.plan = SAMPLE_PLAN
        daemon.state.plan_id = "plan-1"
        daemon.state.current_task_id = "task-2"

        daemon.setup_repo()
        daemon._write_context()

        # .claude dir created
        assert (tmp_repo / ".claude").is_dir()

        # Context file written
        ctx = tmp_repo / ".claude" / "keshro-context.md"
        assert ctx.exists()
        assert "task-2" in ctx.read_text()

        # .gitignore updated
        gi = tmp_repo / ".gitignore"
        assert gi.exists()
        assert "keshro-context.md" in gi.read_text()

    def test_auto_setup_preserves_existing_gitignore(self, tmp_repo):
        """Auto-setup should not clobber existing .gitignore content."""
        gi = tmp_repo / ".gitignore"
        gi.write_text("node_modules/\n.env\n*.pyc\n")

        daemon = KeshroDaemon()
        daemon.state.repo_root = tmp_repo
        daemon.setup_repo()

        content = gi.read_text()
        assert "node_modules/" in content
        assert ".env" in content
        assert "*.pyc" in content
        assert "keshro-context.md" in content


class TestBranchSwitchIntegration:
    """Test: branch switch → re-detect plan → update context on next heartbeat."""

    def test_branch_switch_triggers_plan_re_detection(self, daemon, tmp_repo):
        """Switching branches should change the plan ID and clear cached plan."""
        with patch.object(daemon, "_auto_detect_plan_id", return_value="plan-new"):
            event = GitEvent(
                event_type="branch_switch",
                timestamp=time.monotonic(),
                data={"branch": "feature/new-api"},
            )
            daemon._on_git_event(event)

        assert daemon.state.plan_id == "plan-new"
        assert daemon.state.plan is None  # Cleared, will be fetched on heartbeat
        assert daemon.state.observe_only is False

    def test_branch_switch_to_unknown_plan_keeps_current(self, daemon, tmp_repo):
        """If the new branch has no associated plan, keep the current one."""
        original_plan_id = daemon.state.plan_id

        with patch.object(daemon, "_auto_detect_plan_id", return_value=None):
            event = GitEvent(
                event_type="branch_switch",
                timestamp=time.monotonic(),
                data={"branch": "hotfix/quick-fix"},
            )
            daemon._on_git_event(event)

        # Plan should be unchanged (None != "plan-1" check didn't trigger)
        assert daemon.state.plan_id == original_plan_id


class TestMultipleCommitsIntegration:
    """Test: rapid commits are handled correctly."""

    def test_multiple_commits_complete_multiple_tasks(self, daemon):
        """Two commits matching different tasks should queue two events."""
        events = [
            GitEvent("commit", time.monotonic(), {"sha": "aaa", "message": "task-2: done with API"}),
            GitEvent("commit", time.monotonic() + 1, {"sha": "bbb", "message": "task-3: tests written"}),
        ]

        # Need task-3 to be matchable
        daemon.state.plan["plan_steps"][2]["status"] = "in_progress"

        for e in events:
            daemon._handle_commit(e)

        assert len(daemon.state.event_queue) == 2
        assert daemon.state.event_queue[0]["task_id"] == "task-2"
        assert daemon.state.event_queue[1]["task_id"] == "task-3"
        assert daemon.state.tasks_completed == 2

    def test_duplicate_commit_for_same_task(self, daemon):
        """Two commits mentioning the same task should queue two events (idempotency is backend's job)."""
        for _ in range(2):
            event = GitEvent("commit", time.monotonic(), {"sha": "aaa", "message": "task-2: more work"})
            daemon._handle_commit(event)

        # Both queued — backend handles idempotency
        assert len(daemon.state.event_queue) == 2
