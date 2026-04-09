"""Per-task executor selection for the parallel wave scheduler.

PR 3 of the Keshro executor-abstraction stack. The backend schema (PR 1)
exposes an ``executor`` field on each task and a ``default_executor`` on
each org. This module interprets those values so the CLI's parallel
dispatch loop can run each task on the right runtime.

Today only the local Claude Code subprocess executor is wired up; the
managed-agent path errors out with a clear message pointing at the backend
proxy work in PR 4. The ``Executor`` Protocol is defined here so PR 4's
``ManagedAgentExecutor`` can slot in without further refactoring.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol

LOCAL_CLAUDE_CODE = "local_claude_code"
MANAGED_AGENT = "managed_agent"

VALID_EXECUTORS: frozenset[str] = frozenset({LOCAL_CLAUDE_CODE, MANAGED_AGENT})
DEFAULT_EXECUTOR: str = LOCAL_CLAUDE_CODE


def normalize_executor(value: Any) -> str | None:
    """Return a validated executor identifier or ``None``.

    Mirrors the backend's ``app.services.executors.normalize_executor`` so the
    CLI and server agree on what counts as a valid value. Unknown strings
    return ``None`` rather than raising, letting callers decide whether to
    substitute the default.
    """
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    return normalized if normalized in VALID_EXECUTORS else None


def resolve_task_executor(
    task: dict,
    *,
    cli_override: str | None = None,
    fallback: str = DEFAULT_EXECUTOR,
) -> str:
    """Decide which executor should run a single task.

    Precedence (highest → lowest):
      1. ``cli_override`` — the ``--executor`` flag, if the user set it.
      2. ``task["executor"]`` — the per-task choice stored on the plan.
      3. ``fallback`` — the hard default (``local_claude_code``).

    Invalid values at any level are treated as unset so a typo never
    silently routes traffic to the wrong runtime.
    """
    override = normalize_executor(cli_override)
    if override is not None:
        return override
    task_level = normalize_executor((task or {}).get("executor"))
    if task_level is not None:
        return task_level
    return normalize_executor(fallback) or DEFAULT_EXECUTOR


class ManagedAgentNotWiredError(RuntimeError):
    """Raised when the managed-agent path is requested before PR 4 lands.

    The backend proxy endpoint that streams session events into the task
    event log is not yet in place. Until it is, a CLI run that targets
    ``managed_agent`` for a task must fail loudly rather than silently
    fall back to local execution — silent fallback would make it look
    like managed execution is working when it isn't.
    """


class Executor(Protocol):
    """Runs a single task and returns the execution result.

    The parallel wave scheduler in ``cli._run_parallel`` calls this for
    every actionable task. The ``task`` dict is the plan step as returned
    by the backend (``PlanStep`` schema). ``**kwargs`` captures the
    launch parameters the local subprocess path already expects
    (``session_id``, ``agent``, ``visible``, ``launch_index``, etc.) so
    implementations can accept or ignore them as needed.
    """

    async def run_task(
        self,
        task: dict,
        plan: dict,
        plan_id: str,
        work_dir: str,
        total_agents: int,
        semaphore: Any,
        api_client: Any,
        **kwargs: Any,
    ) -> Any:
        ...


# Alias type for "a callable that launches one task the old way", so cli.py
# can pass its existing ``_launch_single_agent`` in without cyclic imports.
LaunchCallable = Callable[..., Awaitable[Any]]


class LocalClaudeCodeExecutor:
    """Delegates to the existing ``_launch_single_agent`` code path.

    Keeping this a thin pass-through for PR 3 so we do not have to refactor
    the ~1000-line launcher. PR 4's ``ManagedAgentExecutor`` sits next to
    this one and implements the same interface.
    """

    def __init__(self, launch: LaunchCallable) -> None:
        self._launch = launch

    async def run_task(
        self,
        task: dict,
        plan: dict,
        plan_id: str,
        work_dir: str,
        total_agents: int,
        semaphore: Any,
        api_client: Any,
        **kwargs: Any,
    ) -> Any:
        return await self._launch(
            task,
            plan,
            plan_id,
            work_dir,
            total_agents,
            semaphore,
            api_client,
            **kwargs,
        )


class ManagedAgentExecutor:
    """Placeholder that errors until PR 4 wires the backend session proxy.

    When the user explicitly opts a task into ``managed_agent`` execution
    and invokes ``keshro continue`` today, we refuse rather than silently
    running it locally. The error message tells them exactly how to get
    unblocked (switch the task back to local, or wait for PR 4).
    """

    async def run_task(
        self,
        task: dict,
        plan: dict,
        plan_id: str,
        work_dir: str,
        total_agents: int,
        semaphore: Any,
        api_client: Any,
        **kwargs: Any,
    ) -> Any:
        task_title = str((task or {}).get("title") or "task").strip() or "task"
        raise ManagedAgentNotWiredError(
            f"Task {task_title!r} is set to run on Anthropic Claude Managed Agents, "
            f"but the backend session proxy is not yet wired up in this version of "
            f"Keshro. Switch the task's executor to 'local_claude_code' "
            f"(or pass --executor local) to run it locally, or wait for the next "
            f"Keshro release to enable managed execution."
        )


def build_executor(name: str, *, launch: LaunchCallable) -> Executor:
    """Construct the ``Executor`` implementation for a given identifier.

    ``launch`` is the existing CLI single-task launcher callable; it is
    injected here so ``executor.py`` can stay free of any dependency on
    ``cli.py``'s large internal surface.
    """
    normalized = normalize_executor(name) or DEFAULT_EXECUTOR
    if normalized == MANAGED_AGENT:
        return ManagedAgentExecutor()
    return LocalClaudeCodeExecutor(launch=launch)
