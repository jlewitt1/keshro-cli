"""End-to-end integration tests for the full daemon (Phase 0.1 + 0.2).

Tests the complete flow: daemon starts → installs hooks → receives hook events
via socket → tracks files → detects commands → sends API calls → writes context
→ shuts down cleanly.
"""

import asyncio
import json
import time
from unittest.mock import patch

import pytest

from keshro_cli.daemon import _socket_path, _status_file, KeshroDaemon
from keshro_cli.hooks import HOOK_SCRIPT_NAME, install_hooks, uninstall_hooks
from keshro_cli.watcher import GitEvent


# ---------------------------------------------------------------------------
# Fake async HTTP client
# ---------------------------------------------------------------------------

SAMPLE_PLAN = {
    "id": "plan-e2e",
    "title": "E2E Test Plan",
    "plan_steps": [
        {
            "id": "task-1",
            "title": "Setup",
            "status": "done",
            "depends_on": [],
            "description": "Initial setup",
            "related_files": ["setup.py"],
        },
        {
            "id": "task-2",
            "title": "Build API",
            "status": "in_progress",
            "depends_on": ["task-1"],
            "description": "Implement endpoints",
            "acceptance_criteria": "All tests pass",
            "related_files": ["api.py", "routes.py"],
        },
        {
            "id": "task-3",
            "title": "Tests",
            "status": "todo",
            "depends_on": ["task-2"],
            "description": "Write test suite",
        },
    ],
}


class _FakeAsyncResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, path, **kwargs):
        self.calls.append(("GET", path, kwargs))
        if (
            "/v1/plans/" in path
            and "task-event" not in path
            and "heartbeat" not in path
        ):
            return _FakeAsyncResponse(SAMPLE_PLAN)
        return _FakeAsyncResponse({})

    async def post(self, path, **kwargs):
        self.calls.append(("POST", path, kwargs))
        return _FakeAsyncResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_repo(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir / "refs" / "heads").mkdir(parents=True)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# E2E tests
# ---------------------------------------------------------------------------


