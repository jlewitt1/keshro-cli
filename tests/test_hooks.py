"""Tests for Claude Code hook integration (Phase 0.2)."""

import asyncio
import json
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from keshro_cli.hooks import (
    HOOK_MARKER,
    HOOK_SCRIPT_NAME,
    WATCHED_TOOLS,
    HookSocketServer,
    detect_keshro_command,
    extract_bash_command,
    extract_file_paths,
    generate_hook_script,
    install_hooks,
    parse_hook_event,
    uninstall_hooks,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_repo(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    return tmp_path


@pytest.fixture
def socket_path():
    """Short socket path to avoid AF_UNIX length limit."""
    path = Path(f"/tmp/keshro-test-{uuid.uuid4().hex[:8]}.sock")
    yield path
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Hook registration
# ---------------------------------------------------------------------------


class TestInstallHooks:
    def test_creates_hook_script(self, tmp_repo):
        sock = Path("/tmp/test.sock")
        install_hooks(tmp_repo, sock)

        script = tmp_repo / ".claude" / HOOK_SCRIPT_NAME
        assert script.exists()
        content = script.read_text()
        assert str(sock) in content
        assert "nc -U" in content

    def test_registers_hooks_in_settings(self, tmp_repo):
        install_hooks(tmp_repo, Path("/tmp/test.sock"))

        settings = json.loads((tmp_repo / ".claude" / "settings.json").read_text())
        post_hooks = settings["hooks"]["PostToolUse"]
        matchers = [h["matcher"] for h in post_hooks]
        assert set(matchers) == set(WATCHED_TOOLS)

    def test_preserves_existing_settings(self, tmp_repo):
        settings_file = tmp_repo / ".claude" / "settings.json"
        settings_file.write_text(json.dumps({
            "theme": "dark",
            "hooks": {"PreToolUse": [{"matcher": "Write", "hooks": []}]}
        }))

        install_hooks(tmp_repo, Path("/tmp/test.sock"))

        settings = json.loads(settings_file.read_text())
        assert settings["theme"] == "dark"
        assert len(settings["hooks"]["PreToolUse"]) == 1

    def test_idempotent(self, tmp_repo):
        install_hooks(tmp_repo, Path("/tmp/test.sock"))
        install_hooks(tmp_repo, Path("/tmp/test.sock"))

        settings = json.loads((tmp_repo / ".claude" / "settings.json").read_text())
        post_hooks = settings["hooks"]["PostToolUse"]
        # Should not duplicate — exactly len(WATCHED_TOOLS) entries
        assert len(post_hooks) == len(WATCHED_TOOLS)

    def test_preserves_non_keshro_hooks(self, tmp_repo):
        settings_file = tmp_repo / ".claude" / "settings.json"
        settings_file.write_text(json.dumps({
            "hooks": {"PostToolUse": [
                {"matcher": "Write", "hooks": [{"type": "command", "command": "/usr/bin/prettier"}]}
            ]}
        }))

        install_hooks(tmp_repo, Path("/tmp/test.sock"))

        settings = json.loads(settings_file.read_text())
        post_hooks = settings["hooks"]["PostToolUse"]
        # Original prettier hook + our 4 hooks
        assert len(post_hooks) == len(WATCHED_TOOLS) + 1
        commands = [h["hooks"][0]["command"] for h in post_hooks]
        assert "/usr/bin/prettier" in commands


class TestUninstallHooks:
    def test_removes_keshro_hooks(self, tmp_repo):
        install_hooks(tmp_repo, Path("/tmp/test.sock"))
        uninstall_hooks(tmp_repo)

        settings = json.loads((tmp_repo / ".claude" / "settings.json").read_text())
        post_hooks = settings["hooks"]["PostToolUse"]
        assert len(post_hooks) == 0

    def test_preserves_other_hooks(self, tmp_repo):
        settings_file = tmp_repo / ".claude" / "settings.json"
        settings_file.write_text(json.dumps({
            "hooks": {"PostToolUse": [
                {"matcher": "Write", "hooks": [{"type": "command", "command": "/usr/bin/prettier"}]}
            ]}
        }))
        install_hooks(tmp_repo, Path("/tmp/test.sock"))
        uninstall_hooks(tmp_repo)

        settings = json.loads(settings_file.read_text())
        post_hooks = settings["hooks"]["PostToolUse"]
        assert len(post_hooks) == 1
        assert post_hooks[0]["hooks"][0]["command"] == "/usr/bin/prettier"

    def test_removes_hook_script(self, tmp_repo):
        install_hooks(tmp_repo, Path("/tmp/test.sock"))
        assert (tmp_repo / ".claude" / HOOK_SCRIPT_NAME).exists()
        uninstall_hooks(tmp_repo)
        assert not (tmp_repo / ".claude" / HOOK_SCRIPT_NAME).exists()


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------


class TestParseHookEvent:
    def test_valid_json(self):
        data = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}).encode()
        event = parse_hook_event(data)
        assert event["tool_name"] == "Bash"

    def test_malformed_json(self):
        assert parse_hook_event(b"not json{{{") is None

    def test_empty_bytes(self):
        assert parse_hook_event(b"") is None


