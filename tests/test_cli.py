import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import click
import httpx
import pytest

from keshro_cli import __version__, cli


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _auth_with_org():
    return {
        "default_org_id": "org-456",
        "default_org_name": "Demo Inc",
    }


def _auth_with_plan():
    return {
        "default_plan_id": "plan-123",
        "default_plan_title": "AWS Batch to Airflow pilot",
    }


def test_build_agent_exec_command_omits_add_dir_for_codex():
    command = cli._build_agent_exec_command(
        "codex",
        "codex",
        "do the thing",
        task_title="Test task",
        work_dir="/tmp/demo",
        worktree_name="keshro-demo",
    )

    assert "--add-dir" not in command
    assert command[:3] == ["codex", "exec", "do the thing"]


def test_merge_codex_worktree_changes_applies_worktree_diff():
    with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as worktree_parent:
        subprocess.run(
            ["git", "init"], cwd=repo_dir, check=True, capture_output=True, text=True
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )

        target = Path(repo_dir) / "demo.txt"
        target.write_text("base\n")
        subprocess.run(
            ["git", "add", "demo.txt"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "base"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )

        base_rev = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        worktree_dir = os.path.join(worktree_parent, "codex-worktree")
        branch_name = "keshro-test-branch"
        subprocess.run(
            ["git", "worktree", "add", "-b", branch_name, worktree_dir, base_rev],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        try:
            Path(worktree_dir, "demo.txt").write_text("updated\n")
            import asyncio

            asyncio.run(
                cli._merge_codex_worktree_changes(
                    repo_dir,
                    worktree_dir,
                    base_rev,
                    "task-123",
                )
            )
            assert target.read_text() == "updated\n"
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", worktree_dir],
                cwd=repo_dir,
                check=False,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "branch", "-D", branch_name],
                cwd=repo_dir,
                check=False,
                capture_output=True,
                text=True,
            )


def test_merge_codex_worktree_changes_resets_repo_after_apply_failure(monkeypatch):
    import asyncio

    calls = []

    class _FakeProc:
        def __init__(self, returncode=0, stdout=b"", stderr=b""):
            self.returncode = returncode
            self._stdout = stdout
            self._stderr = stderr

        async def communicate(self, _input=None):
            await asyncio.sleep(0)
            return self._stdout, self._stderr

    async def _fake_git_stdout(*args, cwd):
        calls.append(("git_stdout", args, cwd))
        if args[:3] == ("git", "status", "--short"):
            return "M file.txt"
        if args[:3] == ("git", "rev-parse", "HEAD"):
            return "worktree-head"
        return ""

    async def _fake_create_subprocess_exec(*args, **kwargs):
        calls.append(("subprocess", args, kwargs.get("cwd")))
        if args[:2] == ("git", "commit"):
            return _FakeProc(returncode=0)
        if args[:2] == ("git", "diff"):
            return _FakeProc(returncode=0, stdout=b"patch-bytes")
        if args[:3] == ("git", "apply", "--3way"):
            return _FakeProc(returncode=1, stderr=b"apply failed")
        if args[:4] == ("git", "reset", "--hard", "HEAD"):
            return _FakeProc(returncode=0)
        raise AssertionError(f"Unexpected subprocess args: {args}")

    monkeypatch.setattr(cli, "_git_stdout", _fake_git_stdout)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    with pytest.raises(RuntimeError, match="apply failed"):
        asyncio.run(
            cli._merge_codex_worktree_changes(
                "/tmp/repo", "/tmp/worktree", "base-rev", "task-123"
            )
        )

    assert any(
        call[0] == "subprocess" and call[1][:4] == ("git", "reset", "--hard", "HEAD")
        for call in calls
    )


def test_collect_task_outcome_uses_merge_base_when_no_checkpoint(monkeypatch):
    seen_ranges = []

    def _fake_run(cmd, capture_output=None, text=None, cwd=None, check=None):
        if cmd == ["git", "log", "--grep=keshro: checkpoint", "-1", "--format=%H"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "merge-base", "HEAD", "origin/main"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="merge-base-sha\n", stderr=""
            )
        if cmd[:3] == ["git", "diff", "--numstat"]:
            seen_ranges.append(cmd[3])
            return subprocess.CompletedProcess(
                cmd, 0, stdout="3\t1\tsrc/demo.py\n", stderr=""
            )
        if cmd[:3] == ["git", "diff", "--name-status"]:
            assert cmd[3] == "merge-base-sha..HEAD"
            return subprocess.CompletedProcess(cmd, 0, stdout="M\tsrc/demo.py\n", stderr="")
        if cmd[:3] == ["git", "log", "--format=%H"]:
            assert cmd[3] == "merge-base-sha..HEAD"
            return subprocess.CompletedProcess(
                cmd, 0, stdout="commit-1\ncommit-2\n", stderr=""
            )
        if cmd[:3] == ["git", "diff", "--stat"]:
            assert cmd[3] == "merge-base-sha..HEAD"
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=(
                    " src/demo.py | 4 ++--\n"
                    " 1 file changed, 3 insertions(+), 1 deletion(-)\n"
                ),
                stderr="",
            )
        raise AssertionError(cmd)

    monkeypatch.setattr("keshro_cli.cli.subprocess.run", _fake_run)

    outcome = cli._collect_task_outcome("/tmp/demo")

    assert seen_ranges == ["merge-base-sha..HEAD"]
    assert outcome == {
        "files_changed": [
            {
                "path": "src/demo.py",
                "lines_added": 3,
                "lines_removed": 1,
                "change_type": "modified",
            }
        ],
        "commits": ["commit-1", "commit-2"],
        "diff_stat": "1 file changed, 3 insertions(+), 1 deletion(-)",
    }


