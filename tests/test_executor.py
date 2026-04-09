"""Unit tests for keshro_cli.executor — the per-task executor selection layer.

PR 3 of the executor-abstraction stack. These tests verify:
- normalize_executor accepts known values and rejects garbage
- resolve_task_executor honors the precedence:
    cli_override > task.executor > plan_default > fallback
- LocalClaudeCodeExecutor delegates to the injected launch callable
- ManagedAgentExecutor refuses cleanly until PR 4 wires it
- build_executor picks the right concrete implementation
"""

from __future__ import annotations

import asyncio

import pytest

from keshro_cli import executor as ex


# ---------------------------------------------------------------------------
# normalize_executor
# ---------------------------------------------------------------------------


def test_normalize_executor_accepts_known_values():
    assert ex.normalize_executor("local_claude_code") == "local_claude_code"
    assert ex.normalize_executor("managed_agent") == "managed_agent"


def test_normalize_executor_lowercases_and_strips():
    assert ex.normalize_executor("  Local_Claude_Code  ") == "local_claude_code"
    assert ex.normalize_executor("MANAGED_AGENT") == "managed_agent"


def test_normalize_executor_rejects_unknown_values():
    assert ex.normalize_executor("local") is None  # alias is the CLI's job
    assert ex.normalize_executor("anthropic") is None
    assert ex.normalize_executor("garbage") is None
    assert ex.normalize_executor("") is None
    assert ex.normalize_executor(None) is None


# ---------------------------------------------------------------------------
# resolve_task_executor — precedence
# ---------------------------------------------------------------------------


def test_resolve_task_executor_uses_cli_override_first():
    task = {"executor": "managed_agent"}
    assert (
        ex.resolve_task_executor(task, cli_override="local_claude_code")
        == "local_claude_code"
    )


def test_resolve_task_executor_falls_back_to_task_executor():
    task = {"executor": "managed_agent"}
    assert ex.resolve_task_executor(task) == "managed_agent"


def test_resolve_task_executor_uses_default_when_unset():
    assert ex.resolve_task_executor({}) == ex.DEFAULT_EXECUTOR
    assert ex.resolve_task_executor({"executor": None}) == ex.DEFAULT_EXECUTOR


def test_resolve_task_executor_ignores_invalid_task_value():
    task = {"executor": "wat"}
    assert ex.resolve_task_executor(task) == ex.DEFAULT_EXECUTOR


def test_resolve_task_executor_ignores_invalid_cli_override():
    task = {"executor": "managed_agent"}
    # Invalid override gets ignored, falling through to task.executor
    assert ex.resolve_task_executor(task, cli_override="bogus") == "managed_agent"


def test_resolve_task_executor_accepts_explicit_fallback():
    assert ex.resolve_task_executor({}, fallback="managed_agent") == "managed_agent"


def test_resolve_task_executor_uses_plan_default_when_task_unset():
    """Legacy plan whose task has no executor → use plan_default."""
    assert (
        ex.resolve_task_executor({}, plan_default="managed_agent")
        == "managed_agent"
    )


def test_resolve_task_executor_task_value_wins_over_plan_default():
    """A per-task choice always beats the plan-level default."""
    task = {"executor": "local_claude_code"}
    assert (
        ex.resolve_task_executor(task, plan_default="managed_agent")
        == "local_claude_code"
    )


def test_resolve_task_executor_cli_override_wins_over_plan_default():
    assert (
        ex.resolve_task_executor(
            {"executor": "local_claude_code"},
            cli_override="managed_agent",
            plan_default="local_claude_code",
        )
        == "managed_agent"
    )


def test_resolve_task_executor_invalid_plan_default_falls_through():
    """Garbage plan_default values fall through to the hard fallback."""
    assert (
        ex.resolve_task_executor({}, plan_default="garbage")
        == ex.DEFAULT_EXECUTOR
    )


# ---------------------------------------------------------------------------
# LocalClaudeCodeExecutor — delegates to injected launcher
# ---------------------------------------------------------------------------


def test_local_executor_forwards_args_to_launch_callable():
    captured: dict = {}

    async def fake_launch(task, plan, plan_id, work_dir, total, sem, client, **kw):
        captured["task"] = task
        captured["plan_id"] = plan_id
        captured["work_dir"] = work_dir
        captured["total"] = total
        captured["session_id"] = kw.get("session_id")
        captured["agent"] = kw.get("agent")
        return "ok"

    executor = ex.LocalClaudeCodeExecutor(launch=fake_launch)
    result = asyncio.run(
        executor.run_task(
            {"id": "t-1", "title": "Test"},
            {"id": "plan-1"},
            "plan-1",
            "/tmp/work",
            3,
            object(),  # sentinel semaphore
            object(),  # sentinel api_client
            session_id="sess-abc",
            agent="claude",
            visible=False,
            launch_index=0,
        )
    )
    assert result == "ok"
    assert captured["task"]["id"] == "t-1"
    assert captured["plan_id"] == "plan-1"
    assert captured["work_dir"] == "/tmp/work"
    assert captured["total"] == 3
    assert captured["session_id"] == "sess-abc"
    assert captured["agent"] == "claude"


# ---------------------------------------------------------------------------
# ManagedAgentExecutor — fails closed
# ---------------------------------------------------------------------------


def test_managed_executor_raises_with_actionable_message():
    executor = ex.ManagedAgentExecutor()
    with pytest.raises(ex.ManagedAgentNotWiredError) as excinfo:
        asyncio.run(
            executor.run_task(
                {"id": "t-2", "title": "Migrate Auth Service"},
                {},
                "plan-1",
                "/tmp/work",
                1,
                object(),
                object(),
            )
        )
    msg = str(excinfo.value)
    assert "Migrate Auth Service" in msg
    assert "local_claude_code" in msg


# ---------------------------------------------------------------------------
# build_executor
# ---------------------------------------------------------------------------


def test_build_executor_returns_local_for_default():
    async def fake_launch(*args, **kwargs):
        return "local"

    inst = ex.build_executor(ex.LOCAL_CLAUDE_CODE, launch=fake_launch)
    assert isinstance(inst, ex.LocalClaudeCodeExecutor)


def test_build_executor_returns_managed_for_managed_agent():
    async def fake_launch(*args, **kwargs):
        return "should_not_be_called"

    inst = ex.build_executor(ex.MANAGED_AGENT, launch=fake_launch)
    assert isinstance(inst, ex.ManagedAgentExecutor)


def test_build_executor_falls_back_to_local_on_unknown_name():
    async def fake_launch(*args, **kwargs):
        return "local"

    inst = ex.build_executor("garbage", launch=fake_launch)
    assert isinstance(inst, ex.LocalClaudeCodeExecutor)
