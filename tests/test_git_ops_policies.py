import asyncio

from keshro_cli import git_ops


class _DummyClient:
    async def get(self, *args, **kwargs):
        raise AssertionError("client.get should not be called")

    async def patch(self, *args, **kwargs):
        raise AssertionError("client.patch should not be called")


def test_create_task_pr_respects_disabled_policy(monkeypatch):
    async def _fake_git_stdout(*args, cwd):
        if args[:3] == ("git", "rev-parse", "--abbrev-ref"):
            return "feature/task"
        if args[:2] == ("git", "log"):
            return "abc123 test"
        raise AssertionError(args)

    monkeypatch.setattr(git_ops, "_git_stdout", _fake_git_stdout)
    monkeypatch.setattr(git_ops, "_resolve_default_branch", lambda cwd: "main")

    pr_url, branch_pushed = asyncio.run(
        git_ops._create_task_pr(
            exec_dir="/tmp/repo",
            task_id="task-1",
            task_title="Do thing",
            plan_title="Plan",
            task={},
            api_client=_DummyClient(),
            plan_id="plan-1",
            pr_policy="disabled",
        )
    )

    assert pr_url is None
    assert branch_pushed is False


def test_create_task_pr_respects_manual_policy(monkeypatch):
    calls = []

    async def _fake_git_stdout(*args, cwd):
        calls.append(args)
        if args[:3] == ("git", "rev-parse", "--abbrev-ref"):
            return "feature/task"
        if args[:2] == ("git", "log"):
            return "abc123 test"
        if args[:4] == ("git", "push", "-u", "origin"):
            return ""
        if args[:4] == ("git", "remote", "get-url", "origin"):
            return "git@github.com:acme/demo.git"
        raise AssertionError(args)

    monkeypatch.setattr(git_ops, "_git_stdout", _fake_git_stdout)
    monkeypatch.setattr(git_ops, "_resolve_default_branch", lambda cwd: "main")
    monkeypatch.setattr(git_ops, "_find_existing_pr", lambda **kwargs: asyncio.sleep(0, result=None))

    pr_url, branch_pushed = asyncio.run(
        git_ops._create_task_pr(
            exec_dir="/tmp/repo",
            task_id="task-1",
            task_title="Do thing",
            plan_title="Plan",
            task={},
            api_client=_DummyClient(),
            plan_id="plan-1",
            pr_policy="manual",
        )
    )

    assert pr_url is None
    assert branch_pushed is True
    assert any(call[:4] == ("git", "push", "-u", "origin") for call in calls)