def test_collect_task_outcome_falls_back_to_root_commit_without_merge_base(monkeypatch):
    seen_ranges = []

    def _fake_run(cmd, capture_output=None, text=None, cwd=None, check=None):
        if cmd == ["git", "log", "--grep=keshro: checkpoint", "-1", "--format=%H"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["git", "merge-base", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd == ["git", "rev-list", "--max-parents=0", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="root-sha\n", stderr="")
        if cmd[:3] == ["git", "diff", "--numstat"]:
            seen_ranges.append(cmd[3])
            return subprocess.CompletedProcess(
                cmd, 0, stdout="5\t0\tsrc/rooted.py\n", stderr=""
            )
        if cmd[:3] == ["git", "diff", "--name-status"]:
            assert cmd[3] == "root-sha..HEAD"
            return subprocess.CompletedProcess(cmd, 0, stdout="A\tsrc/rooted.py\n", stderr="")
        if cmd[:3] == ["git", "log", "--format=%H"]:
            assert cmd[3] == "root-sha..HEAD"
            return subprocess.CompletedProcess(cmd, 0, stdout="commit-1\n", stderr="")
        if cmd[:3] == ["git", "diff", "--stat"]:
            assert cmd[3] == "root-sha..HEAD"
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=(
                    " src/rooted.py | 5 +++++\n"
                    " 1 file changed, 5 insertions(+)\n"
                ),
                stderr="",
            )
        raise AssertionError(cmd)

    monkeypatch.setattr("keshro_cli.cli.subprocess.run", _fake_run)

    outcome = cli._collect_task_outcome("/tmp/demo")

    assert seen_ranges == ["root-sha..HEAD"]
    assert outcome == {
        "files_changed": [
            {
                "path": "src/rooted.py",
                "lines_added": 5,
                "lines_removed": 0,
                "change_type": "added",
            }
        ],
        "commits": ["commit-1"],
        "diff_stat": "1 file changed, 5 insertions(+)",
    }


def test_launch_single_agent_marks_task_blocked_when_codex_merge_fails(monkeypatch):
    import asyncio

    class _FakeProc:
        def __init__(self, returncode=0, stdout=b"", stderr=b""):
            self.returncode = returncode
            self._stdout = stdout
            self._stderr = stderr

        async def communicate(self, _input=None):
            await asyncio.sleep(0)
            return self._stdout, self._stderr

    class _FakeAsyncClient:
        def __init__(self):
            self.posts = []

        async def post(self, path, json=None):
            self.posts.append((path, json))
            return None

    task_updates = []

    async def _fake_mark_task_status_async(
        client, plan_id, task_id, status, notes=None, blocked_reason=None
    ):
        task_updates.append(
            {
                "plan_id": plan_id,
                "task_id": task_id,
                "status": status,
                "notes": notes,
                "blocked_reason": blocked_reason,
            }
        )

    async def _fake_git_stdout(*args, cwd):
        if args[:3] == ("git", "rev-parse", "HEAD"):
            return "base-rev"
        return ""

    async def _fake_cleanup_worktree(repo_dir, worktree_path):
        return None

    async def _fake_merge_codex_worktree_changes(
        repo_dir, worktree_path, base_rev, task_id
    ):
        raise RuntimeError("apply failed")

    async def _fake_create_subprocess_exec(*args, **kwargs):
        if args[:3] == ("git", "worktree", "add"):
            return _FakeProc(returncode=0)
        if args and args[0] == "codex":
            return _FakeProc(returncode=0, stdout=b"codex finished", stderr=b"")
        if args[:3] == ("git", "branch", "-D"):
            return _FakeProc(returncode=0)
        raise AssertionError(f"Unexpected subprocess args: {args}")

    monkeypatch.setattr(cli, "_resolve_prompt_agent", lambda agent: ("codex", "codex"))
    monkeypatch.setattr(
        cli,
        "_build_parallel_prompt",
        lambda plan, task, total_agents, work_dir=None: "prompt",
    )
    monkeypatch.setattr(cli, "_mark_task_status_async", _fake_mark_task_status_async)
    monkeypatch.setattr(cli, "_git_stdout", _fake_git_stdout)
    monkeypatch.setattr(cli, "_cleanup_worktree", _fake_cleanup_worktree)
    monkeypatch.setattr(
        cli, "_merge_codex_worktree_changes", _fake_merge_codex_worktree_changes
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    result = asyncio.run(
        cli._launch_single_agent(
            {"id": "task-1", "title": "Test task"},
            {"id": "plan-1"},
            "plan-1",
            "/tmp/project",
            1,
            asyncio.Semaphore(1),
            _FakeAsyncClient(),
            session_id="session-1",
            agent="codex",
        )
    )

    assert result.exit_code == 1
    assert any(update["status"] == "blocked" for update in task_updates)
    assert not any(update["status"] == "completed" for update in task_updates)


def test_launch_single_agent_marks_task_blocked_when_codex_worktree_create_fails(
    monkeypatch,
):
    import asyncio

    class _FakeProc:
        def __init__(self, returncode=0, stdout=b"", stderr=b""):
            self.returncode = returncode
            self._stdout = stdout
            self._stderr = stderr

        async def communicate(self, _input=None):
            await asyncio.sleep(0)
            return self._stdout, self._stderr

    class _FakeAsyncClient:
        def __init__(self):
            self.posts = []

        async def post(self, path, json=None):
            self.posts.append((path, json))
            return None

    task_updates = []

    async def _fake_mark_task_status_async(
        client, plan_id, task_id, status, notes=None, blocked_reason=None
    ):
        task_updates.append(
            {
                "plan_id": plan_id,
                "task_id": task_id,
                "status": status,
                "notes": notes,
                "blocked_reason": blocked_reason,
            }
        )

    async def _fake_git_stdout(*args, cwd):
        if args[:3] == ("git", "rev-parse", "HEAD"):
            return "base-rev"
        return ""

    async def _fake_create_subprocess_exec(*args, **kwargs):
        if args[:3] == ("git", "worktree", "add"):
            return _FakeProc(returncode=1, stderr=b"fatal: worktree add failed")
        raise AssertionError(f"Unexpected subprocess args: {args}")

    monkeypatch.setattr(cli, "_resolve_prompt_agent", lambda agent: ("codex", "codex"))
    monkeypatch.setattr(
        cli,
        "_build_parallel_prompt",
        lambda plan, task, total_agents, work_dir=None: "prompt",
    )
    monkeypatch.setattr(cli, "_mark_task_status_async", _fake_mark_task_status_async)
    monkeypatch.setattr(cli, "_git_stdout", _fake_git_stdout)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    result = asyncio.run(
        cli._launch_single_agent(
            {"id": "task-1", "title": "Test task"},
            {"id": "plan-1"},
            "plan-1",
            "/tmp/project",
            1,
            asyncio.Semaphore(1),
            _FakeAsyncClient(),
            session_id="session-1",
            agent="codex",
        )
    )

    assert result.exit_code == 1
    assert any(update["status"] == "blocked" for update in task_updates)
    assert "worktree add failed" in (task_updates[-1]["blocked_reason"] or "")


def test_launch_single_agent_deletes_codex_branch_when_subprocess_launch_fails(
    monkeypatch,
):
    import asyncio

    calls = []

    class _FakeProc:
        def __init__(self, returncode=0, stdout=b"", stderr=b""):
            self.returncode = returncode
            self._stdout = stdout
            self._stderr = stderr

        async def communicate(self, _input=None):
            await asyncio.sleep(0)
            return self._stdout, self._stderr

    class _FakeAsyncClient:
        async def post(self, path, json=None):
            return None

    async def _fake_mark_task_status_async(
        client, plan_id, task_id, status, notes=None, blocked_reason=None
    ):
        return None

    async def _fake_git_stdout(*args, cwd):
        if args[:3] == ("git", "rev-parse", "HEAD"):
            return "base-rev"
        return ""

    async def _fake_cleanup_worktree(repo_dir, worktree_path):
        calls.append(("cleanup", repo_dir, worktree_path))

    async def _fake_create_subprocess_exec(*args, **kwargs):
        calls.append(("subprocess", args, kwargs.get("cwd")))
        if args[:3] == ("git", "worktree", "add"):
            return _FakeProc(returncode=0)
        if args and args[:3] == ("git", "branch", "-D"):
            return _FakeProc(returncode=0)
        if args and args[0] == "codex":
            raise RuntimeError("spawn failed")
        raise AssertionError(f"Unexpected subprocess args: {args}")

    monkeypatch.setattr(cli, "_resolve_prompt_agent", lambda agent: ("codex", "codex"))
    monkeypatch.setattr(
        cli,
        "_build_parallel_prompt",
        lambda plan, task, total_agents, work_dir=None: "prompt",
    )
    monkeypatch.setattr(cli, "_mark_task_status_async", _fake_mark_task_status_async)
    monkeypatch.setattr(cli, "_git_stdout", _fake_git_stdout)
    monkeypatch.setattr(cli, "_cleanup_worktree", _fake_cleanup_worktree)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    result = asyncio.run(
        cli._launch_single_agent(
            {"id": "task-1", "title": "Test task"},
            {"id": "plan-1"},
            "plan-1",
            "/tmp/project",
            1,
            asyncio.Semaphore(1),
            _FakeAsyncClient(),
            session_id="session-1",
            agent="codex",
        )
    )

    assert result.exit_code == 1
    assert any(call[0] == "cleanup" for call in calls)
    assert any(
        call[0] == "subprocess" and call[1][:3] == ("git", "branch", "-D")
        for call in calls
    )


def test_launch_single_agent_retries_codex_after_live_conflict(monkeypatch):
    import asyncio

    calls = []
    task_updates = []
    merge_calls = []
    note_events = []
    heartbeat_checks = []

    class _FakeProc:
        def __init__(self, returncode=0, stdout=b"", stderr=b""):
            self.returncode = returncode
            self._stdout = stdout
            self._stderr = stderr

        async def communicate(self, _input=None):
            await asyncio.sleep(0)
            return self._stdout, self._stderr

        async def wait(self):
            return self.returncode

    class _FakeAsyncClient:
        async def post(self, path, json=None):
            calls.append(("post", path, json))
            if json and json.get("event") == "note":
                note_events.append(json.get("note") or "")
            return None

    codex_runs = [
        _FakeProc(returncode=0, stdout=b"first attempt", stderr=b""),
        _FakeProc(returncode=0, stdout=b"second attempt", stderr=b""),
    ]

    async def _fake_mark_task_status_async(
        client, plan_id, task_id, status, notes=None, blocked_reason=None
    ):
        task_updates.append(
            {
                "plan_id": plan_id,
                "task_id": task_id,
                "status": status,
                "notes": notes,
                "blocked_reason": blocked_reason,
            }
        )

    async def _fake_git_stdout(*args, cwd):
        if args[:3] == ("git", "rev-parse", "HEAD"):
            return "base-rev"
        return ""

    async def _fake_cleanup_worktree(repo_dir, worktree_path):
        return None

    async def _fake_merge_codex_worktree_changes(
        repo_dir, worktree_path, base_rev, task_id
    ):
        merge_calls.append((repo_dir, worktree_path, base_rev, task_id))

    async def _fake_watch_live_conflicts(
        client, plan_id, task_id, *, worktree_path, proc, session_id=""
    ):
        heartbeat_checks.append((plan_id, task_id, worktree_path, session_id))
        if len(heartbeat_checks) == 1:
            return {
                "action": "pause",
                "conflict_task_id": "task-9",
                "reason": "Live overlap detected with task-9 on src/shared/util.py.",
            }
        return {}

    async def _fake_wait_for_conflict_resolution(client, plan_id, task_id):
        return {"action": "needs_rebase", "task": {"runtime_status": "needs_rebase"}}

    async def _fake_rebase_codex_worktree_onto_latest(repo_dir, worktree_path, task_id):
        calls.append(("rebase", repo_dir, worktree_path, task_id))
        return "rebased-base"

    async def _fake_create_subprocess_exec(*args, **kwargs):
        calls.append(("subprocess", args, kwargs.get("cwd")))
        if args[:3] == ("git", "worktree", "add"):
            return _FakeProc(returncode=0)
        if args and args[:3] == ("git", "branch", "-D"):
            return _FakeProc(returncode=0)
        if args and args[0] == "codex":
            return codex_runs.pop(0)
        raise AssertionError(f"Unexpected subprocess args: {args}")

    monkeypatch.setattr(cli, "_resolve_prompt_agent", lambda agent: ("codex", "codex"))
    monkeypatch.setattr(
        cli,
        "_build_parallel_prompt",
        lambda plan, task, total_agents, work_dir=None: "prompt",
    )
    monkeypatch.setattr(cli, "_mark_task_status_async", _fake_mark_task_status_async)
    monkeypatch.setattr(cli, "_git_stdout", _fake_git_stdout)
    monkeypatch.setattr(cli, "_cleanup_worktree", _fake_cleanup_worktree)
    monkeypatch.setattr(
        cli, "_merge_codex_worktree_changes", _fake_merge_codex_worktree_changes
    )
    monkeypatch.setattr(cli, "_watch_live_conflicts", _fake_watch_live_conflicts)
    monkeypatch.setattr(
        cli, "_wait_for_conflict_resolution", _fake_wait_for_conflict_resolution
    )
    monkeypatch.setattr(
        cli, "_rebase_codex_worktree_onto_latest", _fake_rebase_codex_worktree_onto_latest
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    result = asyncio.run(
        cli._launch_single_agent(
            {"id": "task-1", "title": "Test task"},
            {"id": "plan-1"},
            "plan-1",
            "/tmp/project",
            1,
            asyncio.Semaphore(1),
            _FakeAsyncClient(),
            session_id="session-1",
            agent="codex",
        )
    )

    assert result.exit_code == 0
    assert len(heartbeat_checks) == 2
    assert any(call[0] == "rebase" for call in calls)
    assert merge_calls[-1][2] == "rebased-base"
    assert any(update["status"] == "completed" for update in task_updates)
    assert any("Resuming after rebasing" in note for note in note_events)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code} error",
                request=httpx.Request("GET", "https://api.example.test"),
                response=httpx.Response(
                    self.status_code,
                    request=httpx.Request("GET", "https://api.example.test"),
                ),
            )

    def json(self):
        return self._payload


def test_find_plan_task_supports_all_plan_task_shapes():
    assert cli._find_plan_task({"plan_steps": [{"id": "task-1"}]}, "task-1") == {
        "id": "task-1"
    }
    assert cli._find_plan_task({"tasks": [{"id": "task-2"}]}, "task-2") == {
        "id": "task-2"
    }
    assert cli._find_plan_task(
        {"plan": {"tasks": [{"id": "task-3"}]}}, "task-3"
    ) == {"id": "task-3"}


def test_wait_for_conflict_resolution_skips_first_active_poll(monkeypatch):
    import asyncio

    responses = [
        _FakeResponse({"tasks": [{"id": "task-1", "runtime_status": "active"}]}),
        _FakeResponse({"tasks": [{"id": "task-1", "runtime_status": "needs_rebase"}]}),
    ]
    sleep_calls = []

    class _PollingClient:
        def __init__(self, payloads):
            self._payloads = payloads
            self.calls = 0

        async def get(self, path):
            assert path == "/v1/plans/plan-1"
            response = self._payloads[min(self.calls, len(self._payloads) - 1)]
            self.calls += 1
            return response

    async def _fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(cli.asyncio, "sleep", _fake_sleep)

    client = _PollingClient(responses)
    result = asyncio.run(
        cli._wait_for_conflict_resolution(client, "plan-1", "task-1")
    )

    assert result["action"] == "needs_rebase"
    assert client.calls == 2
    assert sleep_calls == [cli._LIVE_CONFLICT_POLL_SECONDS]


def test_commit_codex_worktree_snapshot_skips_git_add_when_clean(monkeypatch):
    import asyncio

    calls = []

    async def _fake_git_stdout(*args, cwd):
        calls.append((args, cwd))
        if args == ("git", "status", "--short"):
            return ""
        raise AssertionError(f"Unexpected git command: {args}")

    monkeypatch.setattr(cli, "_git_stdout", _fake_git_stdout)

    result = asyncio.run(
        cli._commit_codex_worktree_snapshot("/tmp/worktree", "task-1")
    )

    assert result is False
    assert calls == [(("git", "status", "--short"), "/tmp/worktree")]


class _FakeClient:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, path, params=None, headers=None, timeout=None):
        self.calls.append(("GET", path, params))
        if path == "/v1/plans/templates":
            return _FakeResponse(
                [
                    {
                        "key": "aws-batch-to-airflow",
                        "source_type": "AWS Batch",
                        "target_type": "Airflow",
                        "title": "AWS Batch to Airflow",
                        "summary": "Saved migration template for AWS Batch to Airflow.",
                        "why_use_it": "Separate scheduling/orchestration logic from the containerized job payload first.",
                        "plan_steps": [
                            {"title": "Capture current AWS Batch migration context"},
                            {"title": "Capture migration outcome and follow-up work"},
                        ],
                    }
                ]
            )
        if path == "/v1/orgs":
            return _FakeResponse(
                [
                    {"id": "org-123", "name": "Acme"},
                    {"id": "org-456", "name": "Demo Inc"},
                ]
            )
        if path == "/v1/migrations":
            return _FakeResponse(
                [
                    {
                        "id": "migration-123",
                        "status": "completed",
                        "source_type": "AWS Batch",
                        "target_type": "Airflow",
                        "migration_mode": "software",
                        "input_method": "context",
                        "created_at": "2026-03-11T15:30:00Z",
                        "confidence_score": 72,
                        "outcome_status": "partial",
                        "org_id": "org-123",
                    }
                ]
            )
        if path == "/v1/migrations/migration-123":
            return _FakeResponse(
                {
                    "id": "migration-123",
                    "status": "completed",
                    "source_type": "AWS Batch",
                    "target_type": "Airflow",
                    "migration_mode": "software",
                    "input_method": "context",
                    "created_at": "2026-03-11T15:30:00Z",
                    "confidence_score": 72,
                    "confidence_explanation": "Good source coverage and prior migration matches.",
                    "effort_estimate": {"total_hours": 36},
                    "cost_estimate": {"total_cost_low": 5000, "total_cost_high": 9000},
                    "risks": [
                        {"severity": "high", "title": "Scheduler parity gaps"},
                        {"severity": "medium", "title": "IAM drift during cutover"},
                    ],
                    "notes": "Pilot DAG first, then full cutover.",
                    "unknowns": [
                        {
                            "id": "u1",
                            "question": "Which Batch jobs need strict ordering guarantees?",
                            "priority": "high",
                        },
                        {
                            "id": "u2",
                            "question": "Do any jobs depend on ECS task role side effects?",
                            "priority": "medium",
                            "answer": "Not in the pilot scope.",
                        },
                    ],
                    "assessment_quality": {
                        "required_validations": [
                            "Compare Batch and Airflow outputs for the pilot DAG."
                        ],
                        "next_actions": [
                            "Confirm the cutover window with the platform team."
                        ],
                    },
                    "migration_steps": [
                        {
                            "order": 1,
                            "title": "Inventory Batch jobs",
                            "description": "Review queues and job definitions",
                        }
                    ],
                    "outcome_status": "partial",
                    "org_id": "org-123",
                }
            )
        if path == "/v1/migrations/path-template/lookup":
            template_key = (params or {}).get("template_key")
            if template_key == "aws-batch-to-airflow":
                return _FakeResponse(
                    {
                        "template_key": "aws-batch-to-airflow",
                        "source": "AWS Batch",
                        "target": "Airflow",
                        "title": "AWS Batch to Airflow",
                        "description": "Move orchestration into Airflow.",
                        "fields": [
                            {
                                "id": "batch_workloads",
                                "label": "AWS Batch workloads",
                                "type": "textarea",
                                "required": True,
                            },
                            {
                                "id": "target_airflow_deployment",
                                "label": "Target Airflow deployment",
                                "type": "select",
                                "options": ["AWS MWAA", "Self-hosted"],
                                "required": False,
                            },
                        ],
                        "tips": [],
                        "required_outputs": [],
                        "status": "curated",
                        "is_auto_generated": False,
                    }
                )
            return _FakeResponse({"detail": "not found"}, status_code=404)
        if path == "/v1/migrations/migration-123/plan":
            return _FakeResponse(
                {
                    "id": "plan-123",
                    "title": "AWS Batch to Airflow pilot",
                    "migration_id": "migration-123",
                    "status": "active",
                    "plan_steps": [
                        {"id": "task-1", "status": "done"},
                        {"id": "task-2", "status": "in_progress"},
                        {"id": "task-3", "status": "todo"},
                    ],
                    "plan_validation": {
                        "status": "needs_attention",
                        "overall_score": 68.0,
                        "findings": [{"code": "missing_cutover"}],
                    },
                    "task_feedback_events": [
                        {
                            "event_type": "task_updated",
                            "task_id": "task-456",
                            "task_title": "Translate pilot DAG",
                            "source": "cli",
                            "feedback_reason": "Pilot scope changed after DAG review",
                            "changed_fields": ["status", "notes"],
                            "created_at": "2026-03-15T16:00:00Z",
                        }
                    ],
                }
            )
        if path == "/v1/plans":
            return _FakeResponse(
                [
                    {
                        "id": "plan-123",
                        "title": "AWS Batch to Airflow pilot",
                        "status": "draft",
                        "source_type": "AWS Batch",
                        "target_type": "Airflow",
                        "summary": "Pilot plan for the first DAG migration.",
                        "template_key": "aws-batch-to-airflow",
                        "org_id": "org-123",
                        "updated_at": "2026-03-11T15:30:00Z",
                    }
                ]
            )
        if path == "/v1/plans/plan-123":
            return _FakeResponse(
                {
                    "id": "plan-123",
                    "title": "AWS Batch to Airflow pilot",
                    "migration_id": "migration-123",
                    "status": "ready",
                    "source_type": "AWS Batch",
                    "target_type": "Airflow",
                    "summary": "Pilot plan for the first DAG migration.",
                    "template_key": "aws-batch-to-airflow",
                    "import_source": "template",
                    "org_id": "org-123",
                    "updated_at": "2026-03-11T15:30:00Z",
                    "plan_steps": [
                        {
                            "id": "review-schedules",
                            "order": 1,
                            "title": "Review EventBridge schedules",
                            "description": "Map cron schedules into DAG schedules",
                            "status": "todo",
                            "owner": None,
                            "notes": None,
                            "blocked_reason": "Waiting on environment access",
                            "artifact_links": [
                                "https://linear.app/acme/issue/ENG-42",
                                "https://github.com/acme/migrations/pull/19",
                            ],
                        },
                        {
                            "id": "task-456",
                            "order": 2,
                            "title": "Translate pilot DAG",
                            "description": "Move the first batch workflow into Airflow",
                            "status": "todo",
                            "owner": None,
                            "notes": None,
                            "linear_issue_id": "KES-42",
                            "external_issue_provider": "linear",
                            "external_issue_id": "lin_123",
                            "external_issue_key": "KES-42",
                            "external_issue_url": "https://linear.app/keshro/issue/KES-42",
                            "blocked_reason": None,
                            "artifact_links": [],
                        },
                    ],
                }
            )
        return _FakeResponse({"ok": True})

    def post(self, path, json=None, timeout=None):
        self.calls.append(("POST", path, json))
        if "/push" in path:
            return _FakeResponse({"created": 3, "updated": 1})
        if "/sync-pull" in path:
            return _FakeResponse(
                {
                    "synced": 2,
                    "changes": [
                        {
                            "external_key": "KES-101",
                            "external_status": "completed",
                            "current_status": "in_progress",
                        },
                        {
                            "external_key": "KES-102",
                            "external_status": "in_progress",
                            "current_status": "todo",
                        },
                    ],
                }
            )
        if path == "/v1/migrations/clarifiers":
            return _FakeResponse({"questions": []})
        if path == "/v1/migrations":
            return _FakeResponse(
                {
                    "id": "migration-123",
                    "status": "analyzing",
                    "source_type": json.get("source_type"),
                    "target_type": json.get("target_type"),
                    "input_method": json.get("input_method"),
                }
            )
        if path == "/v1/plans/from-template":
            return _FakeResponse(
                {
                    "id": "plan-123",
                    "title": "AWS Batch to Airflow pilot",
                    "status": "ready",
                    "source_type": "AWS Batch",
                    "target_type": "Airflow",
                    "summary": "Pilot plan for the first DAG migration.",
                    "template_key": json.get("template_key"),
                    "migration_id": json.get("migration_id"),
                }
            )
        if path == "/v1/plans":
            return _FakeResponse(
                {
                    "id": "plan-123",
                    "title": json.get("title") or "Execution Plan",
                    "status": json.get("status") or "draft",
                    "migration_id": json.get("migration_id"),
                    "plan_steps": json.get("plan_steps") or [],
                    "import_source": json.get("import_source"),
                }
            )
        if path.endswith("/tasks"):
            return _FakeResponse(
                {
                    "id": "plan-123",
                    "title": "AWS Batch to Airflow pilot",
                    "plan_steps": [
                        {
                            "id": "task-999",
                            "title": json.get("title"),
                            "status": json.get("status") or "todo",
                            "owner": json.get("owner"),
                            "blocked_reason": json.get("blocked_reason"),
                            "artifact_links": json.get("artifact_links") or [],
                        }
                    ],
                }
            )
        return _FakeResponse({"path": path, "payload": json})

    def patch(self, path, json=None):
        self.calls.append(("PATCH", path, json))
        return _FakeResponse({"path": path, "payload": json})

    def request(self, method, path, json=None):
        self.calls.append((method, path, json))
        if method == "DELETE" and path.endswith("/tasks/task-456"):
            return _FakeResponse({"id": "plan-123", "plan_steps": [], "payload": json})
        return _FakeResponse({"success": True, "path": path, "payload": json})

    def delete(self, path):
        return self.request("DELETE", path, json=None)