class TestExtractFilePaths:
    def test_write_event(self):
        event = {"tool_name": "Write", "tool_input": {"file_path": "/src/main.py"}}
        assert extract_file_paths(event) == ["/src/main.py"]

    def test_edit_event(self):
        event = {"tool_name": "Edit", "tool_input": {"file_path": "/src/app.ts"}}
        assert extract_file_paths(event) == ["/src/app.ts"]

    def test_bash_event_no_paths(self):
        event = {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}
        assert extract_file_paths(event) == []

    def test_missing_file_path(self):
        event = {"tool_name": "Write", "tool_input": {}}
        assert extract_file_paths(event) == []


class TestDetectKeshroCommand:
    def test_task_done(self):
        event = {"tool_name": "Bash", "tool_input": {"command": "keshro task done task-123 -p plan-1"}}
        result = detect_keshro_command(event)
        assert result == ("task_done", "task-123")

    def test_kr_alias(self):
        event = {"tool_name": "Bash", "tool_input": {"command": "kr task done task-abc"}}
        result = detect_keshro_command(event)
        assert result == ("task_done", "task-abc")

    def test_task_note(self):
        event = {"tool_name": "Bash", "tool_input": {"command": 'keshro task note task-123 "learned something"'}}
        result = detect_keshro_command(event)
        assert result == ("task_note", "task-123")

    def test_non_keshro_command(self):
        event = {"tool_name": "Bash", "tool_input": {"command": "npm install"}}
        assert detect_keshro_command(event) is None

    def test_non_bash_tool(self):
        event = {"tool_name": "Write", "tool_input": {"file_path": "/tmp/foo"}}
        assert detect_keshro_command(event) is None


# ---------------------------------------------------------------------------
# Socket server
# ---------------------------------------------------------------------------


class TestHookSocketServer:
    def test_receives_and_processes_event(self, socket_path):
        received = []

        async def _run():
            server = HookSocketServer(socket_path, lambda e: received.append(e))
            await server.start()

            # Send an event
            reader, writer = await asyncio.open_unix_connection(str(socket_path))
            payload = {"tool_name": "Write", "tool_input": {"file_path": "/src/main.py"}}
            writer.write(json.dumps(payload).encode())
            writer.write_eof()
            await asyncio.sleep(0.1)  # Let server process
            writer.close()
            await writer.wait_closed()

            await server.stop()

        asyncio.run(_run())
        assert len(received) == 1
        assert received[0]["tool_name"] == "Write"

    def test_handles_malformed_json(self, socket_path):
        received = []

        async def _run():
            server = HookSocketServer(socket_path, lambda e: received.append(e))
            await server.start()

            reader, writer = await asyncio.open_unix_connection(str(socket_path))
            writer.write(b"not json at all")
            writer.write_eof()
            await asyncio.sleep(0.1)
            writer.close()
            await writer.wait_closed()

            await server.stop()

        asyncio.run(_run())
        assert len(received) == 0  # Malformed events are dropped

    def test_multiple_concurrent_events(self, socket_path):
        received = []

        async def _run():
            server = HookSocketServer(socket_path, lambda e: received.append(e))
            await server.start()

            async def send(tool):
                reader, writer = await asyncio.open_unix_connection(str(socket_path))
                writer.write(json.dumps({"tool_name": tool}).encode())
                writer.write_eof()
                await asyncio.sleep(0.05)
                writer.close()
                await writer.wait_closed()

            await asyncio.gather(*[send(t) for t in ["Write", "Bash", "Edit"]])
            await asyncio.sleep(0.1)
            await server.stop()

        asyncio.run(_run())
        assert len(received) == 3
        tools = sorted(e["tool_name"] for e in received)
        assert tools == ["Bash", "Edit", "Write"]


