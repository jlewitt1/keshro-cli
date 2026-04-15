"""Task management helper functions extracted from cli.py."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ._state import GREEN, RESET, _clean, _state
from .client import make_client, print_output
from .config import load_auth, update_auth
from .context import (
    _discover_git_remote_url,
    _discover_repo_root,
    _fetch_and_display_completion_audit,
    _require_plan_context,
)
from .formatting import _print_task_update_summary


# ---------------------------------------------------------------------------
# Plan helpers (shared with cli.py command handlers)
# ---------------------------------------------------------------------------


def _get_plan_or_exit(plan_id: str | None) -> dict:
    explicit_plan_id = _clean(plan_id)
    resolved_plan_id = _require_plan_context(plan_id)
    try:
        with make_client(_state.api_url, _state.token) as client:
            res = client.get(f"/v1/plans/{resolved_plan_id}")
            res.raise_for_status()
            return res.json()
    except httpx.HTTPStatusError as exc:
        response = exc.response
        cached_plan_id = _clean(load_auth().get("default_plan_id"))
        if (
            not explicit_plan_id
            and response is not None
            and response.status_code == 404
            and cached_plan_id == resolved_plan_id
        ):
            update_auth({"default_plan_id": None, "default_plan_title": None})
        raise


def _find_task(plan: dict, task_id: str) -> dict | None:
    return next(
        (
            step
            for step in (plan.get("plan_steps") or [])
            if _clean(step.get("id")) == _clean(task_id)
        ),
        None,
    )


# ---------------------------------------------------------------------------
# File parsing / validation
# ---------------------------------------------------------------------------


def _read_json_file(path: str | None):
    if not path:
        return []
    return json.loads(Path(path).read_text())


def _infer_types_from_title(title: str) -> tuple[str | None, str | None]:
    match = re.match(r"^\s*(.+?)\s+to\s+(.+?)\s*$", title)
    if not match:
        return None, None
    return match.group(1).strip(), match.group(2).strip()


def _parse_text_steps(text: str) -> tuple[str | None, list[dict]]:
    title = None
    steps: list[dict] = []
    current_step: dict | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if title is None and line.startswith("#"):
            title = line.lstrip("#").strip()
            continue
        bullet = re.match(r"^(?:[-*]|\d+\.)\s+(.*)$", line)
        checkbox = re.match(r"^\[(?: |x|X)\]\s+(.*)$", line)
        if bullet or checkbox:
            content = (bullet or checkbox).group(1).strip()
            current_step = {
                "title": content,
                "description": "",
                "status": "todo",
            }
            steps.append(current_step)
            continue
        if title is None:
            title = line
            continue
        if current_step is None:
            current_step = {
                "title": line,
                "description": "",
                "status": "todo",
            }
            steps.append(current_step)
            continue
        current_step["description"] = (
            f"{current_step['description']}\n{line}".strip()
            if current_step["description"]
            else line
        )
    return title, steps


def _plan_payload_from_file(
    from_path: str,
    import_source: str,
    title: str | None = None,
    summary: str | None = None,
    status: str | None = None,
    migration_id: str | None = None,
    link: list[str] | None = None,
) -> dict:
    body = Path(from_path).read_text()
    parsed = json.loads(body) if from_path.endswith(".json") else None
    if isinstance(parsed, dict):
        chosen_title = title or parsed.get("title") or ""
        return {
            "title": chosen_title,
            "summary": summary or parsed.get("summary"),
            "status": status,
            "template_key": parsed.get("template_key"),
            "import_source": import_source,
            "migration_id": migration_id,
            "plan_steps": parsed.get("plan_steps") or parsed.get("steps") or [],
            "external_links": parsed.get("external_links") or link or [],
        }
    if isinstance(parsed, list):
        chosen_title = title or "Imported plan"
        return {
            "title": chosen_title,
            "summary": summary,
            "status": status,
            "import_source": import_source,
            "migration_id": migration_id,
            "plan_steps": parsed,
            "external_links": link or [],
        }
    file_title, steps = _parse_text_steps(body)
    chosen_title = title or file_title or "Imported plan"
    return {
        "title": chosen_title,
        "summary": summary,
        "status": status,
        "import_source": import_source,
        "migration_id": migration_id,
        "plan_steps": steps,
        "external_links": link or [],
    }


def _validate_plan_payload(payload: dict) -> None:
    missing = [
        key for key in ["migration_id"] if not str(payload.get(key) or "").strip()
    ]
    if missing:
        raise SystemExit(
            f"Missing required plan fields: {', '.join(missing)}. "
            "Create the plan from a migration context or pass the migration ID directly."
        )


# ---------------------------------------------------------------------------
# Runtime context
# ---------------------------------------------------------------------------


def _git_branch_for_runtime(repo_root: Path | None) -> str | None:
    if repo_root is None:
        return None
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(repo_root),
            check=True,
            capture_output=True,
            text=True,
        )
        branch = _clean(result.stdout)
        return branch or None
    except Exception:
        return None


def _python_runtime_details() -> tuple[str | None, str | None]:
    python_exec = shutil.which("python") or shutil.which("python3")
    if not python_exec:
        return None, None
    try:
        result = subprocess.run(
            [python_exec, "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
        version = _clean(result.stdout) or _clean(result.stderr) or None
    except Exception:
        version = None
    return python_exec, version


def _collect_task_runtime_context_for(cwd: str) -> dict:
    cwd = str(Path(cwd).resolve())
    repo_root = _discover_repo_root(cwd)
    python_exec, python_version = _python_runtime_details()
    available_tools: list[str] = []
    missing_tools: list[str] = []
    for tool in ["python", "python3", "uv", "pytest", "git"]:
        if shutil.which(tool):
            available_tools.append(tool)
        else:
            missing_tools.append(tool)

    virtual_env = _clean(os.environ.get("VIRTUAL_ENV")) or None
    if not virtual_env and repo_root:
        candidate = repo_root / ".venv"
        if candidate.exists():
            virtual_env = str(candidate)

    context = {
        "cwd": cwd,
        "repo_root": str(repo_root) if repo_root else None,
        "git_branch": _git_branch_for_runtime(repo_root),
        "git_remote_url": _discover_git_remote_url(repo_root),
        "python_executable": python_exec,
        "python_version": python_version,
        "virtual_env": virtual_env,
        "available_tools": available_tools,
        "missing_tools": missing_tools,
        "os": f"{sys.platform} ({os.name})",
    }
    return {key: value for key, value in context.items() if value not in (None, [], "")}


def _collect_task_outcome(work_dir: str | None = None) -> dict | None:
    """Collect structured git diff data since last keshro checkpoint."""
    try:
        cwd = work_dir or os.getcwd()
        # Find last keshro checkpoint commit
        checkpoint_result = subprocess.run(
            ["git", "log", "--grep=keshro: checkpoint", "-1", "--format=%H"],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        checkpoint = (checkpoint_result.stdout or "").strip()
        diff_range = f"{checkpoint}..HEAD"
        if not checkpoint:
            merge_base = ""
            for base_ref in ("origin/main", "main", "origin/master", "master"):
                merge_base_result = subprocess.run(
                    ["git", "merge-base", "HEAD", base_ref],
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                    check=False,
                )
                candidate = (merge_base_result.stdout or "").strip()
                if candidate:
                    merge_base = candidate
                    break

            if merge_base:
                diff_range = f"{merge_base}..HEAD"
            else:
                root_commit_result = subprocess.run(
                    ["git", "rev-list", "--max-parents=0", "HEAD"],
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                    check=False,
                )
                root_commits = [
                    commit.strip()
                    for commit in (root_commit_result.stdout or "").splitlines()
                    if commit.strip()
                ]
                if not root_commits:
                    return None
                diff_range = f"{root_commits[0]}..HEAD"

        # git diff --numstat for files_changed
        numstat = subprocess.run(
            ["git", "diff", "--numstat", diff_range],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        # git diff --name-status for change_type
        name_status = subprocess.run(
            ["git", "diff", "--name-status", diff_range],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        status_map: dict[str, str] = {}
        type_mapping = {"A": "added", "D": "deleted", "M": "modified", "R": "renamed"}
        for line in (name_status.stdout or "").strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                code = parts[0][0] if parts[0] else "M"
                path = parts[-1]
                status_map[path] = type_mapping.get(code, "modified")

        files_changed: list[dict] = []
        for line in (numstat.stdout or "").strip().splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            added, removed, path = parts[0], parts[1], parts[2]
            files_changed.append(
                {
                    "path": path,
                    "lines_added": int(added) if added != "-" else 0,
                    "lines_removed": int(removed) if removed != "-" else 0,
                    "change_type": status_map.get(path, "modified"),
                }
            )
            if len(files_changed) >= 200:
                break

        # git log for commits
        log_result = subprocess.run(
            ["git", "log", "--format=%H", diff_range],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        commits = [h for h in (log_result.stdout or "").strip().splitlines() if h][:50]

        # git diff --stat for summary
        stat_result = subprocess.run(
            ["git", "diff", "--stat", diff_range],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        stat_lines = (stat_result.stdout or "").strip().splitlines()
        diff_stat = stat_lines[-1].strip() if stat_lines else ""

        if not files_changed and not commits:
            return None

        outcome: dict[str, object] = {}
        if files_changed:
            outcome["files_changed"] = files_changed
        if commits:
            outcome["commits"] = commits
        if diff_stat:
            outcome["diff_stat"] = diff_stat
        return outcome
    except Exception:
        return None


async def _collect_task_outcome_async(work_dir: str | None = None) -> dict | None:
    import functools

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, functools.partial(_collect_task_outcome, work_dir)
    )


def _collect_task_runtime_context() -> dict:
    return _collect_task_runtime_context_for(str(Path.cwd().resolve()))


def _extract_session_id(value: str | None) -> str:
    cleaned = _clean(value)
    if cleaned.startswith("session:"):
        return cleaned.split("session:", 1)[1].strip()
    return ""


def _infer_agent_client() -> str:
    override = _clean(os.environ.get("KESHRO_AGENT_CLIENT"))
    if override:
        return override
    if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_CI"):
        return "Codex"
    if os.environ.get("CURSOR_TRACE_ID") or os.environ.get("CURSOR_SESSION_ID"):
        return "Cursor"
    if (
        os.environ.get("CLAUDECODE")
        or os.environ.get("CLAUDE_CODE")
        or os.environ.get("CLAUDE_SESSION_ID")
    ):
        return "Claude Code"
    return ""


def _infer_model_name() -> str:
    for key in (
        "KESHRO_AGENT_MODEL",
        "OPENAI_MODEL",
        "OPENAI_DEFAULT_MODEL",
        "ANTHROPIC_MODEL",
        "CLAUDE_MODEL",
    ):
        value = _clean(os.environ.get(key))
        if value:
            return value
    return ""


def _post_agent_task_event(
    plan_id: str,
    task_id: str,
    *,
    event: str,
    reason: str | None = None,
    note: str | None = None,
    agent_session_id: str | None = None,
    duration_seconds: float | None = None,
    tokens_used: int | None = None,
    cost_usd: float | None = None,
    model: str | None = None,
    outcome: dict | None = None,
) -> None:
    payload: dict[str, object] = {
        "task_id": task_id,
        "event": event,
    }
    if reason:
        payload["reason"] = reason
    if note:
        payload["note"] = note
    if outcome:
        payload["outcome"] = outcome
    agent_client = _infer_agent_client()
    if agent_client:
        payload["agent_client"] = agent_client
    session_id = _clean(agent_session_id)
    if session_id:
        payload["agent_session_id"] = session_id
    resolved_model = _clean(model) or _infer_model_name()
    if resolved_model:
        payload["model"] = resolved_model
    if duration_seconds is not None:
        payload["duration_seconds"] = duration_seconds
    if tokens_used is not None:
        payload["tokens_used"] = tokens_used
    if cost_usd is not None:
        payload["cost_usd"] = cost_usd

    try:
        with make_client(_state.api_url, _state.token) as client:
            client.post(
                f"/v1/agent/plans/{plan_id}/task-event",
                json=payload,
                timeout=10,
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Task CRUD operations
# ---------------------------------------------------------------------------


def _do_task_update(
    plan_id: str | None,
    task_id: str,
    title: str | None = None,
    description: str | None = None,
    status: str | None = None,
    owner: str | None = None,
    notes: str | None = None,
    issue_id: str | None = None,
    blocked_reason: str | None = None,
    feedback_reason: str | None = None,
    link: list[str] | None = None,
    depends_on: list[str] | None = None,
    parallelizable: bool | None = None,
):
    plan_id = _require_plan_context(plan_id)
    payload: dict = {}
    for key, value in [
        ("title", title),
        ("description", description),
        ("status", status),
        ("owner", owner),
        ("notes", notes),
        ("external_issue_id", issue_id),
        ("blocked_reason", blocked_reason),
        ("feedback_reason", feedback_reason),
    ]:
        if value is not None:
            payload[key] = value
    if link is not None:
        payload["artifact_links"] = link
    if depends_on is not None:
        payload["depends_on"] = depends_on
        payload["scheduling_source"] = "manual"
    if parallelizable is not None:
        payload["parallelizable"] = parallelizable
        payload["scheduling_source"] = "manual"
    payload["runtime_context"] = _collect_task_runtime_context()
    with make_client(_state.api_url, _state.token) as client:
        res = client.patch(
            f"/v1/plans/{plan_id}/tasks/{task_id}",
            json=payload,
        )
        res.raise_for_status()
        plan = res.json()
        if _state.json:
            print_output(plan, True)
            return
        _print_task_update_summary(plan, task_id=task_id, payload=payload)


def _parse_dependency_ids(raw_values: list[str] | None) -> list[str]:
    dependency_ids: list[str] = []
    seen: set[str] = set()
    for raw in raw_values or []:
        for candidate in re.split(r"[\n,]+", str(raw or "")):
            cleaned = _clean(candidate)
            if not cleaned or cleaned in seen:
                continue
            dependency_ids.append(cleaned)
            seen.add(cleaned)
    return dependency_ids


def _build_appended_task_notes(
    plan_id: str | None,
    task_id: str,
    note: str | None,
) -> str | None:
    cleaned_note = _clean(note)
    if not cleaned_note:
        return None
    resolved_plan_id = _require_plan_context(plan_id)
    plan = _get_plan_or_exit(resolved_plan_id)
    task = _find_task(plan, task_id)
    if not task:
        raise SystemExit(f"Task not found: {task_id}")
    existing_notes = _clean(task.get("notes"))
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"{existing_notes}\n\n[{timestamp}] {cleaned_note}".strip()
        if existing_notes
        else f"[{timestamp}] {cleaned_note}"
    )


def _task_completion_requirements(task: dict) -> list[str]:
    requirements: list[str] = []
    if task.get("acceptance_criteria"):
        requirements.append("Acceptance criteria met:")
    if task.get("acceptance_criteria") or task.get("discovery_commands"):
        requirements.append("Verification:")
    return requirements


def _ensure_completion_note_covers_requirements(
    plan_id: str | None,
    task_id: str,
    note: str | None,
) -> None:
    resolved_plan_id = _require_plan_context(plan_id)
    plan = _get_plan_or_exit(resolved_plan_id)
    task = _find_task(plan, task_id)
    if not task:
        raise SystemExit(f"Task not found: {task_id}")
    requirements = _task_completion_requirements(task)
    if not requirements:
        return
    cleaned_note = _clean(note)
    missing = [
        marker for marker in requirements if marker.lower() not in cleaned_note.lower()
    ]
    if not missing:
        return
    title = _clean(task.get("title")) or task_id
    raise SystemExit(
        "Completion note for "
        f"{title} must include {' and '.join(missing)} "
        "before this task can be marked done."
    )


def _infer_task_done_context(plan_id: str | None, task_id: str) -> dict[str, object]:
    resolved_plan_id = _require_plan_context(plan_id)
    plan = _get_plan_or_exit(resolved_plan_id)
    events = [
        dict(event or {})
        for event in (plan.get("task_feedback_events") or [])
        if isinstance(event, dict) and _clean(event.get("task_id")) == task_id
    ]
    events.reverse()

    session_id = ""
    started_at = ""
    for event in events:
        if not session_id:
            session_id = _clean(event.get("agent_session_id"))
            if not session_id:
                feedback_reason = _clean(event.get("feedback_reason"))
                if feedback_reason.startswith("session:"):
                    session_id = feedback_reason.split("session:", 1)[1].strip()
        after = event.get("after") if isinstance(event.get("after"), dict) else {}
        if not started_at and _clean(after.get("status")) == "in_progress":
            started_at = _clean(event.get("created_at"))
        if session_id and started_at:
            break

    duration_seconds = None
    if started_at:
        try:
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            duration_seconds = max(
                0.0, (datetime.now(timezone.utc) - started).total_seconds()
            )
        except Exception:
            duration_seconds = None

    inferred: dict[str, object] = {}
    if session_id:
        inferred["agent_session_id"] = session_id
    if duration_seconds is not None:
        inferred["duration_seconds"] = round(duration_seconds, 1)
    return inferred


def _do_task_start(
    plan_id: str | None,
    task_id: str,
    owner: str | None = None,
    notes: str | None = None,
    feedback_reason: str | None = None,
    link: list[str] | None = None,
):
    _do_task_update(
        plan_id,
        task_id,
        status="in_progress",
        owner=owner,
        notes=_build_appended_task_notes(plan_id, task_id, notes),
        feedback_reason=feedback_reason,
        link=link,
    )
    resolved_plan_id = _require_plan_context(plan_id)
    _post_agent_task_event(
        resolved_plan_id,
        task_id,
        event="start",
        reason=feedback_reason,
        note=notes,
        agent_session_id=_extract_session_id(feedback_reason),
    )


def _do_task_done(
    plan_id: str | None,
    task_id: str,
    notes: str | None = None,
    feedback_reason: str | None = None,
    link: list[str] | None = None,
):
    _ensure_completion_note_covers_requirements(plan_id, task_id, notes)
    _do_task_update(
        plan_id,
        task_id,
        status="completed",
        notes=_build_appended_task_notes(plan_id, task_id, notes),
        feedback_reason=feedback_reason,
        link=link,
        blocked_reason="",
    )
    resolved_plan_id = _require_plan_context(plan_id)
    inferred = _infer_task_done_context(resolved_plan_id, task_id)
    inferred_model = _infer_model_name()
    outcome = _collect_task_outcome()
    _post_agent_task_event(
        resolved_plan_id,
        task_id,
        event="done",
        note=notes,
        agent_session_id=str(inferred.get("agent_session_id") or ""),
        duration_seconds=float(inferred.get("duration_seconds") or 0)
        if inferred.get("duration_seconds") is not None
        else None,
        model=inferred_model or None,
        outcome=outcome,
    )
    # Check if plan is now fully complete and show audit
    try:
        plan = _get_plan_or_exit(resolved_plan_id)
        steps = plan.get("plan_steps") or []
        leaf_steps = [s for s in steps if not s.get("child_task_ids")]
        all_done = bool(leaf_steps) and all(
            _clean(s.get("status") or "").lower() == "completed"
            for s in leaf_steps
        )
        if all_done:
            print(f"\n{GREEN}All plan tasks completed.{RESET}")
            _fetch_and_display_completion_audit(resolved_plan_id, plan)
    except Exception:
        pass


def _append_task_note(
    plan_id: str | None,
    task_id: str,
    note: str,
    feedback_reason: str | None = None,
):
    resolved_plan_id = _require_plan_context(plan_id)
    _do_task_update(
        resolved_plan_id,
        task_id,
        notes=_build_appended_task_notes(resolved_plan_id, task_id, note),
        feedback_reason=feedback_reason,
    )


def _add_task_artifact(
    plan_id: str | None,
    task_id: str,
    artifact_link: str,
    feedback_reason: str | None = None,
):
    resolved_plan_id = _require_plan_context(plan_id)
    plan = _get_plan_or_exit(resolved_plan_id)
    task = _find_task(plan, task_id)
    if not task:
        raise SystemExit(f"Task not found: {task_id}")
    existing_links = [
        _clean(link) for link in (task.get("artifact_links") or []) if _clean(str(link))
    ]
    next_link = artifact_link.strip()
    next_links = (
        existing_links if next_link in existing_links else [*existing_links, next_link]
    )
    _do_task_update(
        resolved_plan_id,
        task_id,
        link=next_links,
        feedback_reason=feedback_reason,
    )


def _do_task_block(
    plan_id: str | None,
    task_id: str,
    blocked_reason: str,
    feedback_reason: str | None = None,
):
    _do_task_update(
        plan_id,
        task_id,
        status="blocked",
        blocked_reason=blocked_reason,
        feedback_reason=feedback_reason,
    )
    resolved_plan_id = _require_plan_context(plan_id)
    inferred = _infer_task_done_context(resolved_plan_id, task_id)
    _post_agent_task_event(
        resolved_plan_id,
        task_id,
        event="block",
        reason=blocked_reason,
        agent_session_id=str(inferred.get("agent_session_id") or ""),
        duration_seconds=float(inferred.get("duration_seconds") or 0)
        if inferred.get("duration_seconds") is not None
        else None,
    )


def _do_task_unblock(
    plan_id: str | None,
    task_id: str,
    notes: str | None = None,
    feedback_reason: str | None = None,
    status: str = "in_progress",
):
    _do_task_update(
        plan_id,
        task_id,
        status=status,
        notes=_build_appended_task_notes(plan_id, task_id, notes),
        blocked_reason="",
        feedback_reason=feedback_reason,
    )


def _do_task_reopen(
    plan_id: str | None,
    task_id: str,
    notes: str | None = None,
    feedback_reason: str | None = None,
    status: str = "todo",
):
    _do_task_update(
        plan_id,
        task_id,
        status=status,
        notes=_build_appended_task_notes(plan_id, task_id, notes),
        blocked_reason="",
        feedback_reason=feedback_reason,
    )