@pytest.fixture
def fake_client(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(cli, "make_client", lambda api_url=None, token=None: client)
    return client


def test_whoami_is_not_a_valid_command():
    code = cli.main(["whoami"])
    assert code != 0


def test_main_without_args_prints_version(capsys):
    cli.main([])
    assert capsys.readouterr().out.strip() == __version__


def test_plan_generate_rejects_migration_like_requests(capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr(
        "keshro_cli.cli._detect_migration_intent",
        lambda _description: ("Prometheus", "SigNoz"),
    )

    code = cli.main(
        [
            "plan",
            "generate",
            "migrate all otel and prometheus related activities to signoz",
        ]
    )

    assert code == 1
    err = ANSI_RE.sub("", capsys.readouterr().err)
    assert "This request looks like a migration." in err
    assert "keshro create -m --context" in err


def test_plan_generate_accepts_non_migration_requests(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr("keshro_cli.cli._detect_migration_intent", lambda _description: None)

    original_post = fake_client.post

    def _post(path, json=None, timeout=None):
        if path == "/v1/plans/generate":
            return _FakeResponse(
                {
                    "id": "plan-generic-1",
                    "title": "Refactor auth module",
                    "status": "draft",
                    "plan_steps": [
                        {"order": 1, "title": "Review existing auth flows"},
                        {"order": 2, "title": "Implement API key support"},
                    ],
                }
            )
        return original_post(path, json=json, timeout=timeout)

    fake_client.post = _post

    code = cli.main(["plan", "generate", "refactor the auth module"])

    assert code == 0
    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "Project created: Refactor auth module" in out
    assert "Tasks: 2" in out


def test_templates_alias_lists_template_ids_by_default(fake_client, capsys):
    cli.main(["templates"])
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["aws-batch-to-airflow"]


def test_templates_alias_supports_verbose_output(fake_client, capsys):
    cli.main(["templates", "--verbose"])
    out = capsys.readouterr().out
    assert "aws-batch-to-airflow" in out
    assert "AWS Batch to Airflow" in out


def test_templates_alias_json_output(fake_client, capsys):
    cli.main(["--json", "templates"])
    out = json.loads(capsys.readouterr().out)
    assert out[0]["key"] == "aws-batch-to-airflow"


def test_templates_alias_can_show_single_template_details(fake_client, capsys):
    cli.main(["templates", "aws-batch-to-airflow"])
    out = capsys.readouterr().out
    assert "aws-batch-to-airflow" in out
    assert "Title:" in out
    assert "Plan steps:" in out


def test_templates_alias_can_show_single_template_details_with_name_flag(
    fake_client, capsys
):
    cli.main(["templates", "-n", "aws-batch-to-airflow"])
    out = capsys.readouterr().out
    assert "aws-batch-to-airflow" in out
    assert "Why use it:" in out


def test_templates_list_alias_is_accepted(fake_client, capsys):
    cli.main(["templates", "list"])
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["aws-batch-to-airflow"]


def test_plan_create_from_template_hits_template_endpoint(fake_client, capsys):
    cli.main(
        [
            "--json",
            "plan",
            "create",
            "migration-123",
            "-T",
            "aws-batch-to-airflow",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == "plan-123"
    assert out["template_key"] == "aws-batch-to-airflow"
    assert out["migration_id"] == "migration-123"


def test_create_migration_from_path_key_prompts_and_posts_payload(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "1")
    monkeypatch.setattr(
        cli, "_set_default_plan_after_create", lambda created, announce=True: None
    )
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/local/bin/claude" if name == "claude" else None,
    )

    def _fake_run(cmd, capture_output, text, cwd, check):
        assert cmd[0] == "/usr/local/bin/claude"
        assert "--add-dir" in cmd
        assert "--permission-mode" in cmd
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="\n".join(
                [
                    "## Versions",
                    "- Source version: 1.0",
                    "- Target version: 2.9",
                    "",
                    "## AWS Batch to Airflow details",
                    "- AWS Batch workloads: scheduled ETL jobs",
                    "- Target Airflow deployment: AWS MWAA",
                    "",
                    "## Additional context",
                    "- Anything else that materially affects risk, effort, validation, cutover, rollback, or delivery: none",
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", _fake_run)
    cli.main(["create", "--template", "aws-batch-to-airflow"])

    out = ANSI_RE.sub("", capsys.readouterr().out)
    clarifier_call = next(
        call for call in fake_client.calls if call[1] == "/v1/migrations/clarifiers"
    )
    create_call = next(
        call for call in fake_client.calls if call[1] == "/v1/migrations"
    )
    payload = clarifier_call[2]
    created_payload = create_call[2]
    assert payload["input_method"] == "cli_agent"
    assert payload["custom_fields"]["batch_workloads"] == "scheduled ETL jobs"
    assert payload["custom_fields"]["__keshro_discovered_context"]
    assert created_payload["custom_fields"]["target_airflow_deployment"] == "AWS MWAA"
    assert (
        "Submitting AWS Batch -> Airflow migration and generating execution plan..."
        in out
    )
    assert "Migration created: AWS Batch -> Airflow" in out
    assert "Dashboard:" in out
    assert "migration-123" in out
    assert "Plan URL:" not in out
    assert "Analysis is still running." in out
    assert "keshro continue" not in out


def test_create_migration_from_path_key_can_use_codex(fake_client, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_HOME", "/tmp/codex-home")
    monkeypatch.setattr(
        cli, "_set_default_plan_after_create", lambda created, announce=True: None
    )
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/Applications/Codex.app/Contents/Resources/codex"
        if name == "codex"
        else None,
    )

    def _fake_run(cmd, capture_output, text, cwd, check):
        assert cmd[:2] == ["/Applications/Codex.app/Contents/Resources/codex", "exec"]
        assert "--sandbox" in cmd
        assert "--ephemeral" in cmd
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="\n".join(
                [
                    "## Versions",
                    "- Source version: 1.0",
                    "- Target version: 2.9",
                    "",
                    "## AWS Batch to Airflow details",
                    "- AWS Batch workloads: scheduled ETL jobs",
                    "- Target Airflow deployment: AWS MWAA",
                    "",
                    "## Additional context",
                    "- Anything else that materially affects risk, effort, validation, cutover, rollback, or delivery: none",
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", _fake_run)

    code = cli.main(["create", "--template", "aws-batch-to-airflow", "--agent", "codex"])

    assert code == 0
    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "Migration created: AWS Batch -> Airflow" in out


def test_create_migration_from_path_key_applies_shared_clarifiers(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "1")
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    def _fake_run(cmd, capture_output, text, cwd, check):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="\n".join(
                [
                    "## Versions",
                    "- Source version: 1.0",
                    "- Target version: 2.9",
                    "",
                    "## AWS Batch to Airflow details",
                    "- AWS Batch workloads: scheduled ETL jobs",
                ]
            ),
            stderr="",
        )

    def _post(path, json=None):
        fake_client.calls.append(("POST", path, json))
        if path == "/v1/migrations/clarifiers":
            return _FakeResponse(
                {
                    "questions": [
                        {
                            "id": "rollback_strategy",
                            "question": "What rollback strategy do you want if validation fails?",
                            "field_target": "rollback_strategy",
                            "answers": [],
                            "allow_custom": True,
                        }
                    ]
                }
            )
        return _FakeResponse({"path": path, "payload": json})

    monkeypatch.setattr("subprocess.run", _fake_run)
    monkeypatch.setattr(fake_client, "post", _post)

    cli.main(
        [
            "create",
            "--template",
            "aws-batch-to-airflow",
            "--answer",
            "rollback_strategy=switch back to Batch scheduling immediately",
        ]
    )

    create_call = next(
        call for call in fake_client.calls if call[1] == "/v1/migrations"
    )
    created_payload = create_call[2]
    assert (
        created_payload["custom_fields"]["rollback_strategy"]
        == "switch back to Batch scheduling immediately"
    )
    assert "Critical clarifications" in created_payload["context"]


def test_create_migration_from_path_key_requires_claude_code(fake_client, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr("keshro_cli.cli._inside_coding_agent", lambda: False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    exit_code = cli.main(["create", "--template", "aws-batch-to-airflow"])
    assert exit_code == 1


def _bypass_auth(monkeypatch):
    """Skip the auth check in continue so tests can focus on prompt output."""
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)


def test_continue_prints_prompt_with_task_context(fake_client, monkeypatch, capsys):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    _bypass_auth(monkeypatch)

    cli.main(["continue", "--no-parallel"])

    out = capsys.readouterr().out
    assert "Task: Review EventBridge schedules" in out
    assert "Task ID: review-schedules" in out
    assert "Execution reminders:" in out
    assert "keshro task start review-schedules -p plan-123" in out


def test_continue_prompt_omits_full_skill_boilerplate_in_non_tty_mode(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    _bypass_auth(monkeypatch)

    cli.main(["continue", "--no-parallel"])

    out = capsys.readouterr().out
    assert "Do NOT use Keshro MCP tools" not in out
    assert "The current task and plan context are provided below" not in out


def test_spinner_truncates_long_messages_but_keeps_animation(monkeypatch, capsys):
    monkeypatch.setattr("keshro_cli.cli._stdout_is_tty", lambda: True)
    monkeypatch.setattr(
        "keshro_cli.cli.shutil.get_terminal_size",
        lambda fallback=(80, 24): os.terminal_size((40, 24)),
    )

    with cli._Spinner(
        "Analyzing the current working directory and generating AWS Batch -> Airflow migration inputs and follow-up questions..."
    ):
        pass

    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "⠋" in out
    assert "…" in out


def test_continue_prompt_mentions_status_tracking_and_blocking_rule(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    _bypass_auth(monkeypatch)

    cli.main(["continue", "--no-parallel"])

    out = capsys.readouterr().out
    assert "keshro status -p plan-123 --watch" in out
    assert "Only mark the task blocked if work cannot continue" in out


def test_continue_prompt_surfaces_plan_risks_unknowns_and_ui_link(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setattr(
        "keshro_cli.cli.load_auth",
        lambda: {**_auth_with_plan(), "api_url": "http://localhost:8000"},
    )
    monkeypatch.setattr(
        "keshro_cli.client.load_auth",
        lambda: {**_auth_with_plan(), "api_url": "http://localhost:8000"},
    )
    _bypass_auth(monkeypatch)

    original_get = fake_client.get

    def _get(path, params=None, headers=None, timeout=None):
        response = original_get(path, params=params, headers=headers, timeout=timeout)
        if path != "/v1/plans/plan-123":
            return response
        plan = response.json()
        plan["enrichment_sources"] = [
            {
                "name": "Web research",
                "detail": "Best practices for AWS Batch -> https://docs.aws.amazon.com/batch/latest/userguide/best-practices.html",
            }
        ]
        plan["decisions"] = {
            "risks": [
                {
                    "title": "Rollback path is unclear",
                    "description": "Current cutover steps do not define a clean rollback.",
                }
            ],
            "unknowns": [
                {
                    "question": "Which environments need phased rollout first?",
                }
            ],
        }
        return _FakeResponse(plan)

    fake_client.get = _get
    cli.main(["continue", "--no-parallel"])
    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "Top plan risks:" in out
    assert "Open questions:" in out
    assert (
        "Review full risks/questions in UI: http://localhost:3000/plans/plan-123" in out
    )
    assert "Source highlights:" not in out


def test_continue_in_agent_mode_resumes_in_progress_task_before_next_todo(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    _bypass_auth(monkeypatch)

    original_get = fake_client.get

    def _get(path, params=None):
        response = original_get(path, params=params)
        if path != "/v1/plans/plan-123":
            return response
        plan = response.json()
        plan["plan_steps"] = [
            {
                **plan["plan_steps"][0],
                "id": "task-1",
                "title": "Set up MWAA environment with Terraform",
                "status": "in_progress",
                "order": 1,
            },
            {
                **plan["plan_steps"][1],
                "id": "task-2",
                "title": "Create and validate DAG files",
                "status": "in_progress",
                "order": 2,
            },
            {
                "id": "task-3",
                "order": 3,
                "title": "Test DAGs locally with MWAA Docker",
                "description": "Validate DAG parsing and dependency compatibility",
                "status": "todo",
            },
        ]
        return _FakeResponse(plan)

    monkeypatch.setattr(fake_client, "get", _get)

    cli.main(["continue", "--no-parallel"])

    out = capsys.readouterr().out
    assert "Task: Set up MWAA environment with Terraform" in out
    assert "Task: Test DAGs locally with MWAA Docker" not in out


def test_continue_prompt_includes_error_guidance(fake_client, monkeypatch, capsys):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    _bypass_auth(monkeypatch)

    cli.main(["continue", "--no-parallel"])

    out = capsys.readouterr().out
    assert "If a keshro command fails" in out


def test_continue_confirms_when_using_implicit_plan_context(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr(
        "keshro_cli.cli._current_plan_label",
        lambda work_dir=None: "AWS Batch to Airflow pilot",
    )
    monkeypatch.setattr("keshro_cli.cli._stdout_is_tty", lambda: True)

    prompted = {}

    def _fake_confirm(message, default=True, abort=False):
        prompted["message"] = message
        prompted["default"] = default
        prompted["abort"] = abort
        return True

    monkeypatch.setattr("typer.confirm", _fake_confirm)
    monkeypatch.setattr(
        "keshro_cli.cli.asyncio.run",
        lambda coro: (coro.close(), None)[1],
    )

    cli.main(["continue", "--no-parallel"])

    assert "AWS Batch to Airflow pilot" in prompted["message"]
    assert "plan-123" in prompted["message"]
    assert prompted["default"] is True
    assert prompted["abort"] is False


def test_continue_skips_confirmation_when_plan_id_is_explicit(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr("keshro_cli.cli._stdout_is_tty", lambda: True)

    def _fail_confirm(*args, **kwargs):
        raise AssertionError("should not prompt when plan id is explicit")

    monkeypatch.setattr("typer.confirm", _fail_confirm)
    monkeypatch.setattr(
        "keshro_cli.cli.asyncio.run",
        lambda coro: (coro.close(), None)[1],
    )

    cli.main(["continue", "-p", "plan-123"])


def test_continue_skips_confirmation_when_migration_id_is_explicit(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr("keshro_cli.cli._stdout_is_tty", lambda: True)

    def _fail_confirm(*args, **kwargs):
        raise AssertionError("should not prompt when migration id is explicit")

    monkeypatch.setattr("typer.confirm", _fail_confirm)
    monkeypatch.setattr(
        "keshro_cli.cli.asyncio.run",
        lambda coro: (coro.close(), None)[1],
    )

    cli.main(["continue", "-m", "migration-123"])


def test_continue_rejects_plan_id_and_migration_id_together(
    fake_client, monkeypatch, capsys
):
    code = cli.main(["continue", "-p", "plan-123", "-m", "migration-123"])

    assert code == 1
    assert "either --plan-id or --migration-id" in capsys.readouterr().err


def test_continue_exits_cleanly_when_implicit_plan_confirmation_is_declined(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.cli._stdout_is_tty", lambda: True)
    monkeypatch.setattr("typer.confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    code = cli.main(["continue"])

    assert code == 0


def test_continue_can_override_implicit_plan_with_migration_id(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)

    original_get = fake_client.get

    def _get(path, params=None, headers=None, timeout=None):
        if path == "/v1/plans/migration-123":
            fake_client.calls.append(("GET", path, params))
            return _FakeResponse({"detail": "not found"}, status_code=404)
        return original_get(path, params=params, headers=headers, timeout=timeout)

    monkeypatch.setattr(fake_client, "get", _get)

    plan_id, title = cli._resolve_continue_override_context("migration-123")

    assert plan_id == "plan-123"
    assert title == "AWS Batch to Airflow pilot"
    assert ("GET", "/v1/plans/migration-123", None) in fake_client.calls
    assert ("GET", "/v1/migrations/migration-123/plan", None) in fake_client.calls


def test_continue_prompt_does_not_tell_claude_to_refetch(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    _bypass_auth(monkeypatch)

    cli.main(["continue", "--no-parallel"])

    out = capsys.readouterr().out
    assert "Do not re-fetch them" in out
    assert "Start by grounding" not in out


def test_continue_exits_when_not_authenticated(fake_client, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: {})
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: {})

    exit_code = cli.main(["continue", "-p", "plan-123"])
    assert exit_code == 1


def test_continue_allows_codex_for_parallel_mode(fake_client, monkeypatch, capsys):
    """Codex should be accepted for parallel mode (no longer rejected)."""
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr(
        "keshro_cli.cli._current_plan_label", lambda work_dir=None: "Test plan"
    )
    monkeypatch.setattr("typer.confirm", lambda *a, **kw: True)
    monkeypatch.setattr("keshro_cli.cli._stdout_is_tty", lambda: True)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    parallel_called = {}

    monkeypatch.setattr(
        "keshro_cli.cli.asyncio.run",
        lambda coro: (parallel_called.update({"yes": True}), coro.close())[1],
    )

    exit_code = cli.main(["continue", "-a", "codex"])
    out = ANSI_RE.sub("", capsys.readouterr().out)

    assert exit_code == 0
    assert parallel_called.get("yes")
    assert "Using Codex" in out


def test_continue_uses_parallel_mode_inside_coding_agent_by_default(
    fake_client, monkeypatch
):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr("keshro_cli.cli._inside_coding_agent", lambda: True)
    monkeypatch.setattr("keshro_cli.cli._stdout_is_tty", lambda: False)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    parallel_called = {}

    monkeypatch.setattr(
        "keshro_cli.cli.asyncio.run",
        lambda coro: (parallel_called.update({"yes": True}), coro.close())[1],
    )

    exit_code = cli.main(["continue"])

    assert exit_code == 0
    assert parallel_called.get("yes")


def test_continue_no_parallel_stays_single_task_inside_coding_agent(
    fake_client, monkeypatch
):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.cli._inside_coding_agent", lambda: True)
    monkeypatch.setattr("keshro_cli.cli._stdout_is_tty", lambda: False)

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "keshro_cli.cli._continue_with_claude",
        lambda resolved_plan_id, **kwargs: captured.update(
            {"plan_id": resolved_plan_id, **kwargs}
        ),
    )

    exit_code = cli.main(["continue", "--no-parallel"])

    assert exit_code == 0
    assert captured["plan_id"] == "plan-123"
    assert captured["parallel"] is False


def test_wrap_prompt_agent_error_suggests_switching_agents(monkeypatch):
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/Applications/Codex.app/Contents/Resources/codex"
        if name == "codex"
        else None,
    )

    message = cli._wrap_prompt_agent_error(
        "You've hit your limit · resets 9pm (Asia/Jerusalem)", "claude"
    )

    assert "Claude Code hit a usage limit" in message
    assert "keshro create --agent codex" in message
    assert "keshro config set --agent codex" in message


def test_continue_exits_when_token_expired(fake_client, monkeypatch):
    monkeypatch.setattr(
        "keshro_cli.cli.load_auth",
        lambda: {"token": "expired-token", "default_plan_id": "plan-123"},
    )
    monkeypatch.setattr(
        "keshro_cli.client.load_auth", lambda: {"token": "expired-token"}
    )

    def _fake_make_client(*args, **kwargs):
        class _ExpiredClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, path, **kw):
                resp = httpx.Response(401, request=httpx.Request("GET", path))
                raise httpx.HTTPStatusError(
                    "Unauthorized", request=resp.request, response=resp
                )

        return _ExpiredClient()

    monkeypatch.setattr("keshro_cli.cli.make_client", _fake_make_client)

    exit_code = cli.main(["continue", "-p", "plan-123"])
    assert exit_code == 1


def test_setup_claude_creates_skill(monkeypatch, tmp_path, capsys):
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr("keshro_cli.cli.CLAUDE_SKILLS_DIR", skills_dir)
    monkeypatch.setattr("keshro_cli.cli.CLAUDE_COMMANDS_DIR", tmp_path / "legacy_commands")

    cli.main(["setup-claude"])

    out = capsys.readouterr().out
    assert "Installed Claude Code skill" in out
    target = skills_dir / "keshro" / "SKILL.md"
    assert target.exists()
    content = target.read_text()
    assert "keshro continue" in content
    assert "Do NOT use Keshro MCP tools" in content
    assert "keshro create --context-file /tmp/keshro-context.txt" in content
    assert "Do not inspect the codebase first to decide whether Keshro is relevant." in content
    assert "the first pass may take a bit before follow-up questions appear" in content
    assert "immediately surface the dashboard URL to the user" in content
    assert "If the status is still `analyzing`, simply tell the user it was created" in content
    assert "Do not summarize findings, comment on elapsed time, or offer to keep polling by default." in content
    assert "move from X to Y" in content
    # Must have YAML frontmatter with description for auto-triggering
    assert content.startswith("---")
    assert "description:" in content


def test_skill_has_trigger_conditions():
    """The SKILL.md frontmatter description must contain trigger keywords
    so Claude Code auto-invokes Keshro for migration/refactor tasks."""
    content = cli.KESHRO_SLASH_COMMAND
    assert content.startswith("---"), "SKILL.md must have YAML frontmatter"
    # Extract the frontmatter description block
    parts = content.split("---", 2)
    frontmatter = parts[1]
    assert "description:" in frontmatter
    for keyword in ["TRIGGER when:", "migrate", "refactor", "upgrade", "convert"]:
        assert keyword in frontmatter, (
            f"'{keyword}' must appear in SKILL.md frontmatter description"
        )
    assert "DO NOT TRIGGER" in frontmatter


def test_setup_claude_overwrites_existing(monkeypatch, tmp_path, capsys):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "keshro"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("old content")
    monkeypatch.setattr("keshro_cli.cli.CLAUDE_SKILLS_DIR", skills_dir)
    monkeypatch.setattr("keshro_cli.cli.CLAUDE_COMMANDS_DIR", tmp_path / "legacy_commands")

    cli.main(["setup-claude"])

    content = (skill_dir / "SKILL.md").read_text()
    assert "old content" not in content
    assert "keshro continue" in content


def test_setup_claude_creates_symlink(monkeypatch, tmp_path, capsys):
    """setup-claude should create a symlink to the package's SKILL.md file."""
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr("keshro_cli.cli.CLAUDE_SKILLS_DIR", skills_dir)
    monkeypatch.setattr("keshro_cli.cli.CLAUDE_COMMANDS_DIR", tmp_path / "legacy_commands")

    cli.main(["setup-claude"])

    target = skills_dir / "keshro" / "SKILL.md"
    assert target.is_symlink(), "SKILL.md should be a symlink, not a regular file"
    assert target.resolve() == cli._SKILL_FILE.resolve()
    assert "TRIGGER when:" in target.read_text()


def test_setup_claude_replaces_regular_file_with_symlink(monkeypatch, tmp_path, capsys):
    """If an old regular file exists (pre-symlink installs), replace it."""
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "keshro"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("old copied content")
    monkeypatch.setattr("keshro_cli.cli.CLAUDE_SKILLS_DIR", skills_dir)
    monkeypatch.setattr("keshro_cli.cli.CLAUDE_COMMANDS_DIR", tmp_path / "legacy_commands")

    cli.main(["setup-claude"])

    target = skill_dir / "SKILL.md"
    assert target.is_symlink()
    assert "old copied content" not in target.read_text()
    assert "TRIGGER when:" in target.read_text()


def test_skill_file_lives_in_package():
    """The skill file must exist in the package data directory."""
    assert cli._SKILL_FILE.exists(), f"Skill file missing at {cli._SKILL_FILE}"
    content = cli._SKILL_FILE.read_text()
    assert content == cli.KESHRO_SLASH_COMMAND


def test_setup_claude_removes_legacy_command(monkeypatch, tmp_path, capsys):
    """setup-claude should remove old ~/.claude/commands/keshro.md."""
    skills_dir = tmp_path / "skills"
    legacy_dir = tmp_path / "legacy_commands"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "keshro.md").write_text("old command file")
    monkeypatch.setattr("keshro_cli.cli.CLAUDE_SKILLS_DIR", skills_dir)
    monkeypatch.setattr("keshro_cli.cli.CLAUDE_COMMANDS_DIR", legacy_dir)

    cli.main(["setup-claude"])

    assert not (legacy_dir / "keshro.md").exists()
    assert (skills_dir / "keshro" / "SKILL.md").exists()


def test_maybe_refresh_claude_migrates_legacy_command(monkeypatch, tmp_path):
    """Auto-refresh should migrate from commands/ to skills/ on upgrade."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    legacy_dir = tmp_path / "legacy_commands"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "keshro.md").write_text("old command content")
    monkeypatch.setattr("keshro_cli.cli.CLAUDE_SKILLS_DIR", skills_dir)
    monkeypatch.setattr("keshro_cli.cli.CLAUDE_COMMANDS_DIR", legacy_dir)

    cli._maybe_refresh_claude()

    assert not (legacy_dir / "keshro.md").exists()
    assert (skills_dir / "keshro" / "SKILL.md").is_symlink()


def test_maybe_refresh_claude_upgrades_stale_symlink(monkeypatch, tmp_path):
    """Auto-refresh should replace a symlink pointing to old install path."""
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "keshro"
    skill_dir.mkdir(parents=True)
    target = skill_dir / "SKILL.md"
    old_file = tmp_path / "old_skill.md"
    old_file.write_text("stale symlink target")
    target.symlink_to(old_file)
    monkeypatch.setattr("keshro_cli.cli.CLAUDE_SKILLS_DIR", skills_dir)
    monkeypatch.setattr("keshro_cli.cli.CLAUDE_COMMANDS_DIR", tmp_path / "legacy_commands")

    cli._maybe_refresh_claude()

    assert target.is_symlink()
    assert target.resolve() == cli._SKILL_FILE.resolve()


def test_maybe_refresh_claude_skips_when_current(monkeypatch, tmp_path):
    """Auto-refresh should not touch a correct symlink."""
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "keshro"
    skill_dir.mkdir(parents=True)
    target = skill_dir / "SKILL.md"
    target.symlink_to(cli._SKILL_FILE)
    mtime_before = target.lstat().st_mtime
    monkeypatch.setattr("keshro_cli.cli.CLAUDE_SKILLS_DIR", skills_dir)
    monkeypatch.setattr("keshro_cli.cli.CLAUDE_COMMANDS_DIR", tmp_path / "legacy_commands")

    cli._maybe_refresh_claude()

    assert target.lstat().st_mtime == mtime_before


def test_maybe_refresh_claude_skips_when_not_installed(monkeypatch, tmp_path):
    """Auto-refresh should not create skill dir if not installed."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    monkeypatch.setattr("keshro_cli.cli.CLAUDE_SKILLS_DIR", skills_dir)
    monkeypatch.setattr("keshro_cli.cli.CLAUDE_COMMANDS_DIR", tmp_path / "legacy_commands")

    cli._maybe_refresh_claude()

    assert not (skills_dir / "keshro").exists()


def test_create_reads_context_from_file(fake_client, monkeypatch, tmp_path, capsys):
    _auth = {**_auth_with_plan(), "token": "ksh_pat_test"}
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: _auth)
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: _auth)
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr("keshro_cli.cli.update_auth", lambda payload: payload)
    monkeypatch.setattr("keshro_cli.cli._inside_coding_agent", lambda: False)
    monkeypatch.setattr("keshro_cli.cli._collect_generic_discovery", lambda _: None)
    monkeypatch.setattr(
        "keshro_cli.cli._prompt_agent_display_name", lambda _agent: "Claude Code"
    )

    context_path = tmp_path / "context.txt"
    context_path.write_text("Scale the Helm chart safely.")

    original_post = fake_client.post

    def _post(path, json=None, timeout=None):
        if path == "/v1/plans/describe/preview":
            return _FakeResponse({"questions": [], "enrichment_context": ""})
        if path == "/v1/plans/generate":
            assert json["description"] == "Scale the Helm chart safely."
            return _FakeResponse(
                {
                    "id": "plan-ctx-1",
                    "title": "Scalability plan",
                    "status": "draft",
                    "plan_steps": [],
                }
            )
        return original_post(path, json=json, timeout=timeout)

    fake_client.post = _post

    code = cli.main(["create", "--context-file", str(context_path)])

    assert code == 0
    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "Scalability plan" in out


def test_should_scan_default_work_dir_accepts_repo_like_root(tmp_path):
    (tmp_path / ".git").mkdir()
    assert cli._should_scan_default_work_dir(str(tmp_path)) is True


def test_should_scan_default_work_dir_skips_parent_directory_with_multiple_repos(tmp_path):
    repo_one = tmp_path / "repo-one"
    repo_two = tmp_path / "repo-two"
    repo_one.mkdir()
    repo_two.mkdir()
    (repo_one / ".git").mkdir()
    (repo_two / "pyproject.toml").write_text("[project]\nname='demo'\n")
    assert cli._should_scan_default_work_dir(str(tmp_path)) is False


def test_should_scan_default_work_dir_honors_explicit_target(tmp_path):
    repo_one = tmp_path / "repo-one"
    repo_two = tmp_path / "repo-two"
    repo_one.mkdir()
    repo_two.mkdir()
    (repo_one / ".git").mkdir()
    (repo_two / "package.json").write_text("{}\n")
    assert cli._should_scan_default_work_dir(str(tmp_path), explicit_target=True) is True


def test_create_skips_default_directory_scan_when_cwd_looks_unrelated(
    fake_client, monkeypatch, tmp_path, capsys
):
    unrelated_root = tmp_path / "workspace"
    unrelated_root.mkdir()
    (unrelated_root / "repo-one").mkdir()
    (unrelated_root / "repo-two").mkdir()
    (unrelated_root / "repo-one" / ".git").mkdir(parents=True)
    (unrelated_root / "repo-two" / "pyproject.toml").write_text("[project]\nname='demo'\n")

    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr("keshro_cli.cli.update_auth", lambda payload: payload)
    monkeypatch.setattr("keshro_cli.cli._inside_coding_agent", lambda: False)
    monkeypatch.setattr("keshro_cli.cli._prompt_agent_display_name", lambda _agent: "Claude Code")
    collect_calls: list[str] = []
    monkeypatch.setattr(
        "keshro_cli.cli._collect_generic_discovery",
        lambda work_dir: collect_calls.append(work_dir) or None,
    )
    monkeypatch.chdir(unrelated_root)

    original_post = fake_client.post

    def _post(path, json=None, timeout=None):
        if path == "/v1/plans/describe/preview":
            assert json["discovered_context"] is None
            return _FakeResponse({"questions": [], "enrichment_context": ""})
        if path == "/v1/plans/generate":
            return _FakeResponse(
                {
                    "id": "plan-general-1",
                    "title": "General project plan",
                    "status": "draft",
                    "plan_steps": [],
                }
            )
        return original_post(path, json=json, timeout=timeout)

    fake_client.post = _post

    code = cli.main(["create", "--context", "refactor auth"])

    assert code == 0
    assert collect_calls == []
    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "Skipping repo scan" in out


def test_resolve_menu_choice_supports_numbers_text_and_aliases():
    assert cli._resolve_menu_choice("2", ["Migration", "General"]) == "General"
    assert cli._resolve_menu_choice("general", ["Migration", "General"]) == "General"
    assert (
        cli._resolve_menu_choice(
            "yes",
            ["Treat as migration", "Treat as a general project"],
            aliases={
                "y": "Treat as migration",
                "yes": "Treat as migration",
                "n": "Treat as a general project",
                "no": "Treat as a general project",
            },
        )
        == "Treat as migration"
    )
    assert (
        cli._resolve_menu_choice(
            "n",
            ["Treat as migration", "Treat as a general project"],
            aliases={
                "y": "Treat as migration",
                "yes": "Treat as migration",
                "n": "Treat as a general project",
                "no": "Treat as a general project",
            },
        )
        == "Treat as a general project"
    )


def test_format_preview_lines_wraps_without_breaking_words():
    lines, was_truncated = cli._format_preview_lines(
        "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu",
        width=20,
        max_lines=4,
    )

    assert lines == [
        "alpha beta gamma",
        "delta epsilon zeta",
        "eta theta iota kappa",
        "lambda mu",
    ]
    assert was_truncated is False


def test_format_preview_lines_truncates_cleanly():
    lines, was_truncated = cli._format_preview_lines(
        "alpha beta gamma. delta epsilon zeta. eta theta iota kappa lambda mu nu xi omicron.",
        width=20,
        max_lines=3,
    )

    assert lines == [
        "alpha beta gamma.",
        "delta epsilon zeta.",
        "eta theta iota...",
    ]
    assert was_truncated is True


def test_format_recommended_suffix_skips_duplicate_and_styles_marker():
    assert (
        cli._format_recommended_suffix("Hybrid orchestrator (Recommended)", True) == ""
    )
    assert (
        cli._format_recommended_suffix("Full replacement", True)
        == f" {cli.GREEN}(recommended){cli.RESET}"
    )
    assert cli._format_recommended_suffix("Scheduling only", False) == ""


def test_prompt_for_migration_template_fields_hides_duplicate_suggested_value_for_options(
    monkeypatch, capsys
):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("keshro_cli.cli._inside_coding_agent", lambda: False)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    template = {
        "fields": [
            {
                "id": "trigger_model",
                "label": "Current trigger model",
                "type": "select",
                "options": [
                    "EventBridge schedule",
                    "Application/API triggered",
                    "Step Functions / workflow engine",
                    "Mixed / unsure",
                ],
            }
        ]
    }

    cli._prompt_for_migration_template_fields(
        template, {"trigger_model": "EventBridge schedule"}
    )

    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "press Enter to keep the current value" in out
    assert "1. EventBridge schedule [suggested]" in out
    assert "Suggested value:" not in out
    assert "Current value:" not in out


def test_prompt_for_migration_template_fields_uses_replacement_label_for_previewed_textarea(
    monkeypatch, capsys
):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("keshro_cli.cli._inside_coding_agent", lambda: False)
    prompts = []

    def _fake_input(prompt=""):
        prompts.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", _fake_input)

    template = {
        "fields": [
            {
                "id": "batch_workloads",
                "label": "AWS Batch workloads",
                "type": "textarea",
                "required": True,
            }
        ]
    }

    cli._prompt_for_migration_template_fields(
        template,
        {
            "batch_workloads": (
                "5 workloads are defined. Scheduled jobs: acme-daily-sales-etl, "
                "acme-data-quality-checks, acme-report-generator."
            )
        },
    )

    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "Current value:" in out
    assert prompts == ["  Enter=keep, v=view full, r=replace: "]


def test_prompt_for_migration_template_fields_offers_view_for_truncated_textarea(
    monkeypatch, capsys
):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("keshro_cli.cli._inside_coding_agent", lambda: False)
    prompts = []
    answers = iter(["v", ""])

    def _fake_input(prompt=""):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", _fake_input)

    template = {
        "fields": [
            {
                "id": "batch_workloads",
                "label": "AWS Batch workloads",
                "type": "textarea",
                "required": True,
            }
        ]
    }

    cli._prompt_for_migration_template_fields(
        template,
        {
            "batch_workloads": (
                "5 scheduled Batch workloads are defined. Daily chain: "
                "`acme-daily-sales-etl` at `02:00 UTC` -> `acme-data-quality-checks` "
                "at `03:00 UTC` -> `acme-report-generator` at `06:00 UTC`. "
                "Independent job: `acme-inventory-sync` every 6 hours. Weekly job: "
                "`acme-customer-churn-scoring` runs Mondays at `04:00 UTC`."
            )
        },
    )

    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "(truncated; type 'v' to view the full value)" in out
    assert "Full value:" in out
    assert prompts == [
        "  Enter=keep, v=view full, r=replace: ",
        "  Enter=keep, v=view full, r=replace: ",
    ]


def test_prompt_for_migration_template_fields_ctrl_c_exits_review(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("keshro_cli.cli._inside_coding_agent", lambda: False)
    prompts = []

    def _fake_input(prompt=""):
        prompts.append(prompt)
        if len(prompts) == 1:
            raise KeyboardInterrupt
        return ""

    monkeypatch.setattr("builtins.input", _fake_input)

    template = {
        "fields": [
            {"id": "batch_workloads", "label": "AWS Batch workloads"},
            {"id": "compute_envs", "label": "Job definitions / compute environments"},
        ]
    }

    result = cli._prompt_for_migration_template_fields(
        template,
        {
            "batch_workloads": "Five scheduled jobs",
            "compute_envs": "Five Fargate job definitions",
        },
    )

    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "Job definitions / compute environments" not in out
    assert prompts == ["  Enter=keep, v=view full, r=replace: "]
    assert result == {
        "batch_workloads": "Five scheduled jobs",
        "compute_envs": "Five Fargate job definitions",
    }


def test_prompt_for_clarifying_questions_keeps_or_overrides_suggestions(monkeypatch):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("keshro_cli.cli._inside_coding_agent", lambda: False)
    responses = iter(["", "custom answer"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(responses))

    questions = [
        {
            "id": "hosting_environment",
            "question": "Where will the new workload run?",
            "answers": [
                {"answer_title": "MWAA", "value": "mwaa", "recommended": True},
                {"answer_title": "Self-hosted", "value": "self_hosted"},
            ],
        },
        {
            "id": "rollback_plan",
            "question": "How will rollback work?",
        },
    ]

    result = cli._prompt_for_clarifying_questions(
        questions,
        {"hosting_environment": "mwaa", "rollback_plan": "disable new scheduler"},
    )

    assert result == {
        "hosting_environment": "mwaa",
        "rollback_plan": "custom answer",
    }


def test_review_agent_suggested_answers_noninteractive_does_not_auto_apply(
    monkeypatch, capsys
):
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("keshro_cli.cli._inside_coding_agent", lambda: False)

    result = cli._review_agent_suggested_answers(
        [{"id": "hosting_environment", "question": "Where will it run?"}],
        {"hosting_environment": "mwaa"},
        heading="Clarifying questions",
        non_interactive_notice="Need user review before applying suggested answers.",
    )

    assert result == {}
    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "Need user review before applying suggested answers." in out


def test_create_interactive_migration_prompt_accepts_no_for_general_project(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr("keshro_cli.cli._collect_generic_discovery", lambda _: None)
    monkeypatch.setattr("keshro_cli.cli.update_auth", lambda payload: payload)
    monkeypatch.setattr("keshro_cli.cli._inside_coding_agent", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(
        "keshro_cli.cli._prompt_agent_display_name", lambda _agent: "Claude Code"
    )
    monkeypatch.setattr(
        "keshro_cli.cli._detect_migration_intent",
        lambda _description: ("AWS Batch", "Airflow"),
    )

    answers = iter(["plan the migration", "no"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    called = {"migration": False}

    def _fake_create_migration_inner(*args, **kwargs):
        called["migration"] = True

    monkeypatch.setattr(
        "keshro_cli.cli._create_migration_inner", _fake_create_migration_inner
    )

    original_post = fake_client.post

    def _post(path, json=None, timeout=None):
        if path == "/v1/plans/describe/preview":
            return _FakeResponse({"questions": [], "enrichment_context": ""})
        if path == "/v1/plans/generate":
            return _FakeResponse(
                {
                    "id": "plan-general-1",
                    "title": "General project plan",
                    "status": "draft",
                    "plan_steps": [],
                }
            )
        return original_post(path, json=json, timeout=timeout)

    fake_client.post = _post

    code = cli.main(["create"])

    assert code == 0
    assert called["migration"] is False
    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "General project plan" in out


def test_create_interactive_migration_prompt_accepts_yes_for_migration(
    fake_client, monkeypatch
):
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr("keshro_cli.cli._collect_generic_discovery", lambda _: None)
    monkeypatch.setattr("keshro_cli.cli._inside_coding_agent", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(
        "keshro_cli.cli._prompt_agent_display_name", lambda _agent: "Claude Code"
    )
    monkeypatch.setattr(
        "keshro_cli.cli._detect_migration_intent",
        lambda _description: ("AWS Batch", "Airflow"),
    )
    monkeypatch.setattr(
        "keshro_cli.cli._find_migration_template",
        lambda _source, _target: "aws-batch-to-airflow",
    )

    answers = iter(["plan the migration", "yes"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    captured: dict[str, object] = {}

    def _fake_create_migration_inner(
        path,
        provided_answers,
        context,
        github_url,
        resource_url,
        org_id,
        work_dir,
        **kwargs,
    ):
        captured["path"] = path
        captured["context"] = context

    monkeypatch.setattr(
        "keshro_cli.cli._create_migration_inner", _fake_create_migration_inner
    )

    code = cli.main(["create"])

    assert code == 0
    assert captured["path"] == "aws-batch-to-airflow"
    assert captured["context"] == "plan the migration"


def test_create_inside_coding_agent_explicit_migration_still_routes(monkeypatch):
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr("keshro_cli.cli._collect_generic_discovery", lambda _: None)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("keshro_cli.cli._inside_coding_agent", lambda: True)
    monkeypatch.setattr(
        "keshro_cli.cli._prompt_agent_display_name", lambda _agent: "Claude Code"
    )
    monkeypatch.setattr(
        "keshro_cli.cli._find_migration_template",
        lambda _source, _target: "aws-batch-to-airflow",
    )

    captured: dict[str, object] = {}

    def _fake_create_migration_inner(
        path,
        provided_answers,
        context,
        github_url,
        resource_url,
        org_id,
        work_dir,
        **kwargs,
    ):
        captured["path"] = path
        captured["context"] = context

    monkeypatch.setattr(
        "keshro_cli.cli._create_migration_inner", _fake_create_migration_inner
    )

    code = cli.main(["create", "--template", "aws-batch-to-airflow", "--context", "migrate from aws batch to airflow"])

    assert code == 0
    assert captured["path"] == "aws-batch-to-airflow"
    assert captured["context"] == "migrate from aws batch to airflow"


def test_create_inside_coding_agent_stops_for_detected_migration_confirmation(
    monkeypatch, capsys
):
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr("keshro_cli.cli._collect_generic_discovery", lambda _: None)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("keshro_cli.cli._inside_coding_agent", lambda: True)
    monkeypatch.setattr(
        "keshro_cli.cli._prompt_agent_display_name", lambda _agent: "Claude Code"
    )
    monkeypatch.setattr(
        "keshro_cli.cli._detect_migration_intent",
        lambda _description: ("AWS Batch", "Airflow"),
    )
    monkeypatch.setattr(
        "keshro_cli.cli._find_migration_template",
        lambda _source, _target: "aws-batch-to-airflow",
    )

    code = cli.main(["create", "--context", "migrate from aws batch to airflow"])

    assert code == 0
    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "This looks like a migration (AWS Batch -> Airflow)." in out
    assert "keshro create --template aws-batch-to-airflow --context 'migrate from aws batch to airflow'" in out


def test_create_explicit_migration_without_template_uses_custom_migration_path(
    monkeypatch,
):
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr("keshro_cli.cli._collect_generic_discovery", lambda _: None)
    monkeypatch.setattr(
        "keshro_cli.cli._prompt_for_optional_cli_context",
        lambda _label, existing: existing,
    )
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(
        "keshro_cli.cli._find_migration_template", lambda _source, _target: None
    )

    captured: dict[str, object] = {}

    def _fake_create_custom_migration_inner(
        source,
        target,
        context,
        github_url,
        resource_url,
        org_id,
        work_dir,
        **kwargs,
    ):
        captured["source"] = source
        captured["target"] = target
        captured["context"] = context

    monkeypatch.setattr(
        "keshro_cli.cli._create_custom_migration_inner",
        _fake_create_custom_migration_inner,
    )

    code = cli.main(
        [
            "create",
            "-m",
            "--source-type",
            "Prometheus",
            "--target-type",
            "SigNoz",
            "--context",
            "migrate the python client from prometheus to signoz",
        ]
    )

    assert code == 0
    assert captured == {
        "source": "Prometheus",
        "target": "SigNoz",
        "context": "migrate the python client from prometheus to signoz",
    }


def test_create_inside_coding_agent_stops_for_custom_migration_confirmation(
    monkeypatch, capsys
):
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr("keshro_cli.cli._collect_generic_discovery", lambda _: None)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("keshro_cli.cli._inside_coding_agent", lambda: True)
    monkeypatch.setattr(
        "keshro_cli.cli._prompt_agent_display_name", lambda _agent: "Claude Code"
    )
    monkeypatch.setattr(
        "keshro_cli.cli._detect_migration_intent",
        lambda _description: ("Prometheus", "SigNoz"),
    )
    monkeypatch.setattr(
        "keshro_cli.cli._find_migration_template", lambda _source, _target: None
    )

    code = cli.main(
        ["create", "--context", "move from prometheus to signoz in the python client"]
    )

    assert code == 0
    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "This looks like a migration (Prometheus -> SigNoz)." in out
    assert (
        "keshro create -m --context 'move from prometheus to signoz in the python client'"
        in out
    )
    assert "Treat as migration" in out


def test_create_inside_coding_agent_stops_before_generation_when_answers_missing(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr("keshro_cli.cli._collect_generic_discovery", lambda _: None)
    monkeypatch.setattr("keshro_cli.cli.update_auth", lambda payload: payload)
    monkeypatch.setattr("keshro_cli.cli._inside_coding_agent", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(
        "keshro_cli.cli._prompt_agent_display_name", lambda _agent: "Claude Code"
    )
    monkeypatch.setattr(
        "keshro_cli.cli._answer_questions_via_agent",
        lambda *args, **kwargs: {"hosting_environment": "mwaa"},
    )
    original_post = fake_client.post

    def _post(path, json=None, timeout=None):
        if path == "/v1/plans/describe/preview":
            return _FakeResponse(
                {
                    "questions": [
                        {
                            "id": "hosting_environment",
                            "question": "Where will the new workflow run?",
                            "answers": [
                                {
                                    "answer_title": "MWAA",
                                    "value": "mwaa",
                                    "recommended": True,
                                }
                            ],
                        }
                    ],
                    "enrichment_context": "",
                }
            )
        if path == "/v1/plans/generate":
            raise AssertionError("plan generation should not happen before user feedback")
        return original_post(path, json=json, timeout=timeout)

    fake_client.post = _post

    code = cli.main(["create", "--context", "refactor auth"])

    assert code == 0
    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "Keshro needs user answers before it can generate this plan." in out
    assert "--answers-file" in out


def test_create_inside_coding_agent_stops_for_clarifiers_until_user_answers(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr("keshro_cli.cli._collect_generic_discovery", lambda _: None)
    monkeypatch.setattr("keshro_cli.cli.update_auth", lambda payload: payload)
    monkeypatch.setattr("keshro_cli.cli._inside_coding_agent", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(
        "keshro_cli.cli._prompt_agent_display_name", lambda _agent: "Claude Code"
    )
    monkeypatch.setattr(
        "keshro_cli.cli._answer_questions_via_agent",
        lambda *args, **kwargs: {"hosting_environment": "mwaa"},
    )

    original_post = fake_client.post

    def _post(path, json=None, timeout=None):
        if path == "/v1/plans/describe/preview":
            return _FakeResponse(
                {
                    "questions": [
                        {
                            "id": "hosting_environment",
                            "question": "Where will the new workflow run?",
                            "answers": [
                                {
                                    "answer_title": "MWAA",
                                    "value": "mwaa",
                                    "recommended": True,
                                }
                            ],
                        }
                    ],
                    "enrichment_context": "",
                }
            )
        if path == "/v1/plans/generate":
            raise AssertionError("plan generation should not happen before user feedback")
        return original_post(path, json=json, timeout=timeout)

    fake_client.post = _post

    code = cli.main(["create", "--context", "refactor auth"])

    assert code == 0
    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "Keshro needs user answers before it can generate this plan." in out
    assert "--answers-file" in out


def test_create_inside_coding_agent_accepts_answer_flags_on_rerun(
    fake_client, monkeypatch
):
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr("keshro_cli.cli._collect_generic_discovery", lambda _: None)
    monkeypatch.setattr("keshro_cli.cli.update_auth", lambda payload: payload)
    monkeypatch.setattr("keshro_cli.cli._inside_coding_agent", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(
        "keshro_cli.cli._prompt_agent_display_name", lambda _agent: "Claude Code"
    )

    captured: dict[str, object] = {}
    original_post = fake_client.post

    def _post(path, json=None, timeout=None):
        if path == "/v1/plans/describe/preview":
            return _FakeResponse(
                {
                    "questions": [
                        {
                            "id": "hosting_environment",
                            "question": "Where will the new workflow run?",
                        }
                    ],
                    "enrichment_context": "",
                }
            )
        if path == "/v1/plans/generate":
            captured["description"] = json["description"]
            return _FakeResponse(
                {
                    "id": "plan-general-1",
                    "title": "General project plan",
                    "status": "draft",
                    "plan_steps": [],
                }
            )
        return original_post(path, json=json, timeout=timeout)

    fake_client.post = _post

    answers_path = Path("/tmp/keshro-test-answers.json")
    answers_path.write_text(json.dumps({"answers": {"hosting_environment": "mwaa"}}))

    code = cli.main(
        [
            "create",
            "--context",
            "refactor auth",
            "--answers-file",
            str(answers_path),
        ]
    )

    assert code == 0
    assert "A: mwaa" in str(captured["description"])


def test_create_inside_coding_agent_rerun_with_answers_file_skips_new_preview(
    fake_client, monkeypatch, tmp_path
):
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr("keshro_cli.cli._collect_generic_discovery", lambda _: None)
    monkeypatch.setattr("keshro_cli.cli.update_auth", lambda payload: payload)
    monkeypatch.setattr("keshro_cli.cli._inside_coding_agent", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(
        "keshro_cli.cli._prompt_agent_display_name", lambda _agent: "Claude Code"
    )

    captured: dict[str, object] = {}
    original_post = fake_client.post
    preview_calls = 0

    def _post(path, json=None, timeout=None):
        nonlocal preview_calls
        if path == "/v1/plans/describe/preview":
            preview_calls += 1
            raise AssertionError("preview should not run on answers-file resume")
        if path == "/v1/plans/generate":
            captured["description"] = json["description"]
            return _FakeResponse(
                {
                    "id": "plan-general-2",
                    "title": "General project plan",
                    "status": "draft",
                    "plan_steps": [],
                }
            )
        return original_post(path, json=json, timeout=timeout)

    fake_client.post = _post

    answers_path = tmp_path / "answers.json"
    answers_path.write_text(
        json.dumps(
            {
                "heading": "Keshro needs user answers before it can generate this plan.",
                "answers": {"hosting_environment": "mwaa"},
                "questions": [
                    {
                        "id": "hosting_environment",
                        "question": "Where will the new workflow run?",
                    }
                ],
                "enrichment_context": "Prefer changes that preserve existing deployment conventions.",
            }
        )
    )

    code = cli.main(
        [
            "create",
            "--context",
            "refactor auth",
            "--answers-file",
            str(answers_path),
        ]
    )

    assert code == 0
    assert preview_calls == 0
    assert "Prefer changes that preserve existing deployment conventions." in str(
        captured["description"]
    )
    assert "Where will the new workflow run?" in str(captured["description"])
    assert "A: mwaa" in str(captured["description"])


def test_review_agent_suggested_answers_accepts_suggestions_inside_agent(
    monkeypatch, capsys
):
    monkeypatch.setattr("keshro_cli.cli._inside_coding_agent", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    result = cli._review_agent_suggested_answers(
        [{"id": "hosting_environment", "question": "Where will it run?"}],
        {"hosting_environment": "mwaa"},
        heading="Clarifying questions",
        non_interactive_notice="should not be used",
    )

    assert result == {"hosting_environment": "mwaa"}
    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "Using agent-suggested answers" in out


def test_create_as_migration_uses_explicit_source_and_target(
    fake_client, monkeypatch
):
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr("keshro_cli.cli._collect_generic_discovery", lambda _: None)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    monkeypatch.setattr(
        "keshro_cli.cli._find_migration_template",
        lambda source, target: (
            "aws-batch-to-airflow"
            if (source, target) == ("AWS Batch", "Airflow")
            else None
        ),
    )

    captured: dict[str, object] = {}

    def _fake_create_migration_inner(
        path,
        provided_answers,
        context,
        github_url,
        resource_url,
        org_id,
        work_dir,
        **kwargs,
    ):
        captured["path"] = path
        captured["context"] = context

    monkeypatch.setattr(
        "keshro_cli.cli._create_migration_inner", _fake_create_migration_inner
    )

    code = cli.main(
        [
            "create",
            "--as-migration",
            "--source-type",
            "AWS Batch",
            "--target-type",
            "Airflow",
            "--context",
            "move orchestration to MWAA",
        ]
    )

    assert code == 0
    assert captured["path"] == "aws-batch-to-airflow"
    assert captured["context"] == "move orchestration to MWAA"


def test_create_as_migration_requires_both_source_and_target(monkeypatch, capsys):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    code = cli.main(
        ["create", "--as-migration", "--source-type", "AWS Batch", "--context", "x"]
    )

    assert code == 1
    assert "Pass both --source-type and --target-type together" in capsys.readouterr().err


def test_create_as_migration_fails_when_detection_cannot_infer_path(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr("keshro_cli.cli._collect_generic_discovery", lambda _: None)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("keshro_cli.cli._detect_migration_intent", lambda _: None)

    code = cli.main(["create", "--as-migration", "--context", "move things around"])

    assert code == 1
    assert "Could not determine the migration source and target" in capsys.readouterr().err


def test_find_migration_template_normalizes_apache_airflow(fake_client):
    template_key = cli._find_migration_template("AWS Batch", "Apache Airflow")

    assert template_key == "aws-batch-to-airflow"


def test_find_migration_template_normalizes_airflow_mwaa_variant(fake_client):
    template_key = cli._find_migration_template(
        "AWS Batch", "Apache Airflow (MWAA)"
    )

    assert template_key == "aws-batch-to-airflow"


def test_plan_create_saves_default_plan_automatically(fake_client, monkeypatch, capsys):
    saved = {}
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: {})
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: {})
    monkeypatch.setattr(
        "keshro_cli.cli.update_auth",
        lambda payload: saved.update(payload) or payload,
    )
    cli.main(
        [
            "plan",
            "create",
            "migration-123",
            "-T",
            "aws-batch-to-airflow",
        ]
    )
    out = capsys.readouterr().out
    assert "Saved default execution context:" in out
    assert saved["default_plan_id"]
    assert saved["default_plan_title"]


def test_config_set_saves_default_agent(monkeypatch, capsys):
    saved = {}
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: {})
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: {})
    monkeypatch.setattr(
        "keshro_cli.cli.update_auth",
        lambda payload: saved.update(payload) or saved,
    )

    code = cli.main(["config", "set", "-a", "codex"])

    assert code == 0
    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "Saved default agent: codex" in out
    assert saved["default_agent"] == "codex"


def test_plan_create_does_not_save_default_plan_in_json_mode(
    fake_client, monkeypatch, capsys
):
    saved = {}
    monkeypatch.setattr(
        "keshro_cli.cli.update_auth",
        lambda payload: saved.update(payload) or payload,
    )
    cli.main(
        [
            "--json",
            "plan",
            "create",
            "migration-123",
            "-T",
            "aws-batch-to-airflow",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == "plan-123"
    assert saved == {}


def test_migration_list_is_concise_by_default(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: {})
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: {})
    cli.main(["migration", "list"])
    out = capsys.readouterr().out
    assert "ID" in out
    assert "PATH" in out
    assert "STATUS" in out
    assert "CREATED" in out
    assert "migration-123" in out
    assert "AWS Batch -> Airflow" in out
    assert "2026-03-11T15:30:00Z" in out
    assert "partial" not in out


def test_migration_list_verbose_includes_metadata(fake_client, capsys):
    cli.main(["migration", "list", "--verbose"])
    out = capsys.readouterr().out
    assert "Outcome:" in out
    assert "partial" in out
    assert "Confidence:" in out


def test_migration_list_latest_limits_rows(fake_client, capsys):
    cli.main(["migration", "list", "--latest", "1"])
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 3
    assert "migration-123" in out[-1]


def test_migration_view_shows_detail(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_org)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_org)
    cli.main(["migration", "view", "migration-123"])
    out = capsys.readouterr().out
    assert "migration-123" in out
    assert "Overview" in out
    assert "Created:" in out
    assert "Execution progress:" in out
    assert "Execution validation:" in out
    assert "Plan:" not in out
    assert "Confidence explanation:" in out
    assert "Assessment" in out
    assert "Confidence (AI-estimated)" not in out
    assert "Effort:" in out
    assert "Cost:" in out
    assert "Risks" in out
    assert "Risks:" in out
    assert "Questions" in out
    assert "Unknowns:" in out
    assert "Checks" in out
    assert "Required validations:" in out
    assert "Next actions:" in out
    assert "Notes" in out
    assert "Steps:" in out
    assert "Links" in out
    assert "Dashboard:" in out
    assert "Plan dashboard:" not in out


def test_confirm_implicit_continue_plan_shows_dashboard_link(monkeypatch):
    monkeypatch.setattr(cli._state, "json", False)
    monkeypatch.setattr(cli, "_stdout_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_current_plan_label", lambda work_dir=None: "Plan A")
    monkeypatch.setattr(cli, "_current_app_url", lambda: "http://localhost:3000")
    monkeypatch.setattr(
        cli,
        "_load_plan_context_details",
        lambda plan_id: {
            "plan_id": plan_id,
            "plan_title": "Plan A",
            "migration_id": "",
            "kind": "plan",
        },
    )
    captured = {}

    def _confirm(message, default=True):
        captured["message"] = message
        return True

    monkeypatch.setattr(cli.typer, "confirm", _confirm)

    assert cli._confirm_implicit_continue_plan("plan-123") == "plan-123"
    assert "Continue with plan 'Plan A' (plan-123)?" in captured["message"]
    assert "Dashboard:" in captured["message"]
    assert "http://localhost:3000/plans/plan-123" in captured["message"]


def test_confirm_implicit_continue_plan_uses_migration_label_and_url(monkeypatch):
    monkeypatch.setattr(cli._state, "json", False)
    monkeypatch.setattr(cli, "_stdout_is_tty", lambda: True)
    monkeypatch.setattr(
        cli, "_current_plan_label", lambda work_dir=None: "AWS Batch to Airflow"
    )
    monkeypatch.setattr(cli, "_current_app_url", lambda: "http://localhost:3000")
    monkeypatch.setattr(
        cli,
        "_load_plan_context_details",
        lambda plan_id: {
            "plan_id": plan_id,
            "plan_title": "AWS Batch to Airflow",
            "migration_id": "mig-123",
            "kind": "migration",
        },
    )
    captured = {}

    def _confirm(message, default=True):
        captured["message"] = message
        return True

    monkeypatch.setattr(cli.typer, "confirm", _confirm)

    assert cli._confirm_implicit_continue_plan("plan-123") == "plan-123"
    assert (
        "Continue with migration 'AWS Batch to Airflow' (plan-123)?"
        in captured["message"]
    )
    assert "http://localhost:3000/migrations/mig-123" in captured["message"]


def test_confirm_implicit_continue_plan_ctrl_c_exits_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(cli._state, "json", False)
    monkeypatch.setattr(cli, "_stdout_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_current_plan_label", lambda work_dir=None: "Plan A")
    monkeypatch.setattr(cli, "_current_app_url", lambda: "http://localhost:3000")
    monkeypatch.setattr(
        cli,
        "_load_plan_context_details",
        lambda plan_id: {
            "plan_id": plan_id,
            "plan_title": "Plan A",
            "migration_id": "",
            "kind": "plan",
        },
    )

    def _confirm(message, default=True):
        raise click.Abort()

    monkeypatch.setattr(cli.typer, "confirm", _confirm)

    with pytest.raises(SystemExit) as exc:
        cli._confirm_implicit_continue_plan("plan-123")

    assert exc.value.code == 0
    assert capsys.readouterr().out.endswith("\n")


def test_main_handles_abort_without_traceback(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "app",
        lambda argv, standalone_mode=False: (_ for _ in ()).throw(click.Abort()),
    )

    assert cli.main(["continue"]) == 130
    assert capsys.readouterr().err.endswith("\n")


def test_migration_delete_hits_endpoint(fake_client, capsys):
    cli.main(["migration", "delete", "migration-123"])
    out = capsys.readouterr().out.strip()
    assert out == "Deleted migration migration-123."
    assert ("DELETE", "/v1/migrations/migration-123", None) in fake_client.calls


def test_migration_create_handles_linked_plan_fetch_request_error(capsys, monkeypatch):
    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json=None):
            assert path == "/v1/migrations"
            return _FakeResponse({"id": "mig-123", "status": "analyzing"})

        def get(self, path, params=None):
            request = httpx.Request("GET", f"http://localhost:8000{path}")
            raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(cli, "make_client", lambda api_url=None, token=None: _Client())
    monkeypatch.setattr(cli, "_state", cli._State(api_url="http://localhost:8000"))

    cli._create_migration_from_payload(
        {"source_type": "AWS Batch", "target_type": "Airflow"},
        {"source": "AWS Batch", "target": "Airflow"},
    )

    out = capsys.readouterr().out
    assert "Migration created: AWS Batch -> Airflow" in out
    assert "mig-123" in out
    assert "Dashboard: http://localhost:3000/migrations/mig-123" in out
    assert "Saved default plan:" not in out
    assert "linked execution plan" not in out
    assert "/v1/migrations/mig-123/plan" not in out


def test_migration_create_sets_default_plan_when_linked_plan_becomes_available(
    capsys, monkeypatch
):
    saved = {}

    class _Client:
        def __init__(self):
            self.plan_attempts = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json=None):
            assert path == "/v1/migrations"
            return _FakeResponse({"id": "mig-123", "status": "analyzing"})

        def get(self, path, params=None):
            if path == "/v1/migrations/mig-123/plan":
                self.plan_attempts += 1
                if self.plan_attempts < 3:
                    request = httpx.Request("GET", f"http://localhost:8000{path}")
                    response = httpx.Response(404, request=request)
                    raise httpx.HTTPStatusError(
                        "not ready", request=request, response=response
                    )
                return _FakeResponse({"id": "plan-new", "title": "New migration plan"})
            raise AssertionError(path)

    monkeypatch.setattr(cli, "make_client", lambda api_url=None, token=None: _Client())
    monkeypatch.setattr(cli, "_state", cli._State(api_url="http://localhost:8000"))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        cli,
        "_set_default_plan_after_create",
        lambda plan, announce=True: saved.update(
            {"id": plan.get("id"), "title": plan.get("title")}
        ),
    )

    cli._create_migration_from_payload(
        {"source_type": "AWS Batch", "target_type": "Airflow"},
        {"source": "AWS Batch", "target": "Airflow"},
    )

    assert saved == {"id": "plan-new", "title": "New migration plan"}


def test_create_migration_sanitizes_invalid_surrogates_in_payload(monkeypatch):
    seen = {}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json=None):
            seen["path"] = path
            seen["json"] = json
            return _FakeResponse({"id": "mig-123", "status": "analyzing"})

        def get(self, path, params=None):
            request = httpx.Request("GET", f"http://localhost:8000{path}")
            raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(cli, "make_client", lambda api_url=None, token=None: _Client())
    monkeypatch.setattr(cli, "_state", cli._State(api_url="http://localhost:8000"))

    cli._create_migration_from_payload(
        {
            "source_type": "AWS Batch",
            "target_type": "Airflow",
            "context": "bad surrogate here \udcc2",
            "custom_fields": {
                "__keshro_discovered_context": "other bad text \udcc2",
            },
        },
        {"source": "AWS Batch", "target": "Airflow"},
    )

    assert seen["path"] == "/v1/migrations"
    assert seen["json"]["context"].endswith("?")
    assert seen["json"]["custom_fields"]["__keshro_discovered_context"].endswith("?")
    assert "\udcc2" not in seen["json"]["context"]


def test_migration_create_does_not_retry_linked_plan_poll_on_non_404_http_error(
    monkeypatch,
):
    attempts = {"count": 0}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json=None):
            assert path == "/v1/migrations"
            return _FakeResponse({"id": "mig-123", "status": "analyzing"})

        def get(self, path, params=None):
            assert path == "/v1/migrations/mig-123/plan"
            attempts["count"] += 1
            request = httpx.Request("GET", f"http://localhost:8000{path}")
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    monkeypatch.setattr(cli, "make_client", lambda api_url=None, token=None: _Client())
    monkeypatch.setattr(cli, "_state", cli._State(api_url="http://localhost:8000"))
    sleeps: list[float] = []
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: sleeps.append(seconds))

    cli._create_migration_from_payload(
        {"source_type": "AWS Batch", "target_type": "Airflow"},
        {"source": "AWS Batch", "target": "Airflow"},
    )

    assert attempts["count"] == 1
    assert sleeps == []


def test_migration_list_json_outputs_machine_readable_rows(fake_client, capsys):
    cli.main(["--json", "migration", "list"])
    out = json.loads(capsys.readouterr().out)
    assert out[0]["id"] == "migration-123"
    assert out[0]["source_type"] == "AWS Batch"


def test_plan_list_is_concise_by_default(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: {})
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: {})
    monkeypatch.setattr(cli, "_format_plan_timestamp", lambda value: "Today 10:30")
    cli.main(["plan", "list"])
    out = capsys.readouterr().out
    assert "ID" in out
    assert "TITLE" in out
    assert "STATUS" in out
    assert "UPDATED" in out
    assert "plan-123" in out
    assert "AWS Batch to Airflow pilot" in out
    assert "AWS Batch -> Airflow" in out
    assert "Today 10:30" in out
    assert "2026-03-11T15:30:00Z" not in out
    assert "Pilot plan for the first DAG migration." not in out
    assert "for org" not in out


def test_plan_list_verbose_includes_summary_and_timestamp(
    fake_client, capsys, monkeypatch
):
    monkeypatch.setattr(
        cli, "_format_verbose_timestamp", lambda value: "2026-03-11 10:30 PDT"
    )
    cli.main(["plan", "list", "--verbose"])
    out = capsys.readouterr().out
    assert "Pilot plan for the first DAG migration." in out
    assert "Updated:" in out
    assert "2026-03-11 10:30 PDT" in out
    assert "2026-03-11T15:30:00Z" not in out


def test_plan_list_latest_limits_rows(fake_client, capsys):
    cli.main(["plan", "list", "--latest", "1"])
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 3
    assert "plan-123" in out[-1]


def test_plan_list_empty_state_shows_org_context(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_org)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_org)

    class _EmptyClient(_FakeClient):
        def get(self, path, params=None):
            if path == "/v1/plans":
                return _FakeResponse([])
            return super().get(path, params=params)

    monkeypatch.setattr(
        cli, "make_client", lambda api_url=None, token=None: _EmptyClient()
    )
    cli.main(["plan", "list"])
    out = capsys.readouterr().out.strip()
    assert out == "No plans found for org Demo Inc."


def test_plan_view_shows_active_org_context(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_org)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_org)
    cli.main(["plan", "view", "plan-123"])
    out = capsys.readouterr().out
    assert "for org Demo Inc" in out


def test_plan_view_is_human_readable_by_default(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: {})
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: {})
    cli.main(["plan", "view", "plan-123"])
    out = capsys.readouterr().out
    assert "AWS Batch to Airflow pilot" in out
    assert "Steps:" in out
    assert "task-id: review-schedules" in out
    assert "Owner: Unassigned" in out
    assert "Map cron schedules into DAG schedules" in out
    assert "Blocked: Waiting on environment access" in out
    assert "Artifacts:" in out
    assert "https://github.com/acme/migrations/pull/19" in out
    assert "for org" not in out


def test_task_view_shows_plan_association(fake_client, capsys):
    cli.main(["task", "view", "plan-123", "review-schedules"])
    out = capsys.readouterr().out
    assert "AWS Batch to Airflow pilot" in out
    assert "plan-123" in out
    assert "Review EventBridge schedules" in out
    assert "review-schedules" in out


def test_task_delete_accepts_plan_id_option(fake_client, capsys):
    cli.main(["--json", "task", "delete", "task-456", "-p", "plan-123"])
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == "plan-123"
    assert out["plan_steps"] == []


def test_plan_task_delete_uses_saved_plan_context(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    monkeypatch.setattr("typer.confirm", lambda *args, **kwargs: True)
    cli.main(["plan", "task", "delete", "task-456"])
    out = capsys.readouterr().out
    assert "Deleted task task-456 from plan plan-123." in out


def test_plan_task_view_uses_saved_plan_context(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    cli.main(["plan", "task", "view", "review-schedules"])
    out = capsys.readouterr().out
    assert "plan-123" in out
    assert "review-schedules" in out


def test_plan_delete_calls_delete_endpoint(fake_client, capsys):
    cli.main(["--json", "plan", "delete", "plan-123"])
    out = json.loads(capsys.readouterr().out)
    assert out["success"] is True
    assert out["path"] == "/v1/plans/plan-123"


def test_json_flag_can_appear_after_subcommand(fake_client, capsys):
    cli.main(["plan", "list", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out[0]["id"] == "plan-123"


def test_config_set_persists_default_org_by_id(monkeypatch, capsys):
    monkeypatch.setattr(
        "keshro_cli.cli.update_auth",
        lambda payload: {"api_url": "http://localhost:8000", **payload},
    )
    code = cli.main(["config", "set", "--org-id", "org-123"])
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == "Saved default context: org-123"


def test_config_set_can_resolve_default_org_by_name(fake_client, monkeypatch, capsys):
    monkeypatch.setattr(
        "keshro_cli.cli.update_auth",
        lambda payload: {"api_url": "http://localhost:8000", **payload},
    )
    code = cli.main(["config", "set", "--org", "Acme"])
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == "Saved default context: Acme"


def test_config_set_can_resolve_default_org_by_partial_name(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setattr(
        "keshro_cli.cli.update_auth",
        lambda payload: {"api_url": "http://localhost:8000", **payload},
    )
    code = cli.main(["config", "set", "--org", "demo"])
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == "Saved default context: Demo Inc"


def test_config_set_can_save_default_plan(fake_client, monkeypatch, capsys):
    monkeypatch.setattr(
        "keshro_cli.cli.update_auth",
        lambda payload: {"api_url": "http://localhost:8000", **payload},
    )
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr(
        "keshro_cli.cli._link_current_repo_to_plan", lambda *args, **kwargs: True
    )
    code = cli.main(["config", "set", "-p", "plan-123"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Saved default context: personal" in out
    assert "Saved default execution context: AWS Batch to Airflow pilot" in out
    assert "Linked the current repo to this execution context in Keshro." in out


def test_require_plan_context_can_resolve_repo_link(monkeypatch):
    monkeypatch.setattr(
        "keshro_cli.cli.load_auth",
        lambda: {
            "api_url": "http://localhost:8000",
            "token": "jwt-123",
        },
    )
    monkeypatch.setattr("keshro_cli.cli.update_auth", lambda payload: payload)

    class _ResolveClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, params=None, headers=None, timeout=None):
            if path == "/v1/plans/repo-link/resolve":
                assert params["repo_root"] == "/tmp/demo"
                return _FakeResponse({"plan_id": "plan-123"})
            if path == "/v1/plans/plan-123":
                return _FakeResponse(
                    {"id": "plan-123", "title": "AWS Batch to Airflow pilot"}
                )
            raise AssertionError(path)

    def _fake_run(cmd, cwd=None, capture_output=None, text=None, check=None):
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="/tmp/demo\n", stderr="")
        if cmd == ["git", "config", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="git@github.com:acme/demo.git\n", stderr=""
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(
        "keshro_cli.cli.make_client", lambda api_url=None, token=None: _ResolveClient()
    )
    monkeypatch.setattr("keshro_cli.cli.subprocess.run", _fake_run)

    assert cli._require_plan_context(None) == "plan-123"


def test_current_plan_id_prefers_repo_resolution_before_cached_default(monkeypatch):
    monkeypatch.setattr(
        "keshro_cli.cli.load_auth",
        lambda: {
            "default_plan_id": "plan-cached",
            "default_plan_title": "Cached plan",
        },
    )
    saved = {}

    monkeypatch.setattr(
        "keshro_cli.cli.update_auth", lambda payload: saved.update(payload) or payload
    )
    monkeypatch.setattr(
        "keshro_cli.cli._resolve_repo_linked_plan",
        lambda *args, **kwargs: ("plan-linked", "Repo linked plan"),
    )

    assert cli._current_plan_id(None) == "plan-linked"
    assert saved["default_plan_id"] == "plan-linked"
    assert saved["default_plan_title"] == "Repo linked plan"


def test_current_plan_id_resolves_explicit_migration_id(monkeypatch):
    monkeypatch.setattr(
        "keshro_cli.cli._resolve_plan_or_migration_context",
        lambda value: ("plan-123", "AWS Batch to Airflow pilot")
        if value == "migration-123"
        else (None, None),
    )

    assert cli._current_plan_id("migration-123") == "plan-123"


def test_get_plan_or_exit_clears_stale_cached_default_on_404(monkeypatch):
    saved = {}
    monkeypatch.setattr(
        "keshro_cli.cli.load_auth",
        lambda: {
            "default_plan_id": "plan-stale",
            "default_plan_title": "Stale plan",
        },
    )
    monkeypatch.setattr(
        "keshro_cli.cli._resolve_repo_linked_plan",
        lambda *args, **kwargs: (None, None),
    )
    monkeypatch.setattr(
        "keshro_cli.cli.update_auth", lambda payload: saved.update(payload) or payload
    )

    class _404Response:
        status_code = 404

        def __init__(self):
            self.request = httpx.Request(
                "GET", "http://localhost:8000/v1/plans/plan-stale"
            )

        def json(self):
            return {"detail": "Plan not found"}

    class _MissingPlanClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, params=None, headers=None, timeout=None):
            response = _404Response()
            raise httpx.HTTPStatusError(
                "Plan not found", request=response.request, response=response
            )

    monkeypatch.setattr(
        "keshro_cli.cli.make_client",
        lambda api_url=None, token=None: _MissingPlanClient(),
    )

    with pytest.raises(httpx.HTTPStatusError):
        cli._get_plan_or_exit(None)

    assert saved["default_plan_id"] is None
    assert saved["default_plan_title"] is None


def test_install_codex_integration_replaces_existing_managed_block(
    monkeypatch, tmp_path
):
    """Codex integration should replace old (un-versioned or stale-versioned)
    keshro blocks with the current versioned block."""
    monkeypatch.setattr("keshro_cli.cli.CODEX_HOME_DIR", tmp_path)
    target = tmp_path / "AGENTS.md"
    # Simulate an old un-versioned marker from a previous install
    old_block = (
        "<!-- keshro-agent-instructions -->\n"
        "# Keshro Integration\n\n"
        "OLD CONTENT\n"
        "<!-- keshro-agent-instructions -->\n"
    )
    target.write_text("Existing intro\n\n" + old_block + "\nExisting footer\n")

    cli._install_codex_integration()

    content = target.read_text()
    from keshro_cli import __version__

    assert "OLD CONTENT" not in content
    assert "Existing intro" in content
    assert "Existing footer" in content
    assert f"v{__version__}" in content
    assert "TRIGGER when:" in content


def test_install_codex_integration_replaces_old_versioned_block(
    monkeypatch, tmp_path
):
    """Codex integration should replace a block from an older CLI version."""
    monkeypatch.setattr("keshro_cli.cli.CODEX_HOME_DIR", tmp_path)
    target = tmp_path / "AGENTS.md"
    old_block = (
        "<!-- keshro-agent-instructions v0.0.1 -->\n"
        "# Keshro Integration\n\n"
        "OLD VERSION CONTENT\n"
        "<!-- keshro-agent-instructions v0.0.1 -->\n"
    )
    target.write_text("Intro\n\n" + old_block + "\nFooter\n")

    cli._install_codex_integration()

    content = target.read_text()
    from keshro_cli import __version__

    assert "OLD VERSION CONTENT" not in content
    assert "v0.0.1" not in content
    assert f"v{__version__}" in content
    assert "Intro" in content
    assert "Footer" in content


def test_maybe_refresh_codex_updates_stale_version(monkeypatch, tmp_path):
    """Auto-refresh should rewrite Codex AGENTS.md when version is stale."""
    monkeypatch.setattr("keshro_cli.cli.CODEX_HOME_DIR", tmp_path)
    target = tmp_path / "AGENTS.md"
    old_block = (
        "<!-- keshro-agent-instructions v0.0.1 -->\n"
        "# Keshro Integration\n\n"
        "STALE CONTENT\n"
        "<!-- keshro-agent-instructions v0.0.1 -->\n"
    )
    target.write_text(old_block)

    cli._maybe_refresh_codex()

    content = target.read_text()
    from keshro_cli import __version__

    assert "STALE CONTENT" not in content
    assert f"v{__version__}" in content
    assert "TRIGGER when:" in content


def test_maybe_refresh_codex_skips_when_current(monkeypatch, tmp_path):
    """Auto-refresh should not touch AGENTS.md if version already matches."""
    monkeypatch.setattr("keshro_cli.cli.CODEX_HOME_DIR", tmp_path)
    target = tmp_path / "AGENTS.md"
    # Install current version first
    cli._install_codex_integration()
    mtime_before = target.stat().st_mtime

    cli._maybe_refresh_codex()

    assert target.stat().st_mtime == mtime_before


def test_maybe_refresh_codex_skips_when_no_file(monkeypatch, tmp_path):
    """Auto-refresh should not create AGENTS.md if it doesn't exist."""
    monkeypatch.setattr("keshro_cli.cli.CODEX_HOME_DIR", tmp_path)
    tmp_path.mkdir(exist_ok=True)

    cli._maybe_refresh_codex()

    assert not (tmp_path / "AGENTS.md").exists()


def test_setup_all_reports_already_present_integrations(monkeypatch, capsys):
    monkeypatch.setattr(
        "keshro_cli.cli._install_agent_integrations",
        lambda silent=True: (
            [],
            ["Claude Code: /tmp/keshro.md", "Codex: /tmp/codex.md"],
        ),
    )

    cli.main(["setup"])
    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "All agent integrations already installed." in out


def test_config_set_can_save_api_url(monkeypatch, capsys):
    monkeypatch.setattr(
        "keshro_cli.cli.update_auth",
        lambda payload: {"default_org_id": None, "default_org_name": None, **payload},
    )
    code = cli.main(["config", "set", "--api-url", "https://api.keshro.test"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Saved default context: personal" in out
    assert "Saved API URL: https://api.keshro.test" in out


def test_config_set_short_alias_can_save_api_url(monkeypatch, capsys):
    monkeypatch.setattr(
        "keshro_cli.cli.update_auth",
        lambda payload: {"default_org_id": None, "default_org_name": None, **payload},
    )
    code = cli.main(["config", "set", "-u", "https://api.keshro.test"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Saved API URL: https://api.keshro.test" in out


def test_plan_create_from_claude_markdown_parses_steps(
    fake_client, tmp_path: Path, capsys
):
    source = tmp_path / "claude.md"
    source.write_text(
        "# AWS Batch to Airflow\n\n"
        "- Inventory Batch jobs\n"
        "- Map schedules to DAGs\n"
    )
    cli.main(
        [
            "--json",
            "plan",
            "create",
            "migration-123",
            "-c",
            str(source),
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == "plan-123"
    assert out["title"] == "AWS Batch to Airflow"
    assert out["migration_id"] == "migration-123"
    assert out["import_source"] == "claude"
    assert len(out["plan_steps"]) == 2


def test_plan_create_accepts_positional_migration_id(fake_client, capsys):
    cli.main(
        [
            "--json",
            "plan",
            "create",
            "migration-123",
            "--title",
            "AWS Batch to Airflow",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["migration_id"] == "migration-123"
    assert out["title"] == "AWS Batch to Airflow"


def test_task_plan_posts_task_update(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "plan",
            "plan-123",
            "--title",
            "Validate pilot DAG",
            "--description",
            "Run the pilot DAG end to end",
            "--blocked-reason",
            "Waiting on staging DAG deployment",
            "--link",
            "https://linear.app/acme/issue/ENG-42",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == "plan-123"
    assert out["plan_steps"][0]["title"] == "Validate pilot DAG"
    assert out["plan_steps"][0]["blocked_reason"] == "Waiting on staging DAG deployment"
    assert out["plan_steps"][0]["artifact_links"] == [
        "https://linear.app/acme/issue/ENG-42"
    ]


def test_task_edit_patches_existing_task(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "edit",
            "plan-123",
            "task-456",
            "-s",
            "in_progress",
            "-o",
            "Platform team",
            "-l",
            "https://github.com/acme/migrations/pull/19",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/v1/plans/plan-123/tasks/task-456"
    assert out["payload"]["status"] == "in_progress"
    assert out["payload"]["artifact_links"] == [
        "https://github.com/acme/migrations/pull/19"
    ]


def test_task_edit_accepts_blocked_reason_short_alias_r(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "edit",
            "plan-123",
            "task-456",
            "-s",
            "blocked",
            "-r",
            "Waiting on Airflow access",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/v1/plans/plan-123/tasks/task-456"
    assert out["payload"]["status"] == "blocked"
    assert out["payload"]["blocked_reason"] == "Waiting on Airflow access"


def test_task_edit_accepts_feedback_reason(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "edit",
            "plan-123",
            "task-456",
            "--reason",
            "Needed a more implementation-specific task",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert (
        out["payload"]["feedback_reason"]
        == "Needed a more implementation-specific task"
    )


def test_task_edit_uses_saved_plan_context(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    cli.main(
        [
            "--json",
            "task",
            "edit",
            "task-456",
            "--status",
            "in_progress",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/v1/plans/plan-123/tasks/task-456"
    assert out["payload"]["status"] == "in_progress"


def test_task_edit_accepts_plan_id_option(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "edit",
            "task-456",
            "--plan-id",
            "plan-123",
            "--status",
            "in_progress",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/v1/plans/plan-123/tasks/task-456"


def test_plan_next_returns_first_actionable_task(fake_client, capsys):
    cli.main(["--json", "plan", "next", "plan-123"])
    out = json.loads(capsys.readouterr().out)
    assert out["plan_id"] == "plan-123"
    assert out["task"]["id"] == "review-schedules"


def test_task_start_marks_task_in_progress(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "start",
            "plan-123",
            "task-456",
            "--notes",
            "Starting the pilot implementation",
            "--reason",
            "Top priority after plan review",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/v1/plans/plan-123/tasks/task-456"
    assert out["payload"]["status"] == "in_progress"
    assert "Starting the pilot implementation" in out["payload"]["notes"]
    assert "[20" in out["payload"]["notes"]
    assert out["payload"]["feedback_reason"] == "Top priority after plan review"


def test_task_next_returns_next_actionable_task(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "next",
            "--plan-id",
            "plan-123",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["plan_id"] == "plan-123"
    assert out["task"]["id"] == "review-schedules"


def test_task_done_marks_task_completed(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "done",
            "plan-123",
            "task-456",
            "--notes",
            "Pilot DAG merged and validated",
            "--link",
            "https://github.com/acme/migrations/pull/21",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/v1/plans/plan-123/tasks/task-456"
    assert out["payload"]["status"] == "completed"
    assert "Pilot DAG merged and validated" in out["payload"]["notes"]
    assert "[20" in out["payload"]["notes"]
    assert out["payload"]["artifact_links"] == [
        "https://github.com/acme/migrations/pull/21"
    ]
    assert out["payload"]["blocked_reason"] == ""


def test_task_done_requires_completion_evidence_when_acceptance_criteria_exist(
    fake_client, monkeypatch, capsys
):
    original_get = fake_client.get

    def _get(path, params=None):
        response = original_get(path, params=params)
        if path != "/v1/plans/plan-123":
            return response
        plan = response.json()
        enriched_steps = []
        for step in plan["plan_steps"]:
            if step["id"] == "task-456":
                enriched_steps.append(
                    {
                        **step,
                        "acceptance_criteria": [
                            "DAG syntax validates without errors",
                            "Retry logic implemented",
                        ],
                        "discovery_commands": [
                            "airflow dags check dags/",
                            "python -m py_compile dags/*.py",
                        ],
                    }
                )
            else:
                enriched_steps.append(step)
        return _FakeResponse({**plan, "plan_steps": enriched_steps})

    monkeypatch.setattr(fake_client, "get", _get)

    code = cli.main(
        [
            "--json",
            "task",
            "done",
            "plan-123",
            "task-456",
            "--notes",
            "Pilot DAG merged and validated",
        ]
    )
    assert code != 0
    err = ANSI_RE.sub("", capsys.readouterr().err)
    assert "Acceptance criteria met:" in err
    assert "Verification:" in err


def test_task_done_accepts_completion_evidence_when_acceptance_criteria_exist(
    fake_client, monkeypatch, capsys
):
    original_get = fake_client.get

    def _get(path, params=None):
        response = original_get(path, params=params)
        if path != "/v1/plans/plan-123":
            return response
        plan = response.json()
        enriched_steps = []
        for step in plan["plan_steps"]:
            if step["id"] == "task-456":
                enriched_steps.append(
                    {
                        **step,
                        "acceptance_criteria": [
                            "DAG syntax validates without errors",
                            "Retry logic implemented",
                        ],
                        "discovery_commands": [
                            "airflow dags check dags/",
                            "python -m py_compile dags/*.py",
                        ],
                    }
                )
            else:
                enriched_steps.append(step)
        return _FakeResponse({**plan, "plan_steps": enriched_steps})

    monkeypatch.setattr(fake_client, "get", _get)

    cli.main(
        [
            "--json",
            "task",
            "done",
            "plan-123",
            "task-456",
            "--notes",
            "Files created: dags/daily_sales_pipeline.py | Acceptance criteria met: DAG syntax validates without errors; retry logic implemented | Verification: airflow dags check dags/ passed; python -m py_compile dags/*.py passed",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["payload"]["status"] == "completed"
    assert "Acceptance criteria met:" in out["payload"]["notes"]
    assert "Verification:" in out["payload"]["notes"]


def test_task_block_marks_task_blocked(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "block",
            "plan-123",
            "task-456",
            "--reason",
            "Waiting on Terraform IAM role changes",
            "--feedback-reason",
            "Access blocker discovered during pilot setup",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/v1/plans/plan-123/tasks/task-456"
    assert out["payload"]["status"] == "blocked"
    assert out["payload"]["blocked_reason"] == "Waiting on Terraform IAM role changes"
    assert (
        out["payload"]["feedback_reason"]
        == "Access blocker discovered during pilot setup"
    )


def test_task_unblock_clears_blocked_reason(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "unblock",
            "plan-123",
            "task-456",
            "--notes",
            "Terraform role applied; resuming pilot",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/v1/plans/plan-123/tasks/task-456"
    assert out["payload"]["status"] == "in_progress"
    assert out["payload"]["blocked_reason"] == ""
    assert "Terraform role applied; resuming pilot" in out["payload"]["notes"]
    assert "[20" in out["payload"]["notes"]


def test_task_done_appends_to_existing_notes(fake_client, monkeypatch, capsys):
    original_get_plan = cli._get_plan_or_exit

    def _plan_with_existing_notes(plan_id: str):
        plan = original_get_plan(plan_id)
        plan["plan_steps"][1][
            "notes"
        ] = "[2026-03-15 16:00 UTC] Existing execution note"
        return plan

    monkeypatch.setattr(cli, "_get_plan_or_exit", _plan_with_existing_notes)

    cli.main(
        [
            "--json",
            "task",
            "done",
            "plan-123",
            "task-456",
            "--notes",
            "Pilot DAG merged and validated",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert "Existing execution note" in out["payload"]["notes"]
    assert "Pilot DAG merged and validated" in out["payload"]["notes"]


def test_task_start_human_output_is_compact(fake_client, capsys):
    cli.main(
        [
            "task",
            "start",
            "plan-123",
            "task-456",
            "--notes",
            "Starting the pilot implementation",
        ]
    )
    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "Updated task" in out
    assert "[in_progress]." in out
    assert "Notes updated" in out
    assert "Plan ID:" not in out
    assert "Task ID:" not in out


def test_task_note_appends_timestamped_note(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "note",
            "plan-123",
            "task-456",
            "--note",
            "Airflow will orchestrate Batch during the pilot",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/v1/plans/plan-123/tasks/task-456"
    assert "Airflow will orchestrate Batch during the pilot" in out["payload"]["notes"]
    assert "[20" in out["payload"]["notes"]


def test_task_artifact_appends_link_without_overwriting(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "artifact",
            "plan-123",
            "task-456",
            "--link",
            "https://github.com/acme/migrations/pull/99",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/v1/plans/plan-123/tasks/task-456"
    assert out["payload"]["artifact_links"] == [
        "https://github.com/acme/migrations/pull/99"
    ]


def test_plan_replan_notes_appends_summary(fake_client, capsys):
    cli.main(
        [
            "--json",
            "plan",
            "replan-notes",
            "Need to keep Batch as the runtime while Airflow takes over orchestration.",
            "--plan-id",
            "plan-123",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/v1/plans/plan-123"
    assert "Pilot plan for the first DAG migration." in out["payload"]["summary"]
    assert (
        "Need to keep Batch as the runtime while Airflow takes over orchestration."
        in out["payload"]["summary"]
    )


def test_task_delete_accepts_feedback_reason(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "delete",
            "plan-123",
            "task-456",
            "--reason",
            "not_relevant",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["payload"]["feedback_reason"] == "not_relevant"


def test_task_delete_prompts_before_removing_linked_issue(
    fake_client, capsys, monkeypatch
):
    seen = {}

    def fake_confirm(message, abort=False):
        seen["message"] = message
        seen["abort"] = abort
        return True

    monkeypatch.setattr("typer.confirm", fake_confirm)
    cli.main(["task", "delete", "plan-123", "task-456"])
    out = capsys.readouterr().out
    assert "Deleted task task-456 from plan plan-123." in out
    assert "linked Linear issue KES-42" in seen["message"]
    assert seen["abort"] is True


def test_task_delete_yes_skips_confirmation(fake_client, capsys, monkeypatch):
    def fail_confirm(*args, **kwargs):
        raise AssertionError("confirm should not be called")

    monkeypatch.setattr("typer.confirm", fail_confirm)
    cli.main(["task", "delete", "plan-123", "task-456", "--yes"])
    out = capsys.readouterr().out
    assert "Deleted task task-456 from plan plan-123." in out


def test_task_plan_human_output_shows_owner(fake_client, capsys):
    cli.main(
        [
            "task",
            "plan",
            "plan-123",
            "--title",
            "Validate pilot DAG",
            "--description",
            "Run the pilot DAG end to end",
            "--owner",
            "Platform team",
        ]
    )
    out = capsys.readouterr().out
    assert "Task:" in out
    assert "Task ID:" in out
    assert "task-999" in out
    assert "Platform team" in out


def test_task_next_uses_saved_plan_context(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    cli.main(["task", "next"])
    method, path, _ = fake_client.calls[-1]
    assert method == "GET"
    assert path == "/v1/plans/plan-123"


def test_plan_replan_notes_accepts_plan_id_option(fake_client, capsys):
    cli.main(
        [
            "--json",
            "plan",
            "replan-notes",
            "--plan-id",
            "plan-123",
            "Pilot DAG completed",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/v1/plans/plan-123"
    assert "Pilot DAG completed" in out["payload"]["summary"]


def test_task_edit_without_plan_context_fails(capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: {})
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: {})
    code = cli.main(["task", "edit", "task-456", "--status", "in_progress"])
    assert code == 1
    assert "Execution context or migration ID required" in capsys.readouterr().err


def test_migration_history_uses_plan_audit_trail(fake_client, capsys):
    cli.main(["migration", "history", "migration-123"])
    out = capsys.readouterr().out
    assert "Audit Trail:" in out
    assert "Translate pilot DAG" in out
    assert "Pilot scope changed after DAG review" in out


def test_migration_history_json(fake_client, capsys):
    cli.main(["--json", "migration", "history", "migration-123"])
    out = json.loads(capsys.readouterr().out)
    assert out["migration_id"] == "migration-123"
    assert out["plan_id"] == "plan-123"
    assert out["task_feedback_events"][0]["task_id"] == "task-456"


def test_config_prints_saved_auth_metadata(monkeypatch, capsys):
    class _ConfigClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, params=None):
            if path == "/v1/auth/me":
                return _FakeResponse({"email": "cli@example.com", "id": "user-1"})
            if path == "/v1/orgs":
                return _FakeResponse([])
            raise AssertionError(path)

    monkeypatch.setattr(
        "keshro_cli.cli.load_auth",
        lambda: {
            "api_url": "https://app.keshro.test",
            "token": "jwt-123",
            "user": {"email": "cli@example.com", "name": "CLI User"},
        },
    )
    monkeypatch.setattr(
        "keshro_cli.cli.make_client", lambda api_url=None, token=None: _ConfigClient()
    )

    cli.main(["config"])
    out = capsys.readouterr().out
    assert "API URL:" in out
    assert "https://app.keshro.test" in out
    assert "Authenticated:" in out
    assert "yes" in out
    assert "User:" in out
    assert "cli@example.com" in out


def test_config_prints_org_memberships(fake_client, monkeypatch, capsys):
    monkeypatch.setattr(
        "keshro_cli.cli.load_auth",
        lambda: {
            "api_url": "https://app.keshro.test",
            "token": "jwt-123",
            "user": {"email": "cli@example.com", "name": "CLI User"},
        },
    )

    cli.main(["config"])
    out = capsys.readouterr().out
    assert "Organizations:" in out
    assert "Acme" in out
    assert "Demo Inc" in out


def test_config_prints_current_repo_migration_context(fake_client, monkeypatch, capsys):
    monkeypatch.setattr(
        "keshro_cli.cli.load_auth",
        lambda: {
            "api_url": "http://localhost:8000",
            "token": "jwt-123",
            "default_plan_id": "plan-123",
            "default_plan_title": "AWS Batch to Airflow",
            "user": {"email": "cli@example.com", "name": "CLI User"},
        },
    )
    monkeypatch.setattr(
        "keshro_cli.cli._resolve_repo_linked_plan",
        lambda work_dir=None: ("plan-123", "AWS Batch to Airflow"),
    )

    cli.main(["config"])
    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "Current repo migration:" in out
    assert "Migration URL:" in out
    assert "/migrations/migration-123" in out


def test_auth_login_with_token_prints_human_text_by_default(monkeypatch, capsys):
    saved = {}

    class _AuthClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, headers=None):
            assert path == "/v1/auth/me"
            assert headers == {"Authorization": "Bearer ksh_pat_test"}
            return _FakeResponse({"email": "cli@example.com", "id": "user-1"})

    monkeypatch.setattr("keshro_cli.auth.httpx.Client", lambda **kwargs: _AuthClient())
    monkeypatch.setattr(
        "keshro_cli.auth.save_auth", lambda payload: saved.update(payload)
    )
    monkeypatch.setattr(
        "keshro_cli.cli._install_claude_integration",
        lambda: Path("/tmp/keshro.md"),
    )
    monkeypatch.setattr(
        "keshro_cli.cli._install_codex_integration",
        lambda: Path("/tmp/codex-AGENTS.md"),
    )

    cli.main(["login", "ksh_pat_test"])
    out = capsys.readouterr().out
    assert "Logged in to Keshro as cli@example.com." in out
    assert "Claude Code: /tmp/keshro.md" in out
    assert "Codex: /tmp/codex-AGENTS.md" in out
    assert saved["token"] == "ksh_pat_test"


def test_auth_login_with_token_validates_with_auth_me(monkeypatch, capsys):
    saved = {}

    class _AuthClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, headers=None):
            assert path == "/v1/auth/me"
            assert headers == {"Authorization": "Bearer ksh_pat_test"}
            return _FakeResponse({"email": "cli@example.com", "id": "user-1"})

    monkeypatch.setattr("keshro_cli.auth.httpx.Client", lambda **kwargs: _AuthClient())
    monkeypatch.setattr(
        "keshro_cli.auth.save_auth", lambda payload: saved.update(payload)
    )
    monkeypatch.setattr(
        "keshro_cli.cli._install_claude_integration",
        lambda: Path("/tmp/keshro.md"),
    )
    monkeypatch.setattr(
        "keshro_cli.cli._install_codex_integration",
        lambda: Path("/tmp/codex-AGENTS.md"),
    )

    cli.main(["--json", "login", "ksh_pat_test"])
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert saved["token"] == "ksh_pat_test"


def test_config_json_outputs_machine_readable_metadata(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setattr(
        "keshro_cli.cli.load_auth",
        lambda: {
            "api_url": "https://app.keshro.test",
            "token": "jwt-123",
            "user": {"email": "cli@example.com"},
        },
    )

    cli.main(["--json", "config"])
    out = json.loads(capsys.readouterr().out)
    assert out["api_url"] == "https://app.keshro.test"
    assert out["authenticated"] is True
    assert out["user"]["email"] == "cli@example.com"
    assert len(out["orgs"]) == 2


def test_config_marks_stale_token_as_not_authenticated(monkeypatch, capsys):
    class _UnauthorizedResponse:
        def raise_for_status(self):
            request = httpx.Request("GET", "https://app.keshro.test/api/auth/me")
            response = httpx.Response(
                401, request=request, json={"detail": "Authentication required"}
            )
            raise httpx.HTTPStatusError(
                "Client error '401 Unauthorized' for url",
                request=request,
                response=response,
            )

        def json(self):
            return {"detail": "Authentication required"}

    class _StaleClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, params=None):
            assert path == "/v1/auth/me"
            return _UnauthorizedResponse()

    monkeypatch.setattr(
        "keshro_cli.cli.load_auth",
        lambda: {
            "api_url": "https://app.keshro.test",
            "token": "jwt-stale",
            "user": {"email": "cli@example.com", "name": "CLI User"},
        },
    )
    monkeypatch.setattr(
        "keshro_cli.cli.make_client", lambda api_url=None, token=None: _StaleClient()
    )

    cli.main(["config"])
    out = capsys.readouterr().out
    assert "Authenticated:" in out
    assert "no" in out


def test_auth_login_requires_token_argument(monkeypatch, capsys):
    monkeypatch.setattr("keshro_cli.auth.load_auth", lambda: {})
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    code = cli.main(["login"])
    captured = capsys.readouterr()
    assert code == 1
    cleaned = ANSI_RE.sub("", captured.err)
    assert "Usage: keshro login <api-token>" in cleaned
    assert "authenticate via browser" in cleaned
    assert "Account -> API" in cleaned


def test_auth_login_without_token_reuses_saved_session(monkeypatch, capsys):
    monkeypatch.setattr(
        "keshro_cli.auth.load_auth",
        lambda: {
            "api_url": "https://app.keshro.test",
            "token": "jwt-123",
            "user": {"email": "cli@example.com"},
        },
    )

    class _AuthClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, headers=None):
            assert path == "/v1/auth/me"
            assert headers == {"Authorization": "Bearer jwt-123"}
            return _FakeResponse({"email": "cli@example.com", "id": "user-1"})

    monkeypatch.setattr("keshro_cli.auth.httpx.Client", lambda **kwargs: _AuthClient())

    cli.main(["login"])
    out = capsys.readouterr().out.strip()
    assert out == "Already logged in to Keshro as cli@example.com."


def test_auth_login_without_token_reports_expired_saved_session(monkeypatch, capsys):
    monkeypatch.setattr(
        "keshro_cli.auth.load_auth",
        lambda: {
            "api_url": "https://app.keshro.test",
            "token": "jwt-stale",
            "user": {"email": "cli@example.com"},
        },
    )

    class _UnauthorizedClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, headers=None):
            request = httpx.Request("GET", "https://app.keshro.test/api/auth/me")
            response = httpx.Response(
                401, request=request, json={"detail": "Authentication required"}
            )
            raise httpx.HTTPStatusError(
                "Client error '401 Unauthorized' for url",
                request=request,
                response=response,
            )

    monkeypatch.setattr(
        "keshro_cli.auth.httpx.Client", lambda **kwargs: _UnauthorizedClient()
    )
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)

    code = cli.main(["login"])
    captured = capsys.readouterr()
    assert code == 1
    cleaned = ANSI_RE.sub("", captured.err)
    assert "keshro login <api-token>" in cleaned
    assert "authenticate via browser" in cleaned
    assert "Account -> API" in cleaned


def test_http_404_errors_render_cleanly(monkeypatch, capsys):
    class _ErrorClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json=None):
            request = httpx.Request("POST", f"http://localhost:8000{path}")
            response = httpx.Response(
                404, request=request, json={"detail": "Template not found"}
            )
            raise httpx.HTTPStatusError("not found", request=request, response=response)

    monkeypatch.setattr(
        cli, "make_client", lambda api_url=None, token=None: _ErrorClient()
    )

    code = cli.main(
        [
            "plan",
            "create",
            "migration-123",
            "--from-template",
            "missing-template",
        ]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert (
        ANSI_RE.sub("", captured.err).strip()
        == "Keshro API error (404): Template not found"
    )


def test_plan_create_requires_migration_id(fake_client, capsys):
    code = cli.main(["plan", "create", "--title", "Manual plan"])
    captured = capsys.readouterr()
    assert code == 2
    assert (
        "Migration ID is required. Run `keshro plan create <migration-id>`."
        in captured.err
    )


def test_plan_create_allows_migration_seed_without_title(monkeypatch, capsys):
    seen = {}

    class _CreatePlanClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json=None):
            seen["path"] = path
            seen["json"] = json
            return _FakeResponse(
                {
                    "id": "plan-123",
                    "title": "Heroku to AWS",
                    "status": "draft",
                    "source_type": "Heroku",
                    "target_type": "AWS",
                    "summary": "Execution plan derived from the migration analysis.",
                    "migration_id": "migration-123",
                    "plan_steps": [],
                    "external_links": [],
                    "updated_at": "2026-03-12T00:00:00",
                }
            )

    monkeypatch.setattr(
        cli, "make_client", lambda api_url=None, token=None: _CreatePlanClient()
    )
    monkeypatch.setattr(cli, "_set_default_plan_after_create", lambda created: None)

    cli.main(["plan", "create", "migration-123"])
    captured = capsys.readouterr()
    assert captured.err == ""
    assert seen["path"] == "/v1/plans"
    assert seen["json"]["migration_id"] == "migration-123"
    assert seen["json"]["title"] is None
    assert seen["json"]["import_source"] == "analysis"


def test_request_errors_render_connection_help(monkeypatch, capsys):
    class _OfflineClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, params=None):
            request = httpx.Request("GET", f"http://localhost:8000{path}")
            raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(
        cli, "make_client", lambda api_url=None, token=None: _OfflineClient()
    )
    monkeypatch.setattr(
        "keshro_cli.cli.load_auth",
        lambda: {"api_url": "http://localhost:8000", "token": "jwt-123"},
    )
    monkeypatch.setattr(
        "keshro_cli.client.load_auth",
        lambda: {"api_url": "http://localhost:8000", "token": "jwt-123"},
    )

    code = cli.main(["templates"])
    captured = capsys.readouterr()
    assert code == 1
    assert "Could not reach Keshro at http://localhost:8000." in captured.err


# ---------------------------------------------------------------------------
# plan push / sync-pull / generate enrichment / status cost tests
# ---------------------------------------------------------------------------


def test_plan_push(fake_client, capsys, monkeypatch):
    auth = {**_auth_with_plan(), "token": "ksh_pat_test"}
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: auth)
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: auth)
    code = cli.main(["plan", "push", "-p", "plan-123", "--provider", "linear"])
    captured = capsys.readouterr()
    assert code == 0
    assert "3 issue(s) created" in captured.out
    assert "1 updated" in captured.out


def test_plan_sync_pull(fake_client, capsys, monkeypatch):
    _auth = {**_auth_with_plan(), "token": "ksh_pat_test"}
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: _auth)
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: _auth)
    code = cli.main(["plan", "sync-pull", "-p", "plan-123"])
    captured = capsys.readouterr()
    assert code == 0
    assert "2 task(s) updated" in captured.out
    assert "KES-101" in captured.out


def test_plan_generate_shows_enrichment(fake_client, capsys, monkeypatch):
    _auth = {**_auth_with_plan(), "token": "ksh_pat_test"}
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: _auth)
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: _auth)
    monkeypatch.setattr("keshro_cli.cli.update_auth", lambda payload: payload)
    original_post = fake_client.post

    def _post_gen(path, json=None, timeout=None):
        if "/generate" in path:
            return _FakeResponse(
                {
                    "id": "plan-gen-1",
                    "title": "Auth Refactor",
                    "status": "draft",
                    "plan_steps": [
                        {
                            "order": 1,
                            "title": "Update auth",
                            "depends_on": [],
                            "risk_level": "high",
                        },
                    ],
                    "enrichment_sources": [
                        {"name": "Greptile", "description": "Codebase analysis"},
                    ],
                    "decisions": {
                        "confidence_score": 82,
                        "risks": [{"severity": "high", "description": "Auth"}],
                    },
                }
            )
        return original_post(path, json=json, timeout=timeout)

    fake_client.post = _post_gen
    code = cli.main(["plan", "generate", "Refactor auth module"])
    captured = capsys.readouterr()
    out = ANSI_RE.sub("", captured.out)
    assert code == 0
    assert "Greptile" in out
    assert "confidence: 82%" in out
    assert "[high risk]" in out


def test_status_shows_cost_summary(fake_client, capsys, monkeypatch):
    _auth = {**_auth_with_plan(), "token": "ksh_pat_test"}
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: _auth)
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: _auth)
    original_get = fake_client.get

    def _get_cost(path, params=None, timeout=None):
        if path == "/v1/plans/plan-123":
            return _FakeResponse(
                {
                    "id": "plan-123",
                    "title": "Auth Refactor",
                    "status": "in_progress",
                    "plan_steps": [
                        {
                            "id": "t1",
                            "order": 1,
                            "title": "Task 1",
                            "status": "completed",
                        },
                    ],
                    "task_feedback_events": [],
                    "agent_cost": {
                        "total_cost_usd": 4.50,
                        "total_tokens": 250000,
                        "total_duration_seconds": 720,
                        "tasks_tracked": 2,
                        "by_model": {
                            "claude-sonnet-4": {
                                "tasks": 2,
                                "cost_usd": 4.50,
                                "tokens": 250000,
                                "duration_seconds": 720,
                            },
                        },
                    },
                    "enrichment_sources": [{"name": "Greptile", "description": "x"}],
                }
            )
        return original_get(path, params=params)

    fake_client.get = _get_cost
    code = cli.main(["status", "-p", "plan-123"])
    captured = capsys.readouterr()
    out = ANSI_RE.sub("", captured.out)
    assert code == 0
    assert "$4.50" in out
    assert "250,000 tokens" in out
    assert "claude-sonnet-4" in out
    assert "Greptile" in out


def test_status_surfaces_enrichment_analysis_and_ui_review_link(
    fake_client, capsys, monkeypatch
):
    _auth = {
        **_auth_with_plan(),
        "token": "ksh_pat_test",
        "api_url": "http://localhost:8000",
    }
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: _auth)
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: _auth)
    original_get = fake_client.get

    def _get_status(path, params=None, headers=None, timeout=None):
        if path == "/v1/plans/plan-123":
            return _FakeResponse(
                {
                    "id": "plan-123",
                    "title": "Kubetorch Helm Chart Horizontal Scaling Enhancement",
                    "status": "draft",
                    "source_type": "",
                    "target_type": "",
                    "updated_at": "2026-03-11T15:30:00Z",
                    "plan_steps": [
                        {
                            "id": "t1",
                            "order": 1,
                            "title": "Audit current chart structure",
                            "status": "todo",
                        }
                    ],
                    "task_feedback_events": [],
                    "enrichment_sources": [
                        {
                            "name": "Web research",
                            "detail": "Best practices for Helm charts -> https://example.com/helm",
                        }
                    ],
                    "decisions": {
                        "confidence_score": 74,
                        "risks": [
                            {"title": "Autoscaling values may break existing installs"}
                        ],
                        "unknowns": [
                            {"question": "Which clusters need backwards compatibility?"}
                        ],
                    },
                }
            )
        return original_get(path, params=params, headers=headers, timeout=timeout)

    fake_client.get = _get_status
    code = cli.main(["status", "-p", "plan-123"])
    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert code == 0
    assert "Enriched by: Web research" in out
    assert "Analysis: confidence: 74% · 1 risk · 1 open question" in out
    assert "Top risks:" in out
    assert "Open questions:" in out
    assert "Review in UI: http://localhost:3000/plans/plan-123" in out
    assert "Best practices for Helm charts" not in out
    assert "├──" not in out