# ---------------------------------------------------------------------------
# Daemon hook event integration
# ---------------------------------------------------------------------------


class TestDaemonHookIntegration:
    """Test that the daemon correctly processes hook events."""

    @pytest.fixture
    def daemon(self, tmp_repo):
        from keshro_cli.daemon import KeshroDaemon
        d = KeshroDaemon(plan_id="plan-1")
        d.state.repo_root = tmp_repo
        d.state.plan_id = "plan-1"
        d.state.plan = {
            "plan_steps": [
                {"id": "task-1", "status": "done", "depends_on": []},
                {"id": "task-2", "status": "in_progress", "depends_on": ["task-1"]},
            ]
        }
        d.state.current_task_id = "task-2"
        d.state.running = True
        d.state.task_start_time = time.monotonic() - 60  # Started 60s ago
        return d

    def test_file_edit_tracked(self, daemon):
        event = {"tool_name": "Write", "tool_input": {"file_path": "/src/api.py"}}
        daemon._on_hook_event(event)

        assert "/src/api.py" in daemon.state.files_touched
        assert daemon.state.files_in_progress["/src/api.py"] == "task-2"
        assert daemon.state.hook_events_received == 1

    def test_keshro_task_done_detected(self, daemon):
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "keshro task done task-2 -p plan-1"},
        }
        daemon._on_hook_event(event)

        assert len(daemon.state.event_queue) == 1
        assert daemon.state.event_queue[0]["task_id"] == "task-2"
        assert daemon.state.tasks_completed == 1

    def test_non_keshro_bash_tracked(self, daemon):
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "npm test"},
        }
        daemon._on_hook_event(event)

        # No task completion, but hook event counted
        assert len(daemon.state.event_queue) == 0
        assert daemon.state.hook_events_received == 1

    def test_bash_test_run_detected(self, daemon):
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "python -m pytest tests/ -v"},
            "tool_response": {"exit_code": 0},
        }
        daemon._on_hook_event(event)

        assert daemon.state.hook_events_received == 1
        # Should have a test_result observation
        assert any(o.signal_type == "test_result" for o in daemon.state.observations)

    def test_bash_test_failure_detected(self, daemon):
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "npm test"},
            "tool_response": {"exit_code": 1},
        }
        daemon._on_hook_event(event)

        obs = [o for o in daemon.state.observations if o.signal_type == "test_result"]
        assert len(obs) == 1
        assert "fail" in obs[0].description

    def test_hook_completion_deduplicates_git_commit(self, daemon):
        """If hook already completed the task, git commit should not duplicate."""
        # Hook completes the task
        daemon._on_hook_event({
            "tool_name": "Bash",
            "tool_input": {"command": "keshro task done task-2 -p plan-1"},
        })
        assert len(daemon.state.event_queue) == 1

        # Git commit for same task should be skipped
        from keshro_cli.watcher import GitEvent
        daemon._handle_commit(GitEvent(
            "commit", 0, {"sha": "abc", "message": "task-2: done"},
        ))
        # Still only 1 event — not duplicated
        assert len(daemon.state.event_queue) == 1
        assert daemon.state.tasks_completed == 1

    def test_daemon_works_without_git_watcher(self, daemon):
        """Hooks-first: daemon should function purely from hook events."""
        # Simulate a full task lifecycle via hooks only (no git)
        daemon._on_hook_event({"tool_name": "Write", "tool_input": {"file_path": "/src/api.py"}})
        daemon._on_hook_event({"tool_name": "Edit", "tool_input": {"file_path": "/src/routes.py"}})
        daemon._on_hook_event({"tool_name": "Bash", "tool_input": {"command": "npm test"}, "tool_response": {"exit_code": 0}})
        daemon._on_hook_event({"tool_name": "Bash", "tool_input": {"command": "keshro task done task-2 -p plan-1"}})

        assert daemon.state.hook_events_received == 4
        assert len(set(daemon.state.files_touched)) == 0  # Reset after completion
        assert daemon.state.tasks_completed == 1
        assert len(daemon.state.event_queue) == 1

    def test_task_metrics_recorded_on_completion(self, daemon):
        # Touch some files
        daemon.state.files_touched = ["/a.py", "/b.py", "/a.py"]

        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "keshro task done task-2 -p plan-1"},
        }
        daemon._on_hook_event(event)

        # Files should be reset after completion
        assert daemon.state.files_touched == []
        # Task start time should be reset
        assert daemon.state.task_start_time > 0
