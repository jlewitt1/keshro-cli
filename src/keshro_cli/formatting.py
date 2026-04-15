"""Display / formatting helpers extracted from cli.py."""

import re
import sys
import textwrap
from datetime import datetime, timezone

import httpx

from ._state import CYAN, DIM, GREEN, RED, RESET, YELLOW, _clean, _state, _stdout_is_tty
from .client import make_client, print_output
from .config import DEFAULT_API_URL


# ---------------------------------------------------------------------------
# Timestamp / duration helpers
# ---------------------------------------------------------------------------


def _parse_timestamp(value: str | None) -> datetime | None:
    raw = _clean(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_duration_compact(seconds: float | int) -> str:
    total_seconds = max(0, int(round(float(seconds or 0))))
    mins, secs = divmod(total_seconds, 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours}h {mins}m"
    if mins > 0:
        return f"{mins}m {secs}s"
    return f"{secs}s"


def _format_agent_phase_label(value: str | None) -> str:
    key = _clean(value).lower().replace("-", "_").replace(" ", "_")
    if not key:
        return ""
    labels = {
        "starting": "starting",
        "running": "running",
        "editing": "editing files",
        "visible_terminal": "visible terminal",
        "validating": "validating",
        "testing": "running tests",
        "linting": "running lint",
        "blocked": "blocked",
        "needs_rebase": "needs rebase",
        "completed": "completed",
    }
    return labels.get(key, key.replace("_", " "))


def _summarize_agent_session_line(session: dict) -> str:
    from .cli import _should_ignore_agent_output_line

    status = _clean(session.get("status")).lower() or "running"
    phase = _format_agent_phase_label(session.get("current_phase"))
    progress = _clean(session.get("progress_message"))
    touched_files = tuple((session.get("touched_files") or [])[:3])
    conflicting_files = tuple((session.get("conflicting_files") or [])[:3])
    recent_errors = session.get("recent_errors") or []
    latest_error = _clean(recent_errors[-1] if recent_errors else "")
    if _should_ignore_agent_output_line(latest_error):
        latest_error = ""

    details: list[str] = []
    if conflicting_files:
        details.append(f"waiting on conflict: {', '.join(conflicting_files)}")
    elif latest_error and status in {"error", "failed", "blocked", "failed_to_launch"}:
        details.append(f"error: {latest_error}")
    else:
        if phase:
            details.append(phase)
        if progress:
            details.append(progress)
        if touched_files:
            details.append(f"files: {', '.join(touched_files)}")

    if not details:
        details.append(status or "running")
    return " | ".join(details)


def _plan_execution_snapshot(plan: dict) -> str:
    from .cli import _all_actionable_tasks

    steps = plan.get("plan_steps") or []
    total = len(steps)
    done = len([s for s in steps if _clean(s.get("status")).lower() == "completed"])
    active = [s for s in steps if _clean(s.get("status")).lower() == "in_progress"]
    blocked = [s for s in steps if _clean(s.get("status")).lower() == "blocked"]
    ready = _all_actionable_tasks(plan)

    parts = [f"{done}/{total} done" if total > 0 else "0 tasks"]
    if active:
        parts.append(f"{len(active)} active")
    if blocked:
        parts.append(f"{len(blocked)} blocked")
    if ready:
        parts.append(f"{len(ready)} ready")

    active_titles = [
        _clean(step.get("title")) or _clean(step.get("id")) or "task"
        for step in active[:3]
    ]
    if active_titles:
        parts.append(f"running: {', '.join(active_titles)}")
    elif ready:
        ready_titles = [
            _clean(step.get("title")) or _clean(step.get("id")) or "task"
            for step in ready[:3]
        ]
        parts.append(f"next: {', '.join(ready_titles)}")

    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Async / event helpers
# ---------------------------------------------------------------------------


async def _mark_agent_session_stopped_async(
    client: httpx.AsyncClient,
    plan_id: str,
    task_id: str,
    *,
    session_id: str = "",
    exec_dir: str | None = None,
    reason: str = "Execution stopped by user before task completion.",
    runtime_context: dict | None = None,
) -> None:
    from .cli import _post_agent_heartbeat_async

    try:
        await _post_agent_heartbeat_async(
            client,
            plan_id,
            task_id,
            session_id=session_id,
            exec_dir=exec_dir,
            status="stopped",
            current_phase="stopped",
            progress_message=reason,
            recent_error=reason,
            runtime_context=runtime_context,
        )
    except Exception:
        pass


def _event_status(event: dict) -> str:
    after = event.get("after")
    if isinstance(after, dict):
        status = _clean(after.get("status"))
        if status:
            return status
    event_type = _clean(event.get("event_type")).lower()
    if event_type == "task_start":
        return "in_progress"
    if event_type == "task_done":
        return "completed"
    if event_type == "task_block":
        return "blocked"
    return ""


def _elapsed_runtime_from_events(events: list[dict]) -> tuple[float, int]:
    by_task: dict[str, list[dict]] = {}
    for event in events:
        task_id = _clean(event.get("task_id"))
        if not task_id:
            continue
        by_task.setdefault(task_id, []).append(event)

    total_elapsed = 0.0
    tasks_with_elapsed = 0
    for task_events in by_task.values():
        sorted_events = sorted(
            task_events, key=lambda event: _clean(event.get("created_at"))
        )
        started_at: datetime | None = None
        finished_at: datetime | None = None
        for event in sorted_events:
            event_time = _parse_timestamp(event.get("created_at"))
            if event_time is None:
                continue
            status = _event_status(event)
            if started_at is None and status == "in_progress":
                started_at = event_time
                continue
            if started_at is not None and status in {"completed", "blocked"}:
                finished_at = event_time
                break
        if started_at is not None and finished_at is not None:
            total_elapsed += max(0.0, (finished_at - started_at).total_seconds())
            tasks_with_elapsed += 1

    return total_elapsed, tasks_with_elapsed


def _format_plan_timestamp(value: str | None) -> str:
    raw = _clean(value)
    parsed = _parse_timestamp(raw)
    if parsed is None:
        return raw

    local_time = parsed.astimezone()
    now_local = datetime.now().astimezone()
    if local_time.date() == now_local.date():
        return local_time.strftime("Today %H:%M")
    if local_time.year == now_local.year:
        return local_time.strftime("%b %d")
    return local_time.strftime("%Y-%m-%d")


def _format_verbose_timestamp(value: str | None) -> str:
    raw = _clean(value)
    parsed = _parse_timestamp(raw)
    if parsed is None:
        return raw
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M %Z")


# ---------------------------------------------------------------------------
# Display / print helpers
# ---------------------------------------------------------------------------


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def _fmt(row: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    print(_fmt(headers))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print(_fmt(row))


def _print_migration_summary(
    migration: dict,
    verbose: bool = False,
    context_label: str | None = None,
    show_id: bool = True,
) -> None:
    migration_id = migration.get("id", "")
    status = _clean(migration.get("status") or "pending") or "pending"
    source = migration.get("source_type") or "Unknown source"
    target = migration.get("target_type") or "Unknown target"
    created_at = _clean(migration.get("created_at"))
    date_part = f"  {DIM}{created_at}{RESET}" if created_at else ""
    suffix = f"  {DIM}for org {context_label}{RESET}" if context_label else ""
    prefix = f"{CYAN}{migration_id}{RESET}  " if show_id and migration_id else ""
    print(f"{prefix}{source} -> {target}  {DIM}[{status}]{RESET}{date_part}{suffix}")
    if verbose:
        if migration.get("outcome_status"):
            print(f"  {DIM}Outcome:{RESET} {migration['outcome_status']}")
        if migration.get("confidence_score") is not None:
            basis = migration.get("confidence_basis")
            tag = " (from real outcomes)" if basis == "outcome_based" else ""
            print(f"  {DIM}Confidence:{RESET} {migration['confidence_score']}{tag}")


def _summarize_plan_progress(plan: dict | None) -> str | None:
    if not isinstance(plan, dict):
        return None
    steps = plan.get("plan_steps") or []
    if not isinstance(steps, list) or not steps:
        return None

    counts = {"done": 0, "in_progress": 0, "blocked": 0, "todo": 0}
    for step in steps:
        status = _clean((step or {}).get("status") or "todo") or "todo"
        if status in counts:
            counts[status] += 1
        else:
            counts["todo"] += 1
    total = len(steps)
    parts = [f"{counts['done']}/{total} done"]
    if counts["in_progress"]:
        parts.append(f"{counts['in_progress']} in progress")
    if counts["blocked"]:
        parts.append(f"{counts['blocked']} blocked")
    if counts["todo"]:
        parts.append(f"{counts['todo']} todo")
    return ", ".join(parts)


def _print_wrapped_block(
    label: str,
    value: str,
    *,
    indent: str = "",
    width: int = 88,
) -> None:
    text = _clean(value)
    if not text:
        return
    prefix = f"{indent}{DIM}{label}:{RESET} "
    wrapped = textwrap.wrap(text, width=width, subsequent_indent=" " * len(prefix))
    if not wrapped:
        return
    print(prefix + wrapped[0])
    for line in wrapped[1:]:
        print(" " * len(prefix) + line)


def _print_migration_detail(
    migration: dict,
    *,
    context_label: str | None = None,
    linked_plan: dict | None = None,
    api_url: str | None = None,
) -> None:
    from .context import _app_url_from_api_url

    _print_migration_summary(
        migration, verbose=True, context_label=context_label, show_id=False
    )
    print()
    print("Overview")
    created_at = _format_verbose_timestamp(_clean(migration.get("created_at")))
    if created_at:
        print(f"{DIM}Created:{RESET} {created_at}")
    if migration.get("migration_mode"):
        print(f"{DIM}Mode:{RESET} {migration['migration_mode']}")
    if migration.get("input_method"):
        print(f"{DIM}Input method:{RESET} {migration['input_method']}")
    if migration.get("outcome_status"):
        print(f"{DIM}Outcome:{RESET} {migration['outcome_status']}")
    if migration.get("analysis_revision") is not None:
        print(f"{DIM}Analysis revision:{RESET} {migration['analysis_revision']}")
    if migration.get("org_id"):
        print(f"{DIM}Org:{RESET} {migration['org_id']}")
    if linked_plan:
        progress = _summarize_plan_progress(linked_plan)
        if progress:
            print(f"{DIM}Execution progress:{RESET} {progress}")
        plan_validation = linked_plan.get("plan_validation") or {}
        if isinstance(plan_validation, dict) and plan_validation.get("status"):
            validation_bits = [_clean(plan_validation.get("status"))]
            if plan_validation.get("overall_score") is not None:
                validation_bits.append(f"score {plan_validation['overall_score']:.1f}")
            findings = plan_validation.get("findings") or []
            if findings:
                validation_bits.append(f"{len(findings)} finding(s)")
            print(f"{DIM}Execution validation:{RESET} {', '.join(validation_bits)}")
    if migration.get("github_url"):
        print(f"{DIM}GitHub URL:{RESET} {migration['github_url']}")
    if migration.get("resource_url"):
        print(f"{DIM}Resource URL:{RESET} {migration['resource_url']}")
    source_files = migration.get("source_files") or []
    if source_files:
        print(f"{DIM}Source files:{RESET} {len(source_files)} attached")
    if migration.get("custom_fields"):
        print(
            f"{DIM}Migration inputs:{RESET} {len(migration['custom_fields'])} captured"
        )
    if migration.get("confidence_explanation"):
        print()
        basis = migration.get("confidence_basis")
        if basis == "outcome_based":
            print(f"Assessment  {DIM}[Confidence (outcome-based)]{RESET}")
        elif basis == "ai_estimated":
            print(f"Assessment  {DIM}[Confidence (AI-estimated)]{RESET}")
        else:
            print("Assessment")
        _print_wrapped_block(
            "Confidence explanation", migration["confidence_explanation"]
        )
    if migration.get("error_message"):
        print(f"{DIM}Error:{RESET} {migration['error_message']}")
    effort = migration.get("effort_estimate") or {}
    if effort.get("total_hours") is not None:
        print(f"{DIM}Effort:{RESET} {effort.get('total_hours')}h")
    cost = migration.get("cost_estimate") or {}
    low = cost.get("total_cost_low")
    high = cost.get("total_cost_high")
    if low is not None or high is not None:
        print(f"{DIM}Cost:{RESET} {low} - {high}")
    risks = migration.get("risks") or []
    if risks:
        print()
        print("Risks")
        high_risk_count = sum(
            1
            for risk in risks
            if _clean((risk or {}).get("severity")).lower() in {"critical", "high"}
        )
        print(
            f"{DIM}Risks:{RESET} {len(risks)} total"
            + (f" ({high_risk_count} high/critical)" if high_risk_count else "")
        )
        for risk in risks[:3]:
            title = _clean((risk or {}).get("title")) or "Untitled risk"
            severity = _clean((risk or {}).get("severity")).upper() or "?"
            print(f"  - [{severity}] {title}")
    unknowns = migration.get("unknowns") or []
    if unknowns:
        print()
        print("Questions")
        pending = [item for item in unknowns if not _clean((item or {}).get("answer"))]
        answered = len(unknowns) - len(pending)
        print(
            f"{DIM}Unknowns:{RESET} {len(unknowns)} total, {len(pending)} pending, {answered} answered"
        )
        for item in pending[:3]:
            question = _truncate_text(_clean((item or {}).get("question")), limit=120)
            if question:
                print(f"  - {question}")
    quality = migration.get("assessment_quality") or {}
    if isinstance(quality, dict):
        required_validations = quality.get("required_validations") or []
        if required_validations:
            print()
            print("Checks")
            print(f"{DIM}Required validations:{RESET}")
            for item in required_validations[:3]:
                print(f"  - {_truncate_text(str(item), limit=120)}")
        next_actions = quality.get("next_actions") or []
        if next_actions:
            print(f"{DIM}Next actions:{RESET}")
            for item in next_actions[:3]:
                print(f"  - {_truncate_text(str(item), limit=120)}")
    if migration.get("notes"):
        print()
        print("Notes")
        note_lines = [
            line.strip()
            for line in str(migration["notes"]).splitlines()
            if line.strip()
        ]
        for line in note_lines[:10]:
            print(f"  {line}")
        if len(note_lines) > 10:
            print("  …")
    steps = migration.get("migration_steps") or []
    if steps:
        print()
        print(f"{DIM}Steps:{RESET}")
        for step in steps:
            title = step.get("title") or "Untitled step"
            order = step.get("order", "?")
            print(f"  {order}. {title}")
            if step.get("description"):
                for line in textwrap.wrap(
                    str(step["description"]),
                    width=82,
                    initial_indent="     ",
                    subsequent_indent="     ",
                ):
                    print(line)
    app_url = _app_url_from_api_url(api_url) if api_url else ""
    migration_id = _clean(migration.get("id"))
    print()
    print("Links")
    if app_url and migration_id:
        print(f"{DIM}Dashboard:{RESET} {app_url}/migrations/{migration_id}")


def _print_plan_summary(
    plan: dict, verbose: bool = False, context_label: str | None = None
) -> None:
    plan_id = plan.get("id", "")
    title = plan.get("title") or "Untitled plan"
    status = _clean(plan.get("status") or "draft") or "draft"
    source = plan.get("source_type") or "Unknown source"
    target = plan.get("target_type") or "Unknown target"
    suffix = f"  {DIM}for org {context_label}{RESET}" if context_label else ""
    print(
        f"{CYAN}{plan_id}{RESET}  {title}  {DIM}[{status}]{RESET}  {source} -> {target}{suffix}"
    )
    if verbose:
        if plan.get("summary"):
            print(f"  {plan['summary']}")
        if plan.get("template_key"):
            print(f"  {DIM}Template:{RESET} {plan['template_key']}")
        if plan.get("org_id"):
            print(f"  {DIM}Org:{RESET} {plan['org_id']}")
        if plan.get("updated_at"):
            print(
                f"  {DIM}Updated:{RESET} {_format_verbose_timestamp(plan.get('updated_at'))}"
            )


def _print_plan_detail(plan: dict, context_label: str | None = None) -> None:
    _print_plan_summary(plan, verbose=True, context_label=context_label)
    if plan.get("import_source"):
        print(f"{DIM}Import source:{RESET} {plan['import_source']}")
    if plan.get("migration_id"):
        print(f"{DIM}Migration:{RESET} {plan['migration_id']}")
    steps = plan.get("plan_steps") or []
    if steps:
        print(f"{DIM}Steps:{RESET}")
        for step in steps:
            title = step.get("title") or "Untitled step"
            status = _clean(step.get("status") or "todo") or "todo"
            owner = _clean(step.get("owner")) or "Unassigned"
            step_id = _clean(step.get("id"))
            line = f"  {step.get('order', '?')}. {title} [{status}]"
            if step_id:
                line = f"{line} {DIM}(task-id: {step_id}){RESET}"
            print(line)
            print(f"     Owner: {owner}")
            if step.get("description"):
                print(f"     {step['description']}")
            if step.get("blocked_reason"):
                print(f"     Blocked: {step['blocked_reason']}")
            links = step.get("artifact_links") or []
            if links:
                print("     Artifacts:")
                for link in links:
                    print(f"       - {link}")
            if step.get("notes"):
                print(f"     Notes: {step['notes']}")
    else:
        print(f"{DIM}Steps:{RESET} none")


def _plan_analysis(plan: dict) -> dict:
    decisions = plan.get("decisions") or {}
    return decisions if isinstance(decisions, dict) else {}


def _truncate_text(value: str, limit: int = 110) -> str:
    text = _clean(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"


_TASK_NOTE_TIMESTAMP_PREFIX_RE = re.compile(
    r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC\]\s*"
)


def _format_task_note_for_terminal(value: str) -> str:
    return _TASK_NOTE_TIMESTAMP_PREFIX_RE.sub("", _clean(value))


def _extract_source_titles(source: dict, limit: int = 3) -> list[str]:
    titles: list[str] = []
    for item in source.get("sources") or []:
        title = _clean((item or {}).get("title"))
        if title:
            titles.append(title)
    if titles:
        return titles[:limit]

    detail = _clean(source.get("detail"))
    if not detail:
        return []

    for raw_line in detail.splitlines():
        line = _clean(raw_line)
        if not line:
            continue
        match = re.match(r"^(.*?)(?:\s*(?:\u2014|->)\s*)?(https?://\S+)\s*$", line)
        if match:
            label = _clean(match.group(1))
            if label:
                titles.append(label)
        elif not re.search(r"https?://", line):
            titles.append(line)
        if len(titles) >= limit:
            break
    return titles[:limit]


def _print_plan_enrichment(plan: dict, *, indent: str = "  ") -> None:
    sources = plan.get("enrichment_sources") or []
    if not sources:
        return
    names = [_clean(s.get("name")) for s in sources if _clean(s.get("name"))]
    if names:
        print(f"{indent}{DIM}Enriched by: {', '.join(names)}{RESET}")


def _print_plan_analysis(
    plan: dict, *, indent: str = "  ", item_limit: int = 3
) -> None:
    analysis = _plan_analysis(plan)
    if not analysis:
        return

    confidence = analysis.get("confidence_score")
    confidence_basis = analysis.get("confidence_basis")
    risks = analysis.get("risks") if isinstance(analysis.get("risks"), list) else []
    unknowns = (
        analysis.get("unknowns") if isinstance(analysis.get("unknowns"), list) else []
    )

    summary_parts: list[str] = []
    if confidence is not None:
        tag = " \u2713" if confidence_basis == "outcome_based" else ""
        summary_parts.append(f"confidence: {confidence}%{tag}")
    if risks:
        summary_parts.append(f"{len(risks)} risk{'s' if len(risks) != 1 else ''}")
    if unknowns:
        summary_parts.append(
            f"{len(unknowns)} open question{'s' if len(unknowns) != 1 else ''}"
        )
    if summary_parts:
        print(f"{indent}{DIM}Analysis: {' \u00b7 '.join(summary_parts)}{RESET}")

    if risks:
        print(f"{indent}{RED}Top risks:{RESET}")
        for risk in risks[:item_limit]:
            title = _clean((risk or {}).get("title"))
            description = _clean((risk or {}).get("description"))
            print(
                f"{indent}  - {_truncate_text(title or description or 'Unspecified risk')}"
            )

    if unknowns:
        print(f"{indent}{YELLOW}Open questions:{RESET}")
        for unknown in unknowns[:item_limit]:
            question = _clean((unknown or {}).get("question"))
            summary = _clean((unknown or {}).get("summary"))
            print(
                f"{indent}  - {_truncate_text(question or summary or 'Unspecified question')}"
            )


def _print_task_detail(
    plan: dict, task_id: str | None = None, title_hint: str | None = None
) -> None:
    from .cli import _format_duration

    steps = plan.get("plan_steps") or []
    task = None
    if task_id:
        task = next(
            (step for step in steps if _clean(step.get("id")) == _clean(task_id)), None
        )
    if task is None and title_hint:
        task = next(
            (
                step
                for step in reversed(steps)
                if _clean(step.get("title")) == _clean(title_hint)
            ),
            None,
        )
    if task is None and steps:
        task = steps[-1]
    if task is None:
        print("Task updated, but no task details were returned.")
        return
    print(f"{DIM}Plan:{RESET} {plan.get('title') or plan.get('id') or 'Untitled plan'}")
    if plan.get("id"):
        print(f"{DIM}Plan ID:{RESET} {plan['id']}")
    print(f"{DIM}Task:{RESET} {task.get('title') or 'Untitled task'}")
    task_id_value = _clean(task.get("id"))
    if task_id_value:
        print(f"{DIM}Task ID:{RESET} {task_id_value}")
    print(f"{DIM}Status:{RESET} {_clean(task.get('status') or 'todo') or 'todo'}")
    print(f"{DIM}Owner:{RESET} {_clean(task.get('owner')) or 'Unassigned'}")
    depends_on = [_clean(dep_id) for dep_id in (task.get("depends_on") or []) if _clean(dep_id)]
    if depends_on:
        print(f"{DIM}Depends on:{RESET} {', '.join(depends_on)}")
    print(
        f"{DIM}Execution mode:{RESET} "
        f"{'parallelizable' if task.get('parallelizable') else 'serial'}"
    )
    if task.get("blocked_reason"):
        print(f"{DIM}Blocked:{RESET} {task['blocked_reason']}")
    if task.get("notes"):
        print(f"{DIM}Notes:{RESET}")
        for line in str(task["notes"]).splitlines()[-4:]:
            cleaned = _clean(line)
            if cleaned:
                print(f"  - {cleaned}")
    links = task.get("artifact_links") or []
    if links:
        print(f"{DIM}Artifacts:{RESET}")
        for link in links:
            print(f"  - {link}")
    execution_log = [
        entry for entry in (task.get("execution_log") or []) if isinstance(entry, dict)
    ]
    if execution_log:
        print(f"{DIM}Recent activity:{RESET}")
        for entry in execution_log[-8:]:
            timestamp = _clean(entry.get("timestamp"))
            label_parts = [
                _clean(entry.get("event_type")) or "activity",
                _clean(entry.get("status")),
                _clean(entry.get("phase")),
            ]
            label = " \u00b7 ".join(part for part in label_parts if part)
            details: list[str] = []
            message = _clean(entry.get("message"))
            error = _clean(entry.get("error"))
            files = [_clean(path) for path in (entry.get("files") or []) if _clean(path)]
            if message:
                details.append(message)
            if error and error != message:
                details.append(f"error: {error}")
            if files:
                details.append(f"files: {', '.join(files[:5])}")
            metric_bits: list[str] = []
            metrics = entry.get("metrics") or {}
            if isinstance(metrics, dict):
                if metrics.get("duration_seconds") is not None:
                    metric_bits.append(
                        f"{_format_duration(float(metrics['duration_seconds']))}"
                    )
                if metrics.get("tokens_used") is not None:
                    metric_bits.append(f"{int(metrics['tokens_used']):,} tokens")
                if metrics.get("cost_usd") is not None:
                    metric_bits.append(f"${float(metrics['cost_usd']):.4f}")
            if metric_bits:
                details.append(", ".join(metric_bits))
            prefix = f"  - [{timestamp}] " if timestamp else "  - "
            print(prefix + (label or "activity"))
            for detail in details[:3]:
                print(f"      {detail}")


def _print_task_update_summary(plan: dict, task_id: str, payload: dict) -> None:
    steps = plan.get("plan_steps") or []
    task = next(
        (step for step in steps if _clean(step.get("id")) == _clean(task_id)),
        None,
    )
    task_data = task or {}
    if task is None:
        task_title = task_id or "task"
        status = _clean(payload.get("status")) or "updated"
    else:
        task_title = task.get("title") or task_id or "Untitled task"
        status = _clean(task.get("status") or payload.get("status") or "todo") or "todo"
    print(f"Updated task {task_title} [{status}].")
    changed_bits: list[str] = []
    if "owner" in payload:
        changed_bits.append(f"Owner: {_clean(task_data.get('owner')) or 'Unassigned'}")
    if "blocked_reason" in payload:
        blocked_reason = _clean(task_data.get("blocked_reason"))
        changed_bits.append(
            f"Blocked: {blocked_reason}" if blocked_reason else "Blocked cleared"
        )
    if "artifact_links" in payload:
        links = task_data.get("artifact_links") or []
        changed_bits.append(f"Artifacts: {len(links)}")
    if "notes" in payload:
        changed_bits.append("Notes updated")
    if changed_bits:
        print("  " + " | ".join(changed_bits))


def _print_task_feedback_events(plan: dict) -> None:
    events = list(plan.get("task_feedback_events") or [])
    if not events:
        print("No execution history found for this migration yet.")
        return
    print(f"{DIM}Plan:{RESET} {plan.get('title') or plan.get('id') or 'Untitled plan'}")
    if plan.get("id"):
        print(f"{DIM}Plan ID:{RESET} {plan['id']}")
    if plan.get("migration_id"):
        print(f"{DIM}Migration ID:{RESET} {plan['migration_id']}")
    print(f"{DIM}Audit Trail:{RESET}")
    for event in reversed(events):
        event_type = _clean(event.get("event_type")) or "updated"
        task_title = _clean(event.get("task_title")) or "Untitled task"
        task_id = _clean(event.get("task_id"))
        created_at = _clean(event.get("created_at"))
        source = _clean(event.get("source")) or "unknown"
        header = f"  - {task_title} [{event_type}]"
        if task_id:
            header = f"{header} {DIM}(task-id: {task_id}){RESET}"
        if created_at:
            header = f"{header}  {DIM}{created_at}{RESET}"
        print(header)
        print(f"    Source: {source}")
        feedback_reason = _clean(event.get("feedback_reason"))
        if feedback_reason:
            print(f"    Reason: {feedback_reason}")
        changed_fields = event.get("changed_fields") or []
        if changed_fields:
            print(f"    Changed: {', '.join(str(field) for field in changed_fields)}")


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def _extract_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        if isinstance(detail, list) and detail:
            first = detail[0]
            if isinstance(first, dict):
                msg = first.get("msg") or first.get("message")
                if isinstance(msg, str) and msg.strip():
                    return msg.strip()
    text = response.text.strip()
    if text:
        return text
    return response.reason_phrase or "Request failed"


def _print_http_error(exc: httpx.HTTPStatusError) -> None:
    response = exc.response
    detail = _extract_error_detail(response)
    status = response.status_code
    payload = {
        "status": "error",
        "code": status,
        "detail": detail,
        "path": response.request.url.path,
    }
    if _state.json:
        print_output(payload, True)
        return
    print(f"{RED}Keshro API error ({status}): {detail}{RESET}", file=sys.stderr)


def _print_request_error(exc: httpx.RequestError) -> None:
    base_url = _clean(_state.api_url) or DEFAULT_API_URL
    detail = (
        f"Could not reach Keshro at {base_url}. "
        "Check that the API is running and your --api-url is correct."
    )
    payload = {"status": "error", "detail": detail}
    if _state.json:
        print_output(payload, True)
        return
    print(f"{RED}{detail}{RESET}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Saved-context display
# ---------------------------------------------------------------------------

_TEAM_CONTEXT_LABELS = {
    "team_size": "Team size",
    "experience_level": "Experience level",
    "familiarity_source": "Familiarity with source",
    "familiarity_target": "Familiarity with target",
    "expected_roles": "Migration roles involved",
    "timeline": "Timeline",
    "additional_team_notes": "Additional team notes",
}

_COST_CONTEXT_LABELS = {
    "dual_run_period": "Planned dual-run / overlap period",
    "current_platform_monthly_cost_usd": "Current platform monthly spend",
    "target_platform_monthly_cost_usd": "Expected target platform monthly spend",
    "data_transfer_volume": "Backfill / data transfer volume",
    "tooling_or_services_cost_usd": "Expected tooling / outside-services cost",
}


def _print_context_block(title: str, mapping: dict, values: dict | None) -> None:
    print(f"{CYAN}{title}{RESET}")
    if not values:
        print(f"  {DIM}(none saved){RESET}")
        return
    for key, label in mapping.items():
        if key in values and values[key] not in (None, "", []):
            print(f"  {DIM}{label}:{RESET} {values[key]}")
    extras = {k: v for k, v in values.items() if k not in mapping and v not in (None, "", [])}
    for key, value in extras.items():
        print(f"  {DIM}{key}:{RESET} {value}")


def _show_saved_context(*, scope: str) -> int:
    """Render saved team/cost context for org or user. Returns exit code."""
    from .cli import _ensure_authenticated
    from .context import _current_org_id

    _ensure_authenticated()
    with make_client(_state.api_url, _state.token) as client:
        if scope == "org":
            org_id = _current_org_id()
            if not org_id:
                print(
                    f"{YELLOW}No active org. Set one with `keshro config set --org <name>` "
                    f"or use `keshro user context` for personal context.{RESET}"
                )
                return 1
            res = client.get(f"/v1/orgs/{org_id}")
            res.raise_for_status()
            body = res.json() or {}
            label = body.get("name") or org_id
        else:
            res = client.get("/v1/auth/me")
            res.raise_for_status()
            body = res.json() or {}
            label = body.get("email") or body.get("name") or "you"
    if _state.json:
        print_output(
            {
                "scope": scope,
                "scope_label": label,
                "team_context": body.get("team_context") or None,
                "cost_context": body.get("cost_context") or None,
            },
            True,
        )
        return 0
    header = f"Saved context for {YELLOW}{label}{RESET} ({scope}):"
    print(header)
    _print_context_block("Team", _TEAM_CONTEXT_LABELS, body.get("team_context"))
    _print_context_block("Cost", _COST_CONTEXT_LABELS, body.get("cost_context"))
    print(
        f"\n{DIM}This is auto-applied to new "
        f"{'org' if scope == 'org' else 'personal'} migrations and projects "
        f"so you aren't asked again.{RESET}"
    )
    return 0


def _clear_saved_context(
    *, scope: str, clear_team: bool, clear_cost: bool, clear_all: bool
) -> int:
    """PATCH the org or user with team_context/cost_context set to {} (clear).
    The backend treats empty dict as 'forget this so the clarifier asks again
    on the next migration / project'."""
    from .cli import _ensure_authenticated
    from .context import _current_org_id

    if clear_all:
        clear_team = True
        clear_cost = True
    if not (clear_team or clear_cost):
        print(
            f"{YELLOW}Specify what to clear: --clear-team, --clear-cost, or --clear-all.{RESET}"
        )
        return 1
    _ensure_authenticated()
    payload: dict = {}
    if clear_team:
        payload["team_context"] = {}
    if clear_cost:
        payload["cost_context"] = {}
    with make_client(_state.api_url, _state.token) as client:
        if scope == "org":
            org_id = _current_org_id()
            if not org_id:
                print(
                    f"{YELLOW}No active org. Set one with `keshro config set --org <name>` "
                    f"or use `keshro user context` for personal context.{RESET}"
                )
                return 1
            res = client.patch(f"/v1/orgs/{org_id}", json=payload)
        else:
            res = client.patch("/v1/auth/me", json=payload)
        res.raise_for_status()
    cleared = []
    if clear_team:
        cleared.append("team")
    if clear_cost:
        cleared.append("cost")
    print(
        f"{GREEN}\u2713{RESET} Cleared saved {' + '.join(cleared)} context "
        f"({'org' if scope == 'org' else 'personal'}). "
        f"Next migration / project will re-ask."
    )
    return 0