class TestDaemonE2ELifecycle:
    """Full daemon lifecycle: start → events → API calls → context → shutdown."""

    def test_full_lifecycle_git_commit_to_api(self, tmp_repo):
        """Git commit with task ID → daemon detects → API call → context updated."""
        fake_client = _FakeAsyncClient()
        daemon = KeshroDaemon(plan_id="plan-e2e", token="test-token")
        daemon.state.repo_root = tmp_repo
        daemon.state.plan = SAMPLE_PLAN.copy()
        daemon.state.plan_id = "plan-e2e"
        daemon.state.current_task_id = "task-2"
        daemon.state.running = True
        daemon.state.task_start_time = time.monotonic() - 30

        # 1. Simulate git commit with task ID
        git_event = GitEvent(
            event_type="commit",
            timestamp=time.monotonic(),
            data={"sha": "abc123", "message": "task-2: implement REST endpoints"},
        )
        daemon._handle_commit(git_event)

        # 2. Verify event queued
        assert len(daemon.state.event_queue) == 1
        assert daemon.state.event_queue[0]["task_id"] == "task-2"

        # 3. Flush to API
        async def _flush():
            with patch("keshro_cli.daemon.make_async_client", return_value=fake_client):
                await daemon._flush_queue()

        asyncio.run(_flush())

        # 4. Verify API received the event
        post_calls = [c for c in fake_client.calls if c[0] == "POST"]
        assert len(post_calls) == 1
        assert "task-event" in post_calls[0][1]
        assert post_calls[0][2]["json"]["task_id"] == "task-2"

        # 5. Verify context file was updated
        ctx_file = tmp_repo / ".claude" / "keshro-context.md"
        assert ctx_file.exists()
        content = ctx_file.read_text()
        assert "plan-e2e" in content

    def test_full_lifecycle_hook_event_to_file_tracking(self, tmp_repo):
        """Hook event → daemon tracks file → records metrics."""
        daemon = KeshroDaemon(plan_id="plan-e2e", token="test-token")
        daemon.state.repo_root = tmp_repo
        daemon.state.plan = SAMPLE_PLAN.copy()
        daemon.state.plan_id = "plan-e2e"
        daemon.state.current_task_id = "task-2"
        daemon.state.running = True
        daemon.state.task_start_time = time.monotonic() - 10

        # 1. Simulate file edits via hooks
        daemon._on_hook_event(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "/src/api.py"},
            }
        )
        daemon._on_hook_event(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": "/src/routes.py"},
            }
        )
        daemon._on_hook_event(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "npm test"},
            }
        )

        # 2. Verify tracking
        assert daemon.state.hook_events_received == 3
        assert "/src/api.py" in daemon.state.files_touched
        assert "/src/routes.py" in daemon.state.files_touched
        assert daemon.state.files_in_progress["/src/api.py"] == "task-2"

        # 3. Agent completes task via keshro command
        daemon._on_hook_event(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "keshro task done task-2 -p plan-e2e"},
            }
        )

        # 4. Verify completion
        assert daemon.state.tasks_completed == 1
        assert len(daemon.state.event_queue) == 1
        # Files should be reset after completion
        assert daemon.state.files_touched == []

    def test_full_lifecycle_setup_hooks_and_cleanup(self, tmp_repo):
        """Setup installs hooks → shutdown removes them."""
        daemon = KeshroDaemon(plan_id="plan-e2e", token="test-token")
        daemon.state.repo_root = tmp_repo

        # 1. Setup
        sock_path = _socket_path(tmp_repo)
        daemon.setup_repo()
        install_hooks(tmp_repo, sock_path)

        # 2. Verify hooks installed
        settings = json.loads((tmp_repo / ".claude" / "settings.json").read_text())
        assert len(settings["hooks"]["PostToolUse"]) > 0
        assert (tmp_repo / ".claude" / HOOK_SCRIPT_NAME).exists()

        # 3. Verify gitignore updated
        gitignore = (tmp_repo / ".gitignore").read_text()
        assert "keshro-context.md" in gitignore
        assert "keshro-hook.sh" in gitignore

        # 4. Cleanup
        uninstall_hooks(tmp_repo)
        settings = json.loads((tmp_repo / ".claude" / "settings.json").read_text())
        assert len(settings["hooks"]["PostToolUse"]) == 0
        assert not (tmp_repo / ".claude" / HOOK_SCRIPT_NAME).exists()

    def test_full_lifecycle_heartbeat_refreshes_everything(self, tmp_repo):
        """Heartbeat → re-fetch plan → update context → write status file."""
        updated_plan = {
            **SAMPLE_PLAN,
            "plan_steps": [
                {"id": "task-1", "status": "done", "depends_on": []},
                {"id": "task-2", "status": "done", "depends_on": ["task-1"]},
                {
                    "id": "task-3",
                    "status": "in_progress",
                    "depends_on": ["task-2"],
                    "title": "Tests",
                    "description": "Write test suite",
                },
            ],
        }
        fake_client = _FakeAsyncClient()
        fake_client._plan = updated_plan

        daemon = KeshroDaemon(plan_id="plan-e2e", token="test-token")
        daemon.state.repo_root = tmp_repo
        daemon.state.plan = SAMPLE_PLAN.copy()
        daemon.state.plan_id = "plan-e2e"
        daemon.state.current_task_id = "task-2"
        daemon.state.running = True

        async def patched_get(path, **kwargs):
            fake_client.calls.append(("GET", path, kwargs))
            if "/v1/plans/" in path:
                return _FakeAsyncResponse(updated_plan)
            return _FakeAsyncResponse({})

        fake_client.get = patched_get

        async def _heartbeat():
            with patch("keshro_cli.daemon.make_async_client", return_value=fake_client):
                await daemon._send_heartbeat()

        asyncio.run(_heartbeat())

        # Plan updated
        assert daemon.state.current_task_id == "task-3"

        # Context file updated
        ctx = (tmp_repo / ".claude" / "keshro-context.md").read_text()
        assert "task-3" in ctx
        assert "Tests" in ctx

        # Status file written
        sf = _status_file(tmp_repo)
        assert sf.exists()
        status = json.loads(sf.read_text())
        assert status["current_task_id"] == "task-3"

        # Cleanup
        sf.unlink(missing_ok=True)

    def test_mixed_signals_git_and_hooks(self, tmp_repo):
        """Both git commits and hook events processed correctly together."""
        fake_client = _FakeAsyncClient()
        daemon = KeshroDaemon(plan_id="plan-e2e", token="test-token")
        daemon.state.repo_root = tmp_repo
        daemon.state.plan = SAMPLE_PLAN.copy()
        daemon.state.plan_id = "plan-e2e"
        daemon.state.current_task_id = "task-2"
        daemon.state.running = True
        daemon.state.task_start_time = time.monotonic() - 60

        # 1. Hook event: file edit
        daemon._on_hook_event(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "/src/api.py"},
            }
        )
        assert daemon.state.hook_events_received == 1
        assert len(daemon.state.files_touched) == 1

        # 2. Git commit: low confidence (no task ID)
        daemon._handle_commit(
            GitEvent(
                "commit",
                time.monotonic(),
                {"sha": "aaa", "message": "wip: working on endpoints"},
            )
        )
        assert len(daemon.state.observations) == 1
        assert len(daemon.state.event_queue) == 0  # Low confidence, no action

        # 3. More hook events
        daemon._on_hook_event(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": "/src/routes.py"},
            }
        )

        # 4. Git commit: high confidence (has task ID)
        daemon._handle_commit(
            GitEvent(
                "commit",
                time.monotonic(),
                {"sha": "bbb", "message": "task-2: complete API implementation"},
            )
        )
        assert daemon.state.tasks_completed == 1
        assert len(daemon.state.event_queue) == 1

        # 5. Flush everything
        async def _flush():
            with patch("keshro_cli.daemon.make_async_client", return_value=fake_client):
                await daemon._flush_queue()

        asyncio.run(_flush())

        # Only the high-confidence event was sent
        post_calls = [c for c in fake_client.calls if c[0] == "POST"]
        assert len(post_calls) == 1
        assert post_calls[0][2]["json"]["task_id"] == "task-2"

    def test_observe_only_mode_with_hooks(self, tmp_repo):
        """In observe-only mode, hooks still track but don't send to API."""
        daemon = KeshroDaemon(token="test-token")
        daemon.state.repo_root = tmp_repo
        daemon.state.observe_only = True
        daemon.state.running = True

        # Hook events are still processed
        daemon._on_hook_event(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "/src/main.py"},
            }
        )

        assert daemon.state.hook_events_received == 1
        assert "/src/main.py" in daemon.state.files_touched
        # But no API events queued (no plan)
        assert len(daemon.state.event_queue) == 0
