import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import urlencode

import click
import httpx
import typer

from . import __version__
from .auth import cmd_auth_login, cmd_auth_logout
from .client import get_default_org_id, make_async_client, make_client, print_output
from .config import DEFAULT_API_URL, load_auth, update_auth


RESET = "\033[0m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"


# ---------------------------------------------------------------------------
# Global state – populated by the app callback before any subcommand runs
# ---------------------------------------------------------------------------


@dataclass
class _State:
    api_url: str = ""
    token: str | None = None
    json: bool = False


_state = _State()


# ---------------------------------------------------------------------------
# Typer apps
# ---------------------------------------------------------------------------

app = typer.Typer(add_completion=False, no_args_is_help=False)
auth_app = typer.Typer(help="Authentication")
plan_app = typer.Typer(help="Plan management")
task_app = typer.Typer(help="Task management")
migration_app = typer.Typer(help="Migration project management")
config_app = typer.Typer(help="Configuration", invoke_without_command=True)
plan_task_app = typer.Typer(help="Plan task management")

app.add_typer(plan_app, name="plan")
app.add_typer(task_app, name="task")
app.add_typer(migration_app, name="migration")
app.add_typer(config_app, name="config")
plan_app.add_typer(plan_task_app, name="task")


# ---------------------------------------------------------------------------
# Helpers (logic unchanged from argparse version)
# ---------------------------------------------------------------------------


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _current_org_id(org_id: str | None = None) -> str | None:
    resolved = _clean(get_default_org_id(org_id))
    return resolved or None


def _current_plan_id(plan_id: str | None = None) -> str | None:
    auth = load_auth()
    resolved = _clean(plan_id or auth.get("default_plan_id"))
    return resolved or None


def _current_context_label() -> str | None:
    auth = load_auth()
    return _clean(auth.get("default_org_name") or auth.get("default_org_id")) or None


def _current_plan_label() -> str | None:
    auth = load_auth()
    return _clean(auth.get("default_plan_title") or auth.get("default_plan_id")) or None


def _require_plan_context(plan_id: str | None = None) -> str:
    resolved = _current_plan_id(plan_id)
    if resolved:
        return resolved
    raise SystemExit(
        "Plan ID required. Pass <plan-id> or save one with `keshro config set --plan-id <plan-id>`."
    )


def _set_default_plan_after_create(plan: dict) -> None:
    if _state.json:
        return
    plan_id = _clean(plan.get("id"))
    if not plan_id:
        return
    plan_title = _clean(plan.get("title")) or plan_id
    current_plan_id = _current_plan_id()
    if current_plan_id == plan_id:
        return
    update_auth({"default_plan_id": plan_id, "default_plan_title": plan_title})
    print(f"Saved default plan: {plan_title}")


def _resolve_plan_context(plan_id: str | None) -> tuple[str | None, str | None]:
    explicit_id = _clean(plan_id)
    if not explicit_id:
        return None, None
    with make_client(_state.api_url, _state.token) as client:
        res = client.get(f"/api/v1/plans/{explicit_id}")
        res.raise_for_status()
        plan = res.json()
    return explicit_id, _clean(plan.get("title")) or explicit_id


def _resolve_org_context(
    org_id: str | None = None, org_name: str | None = None
) -> tuple[str | None, str | None]:
    explicit_id = _clean(org_id)
    if explicit_id:
        return explicit_id, None
    explicit_name = _clean(org_name)
    if not explicit_name:
        return None, None
    with make_client(_state.api_url, _state.token) as client:
        res = client.get("/api/v1/orgs")
        res.raise_for_status()
        orgs = res.json()
    needle = explicit_name.lower()
    exact_matches = [org for org in orgs if _clean(org.get("name")).lower() == needle]
    if len(exact_matches) == 1:
        match = exact_matches[0]
        return (
            _clean(match.get("id")) or None,
            _clean(match.get("name")) or explicit_name,
        )
    partial_matches = [org for org in orgs if needle in _clean(org.get("name")).lower()]
    if not partial_matches:
        raise SystemExit(f"Workspace not found: {explicit_name}")
    if len(partial_matches) > 1:
        options = ", ".join(
            sorted(
                _clean(org.get("name"))
                for org in partial_matches
                if _clean(org.get("name"))
            )
        )
        raise SystemExit(
            f"Multiple workspaces match '{explicit_name}': {options}. Use a more specific name or --org-id."
        )
    match = partial_matches[0]
    return _clean(match.get("id")) or None, _clean(match.get("name")) or explicit_name


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
    migration: dict, verbose: bool = False, context_label: str | None = None
) -> None:
    migration_id = migration.get("id", "")
    status = _clean(migration.get("status") or "pending") or "pending"
    source = migration.get("source_type") or "Unknown source"
    target = migration.get("target_type") or "Unknown target"
    created_at = _clean(migration.get("created_at"))
    date_part = f"  {DIM}{created_at}{RESET}" if created_at else ""
    suffix = f"  {DIM}for org {context_label}{RESET}" if context_label else ""
    print(
        f"{CYAN}{migration_id}{RESET}  {source} -> {target}  {DIM}[{status}]{RESET}{date_part}{suffix}"
    )
    if verbose:
        if migration.get("outcome_status"):
            print(f"  {DIM}Outcome:{RESET} {migration['outcome_status']}")
        if migration.get("confidence_score") is not None:
            print(f"  {DIM}Confidence:{RESET} {migration['confidence_score']}")


def _print_migration_detail(migration: dict, context_label: str | None = None) -> None:
    _print_migration_summary(migration, verbose=True, context_label=context_label)
    if migration.get("migration_mode"):
        print(f"{DIM}Mode:{RESET} {migration['migration_mode']}")
    if migration.get("input_method"):
        print(f"{DIM}Input method:{RESET} {migration['input_method']}")
    if migration.get("org_id"):
        print(f"{DIM}Org:{RESET} {migration['org_id']}")
    if migration.get("github_url"):
        print(f"{DIM}GitHub URL:{RESET} {migration['github_url']}")
    if migration.get("resource_url"):
        print(f"{DIM}Resource URL:{RESET} {migration['resource_url']}")
    if migration.get("confidence_explanation"):
        print(
            f"{DIM}Confidence explanation:{RESET} {migration['confidence_explanation']}"
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
    if migration.get("notes"):
        print(f"{DIM}Notes:{RESET} {migration['notes']}")
    steps = migration.get("migration_steps") or []
    if steps:
        print(f"{DIM}Steps:{RESET}")
        for step in steps:
            title = step.get("title") or "Untitled step"
            order = step.get("order", "?")
            print(f"  {order}. {title}")
            if step.get("description"):
                print(f"     {step['description']}")


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
            print(f"  {DIM}Updated:{RESET} {plan['updated_at']}")


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


def _print_task_detail(
    plan: dict, task_id: str | None = None, title_hint: str | None = None
) -> None:
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
    if task.get("blocked_reason"):
        print(f"{DIM}Blocked:{RESET} {task['blocked_reason']}")
    links = task.get("artifact_links") or []
    if links:
        print(f"{DIM}Artifacts:{RESET}")
        for link in links:
            print(f"  - {link}")


def _print_task_update_summary(plan: dict, task_id: str, payload: dict) -> None:
    steps = plan.get("plan_steps") or []
    task = next(
        (step for step in steps if _clean(step.get("id")) == _clean(task_id)),
        None,
    )
    if task is None:
        task_title = task_id or "task"
        status = _clean(payload.get("status")) or "updated"
    else:
        task_title = task.get("title") or task_id or "Untitled task"
        status = _clean(task.get("status") or payload.get("status") or "todo") or "todo"
    print(f"Updated task {task_title} [{status}].")
    changed_bits: list[str] = []
    if "owner" in payload:
        changed_bits.append(f"Owner: {_clean(task.get('owner')) or 'Unassigned'}")
    if "blocked_reason" in payload:
        blocked_reason = _clean(task.get("blocked_reason"))
        changed_bits.append(
            f"Blocked: {blocked_reason}" if blocked_reason else "Blocked cleared"
        )
    if "artifact_links" in payload:
        links = task.get("artifact_links") or []
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


def _parse_field_assignments(values: list[str] | None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw in values or []:
        item = _clean(raw)
        if not item:
            continue
        if "=" not in item:
            raise SystemExit(
                f"Invalid --field value '{item}'. Use --field field_id=value."
            )
        key, value = item.split("=", 1)
        field_id = _clean(key)
        field_value = value.strip()
        if not field_id or not field_value:
            raise SystemExit(
                f"Invalid --field value '{item}'. Use --field field_id=value."
            )
        parsed[field_id] = field_value
    return parsed


def _normalize_prompt_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean(value).lower()).strip()


def _parse_discovery_key_values(raw: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in raw.splitlines():
        match = re.match(r"^\s*[-*]?\s*([^:]+):\s*(.*)$", line)
        if not match:
            continue
        key = _normalize_prompt_key(match.group(1))
        value = match.group(2).strip()
        if key:
            parsed[key] = value
    return parsed


def _match_select_option(field: dict, value: str) -> str:
    options = [
        str(option).strip()
        for option in (field.get("options") or [])
        if str(option).strip()
    ]
    if not options:
        return value.strip()
    needle = value.strip().lower()
    for option in options:
        if option.lower() == needle:
            return option
    for option in options:
        if needle in option.lower() or option.lower() in needle:
            return option
    return value.strip()


def _build_path_discovery_prompt(template: dict) -> str:
    source = _clean(template.get("source")) or "Source"
    target = _clean(template.get("target")) or "Target"
    lines = [
        f"You are the migration discovery analyst for a {source} -> {target} migration.",
        "",
        "Gather the highest-signal migration facts before planning begins.",
        "Replace the blanks below with concrete answers. Use `Unknown` when you cannot verify a value.",
        "",
        "## Versions",
        "- Source version:",
        "- Target version:",
        "",
        f"## {source} to {target} details",
    ]
    for field in template.get("fields") or []:
        label = _clean(field.get("label")) or _clean(field.get("id")) or "Detail"
        hint = _clean(field.get("hint"))
        option_text = ""
        options = [
            str(option).strip()
            for option in (field.get("options") or [])
            if str(option).strip()
        ]
        if options:
            option_text = f" Options: {' | '.join(options)}"
        suffix = f" Hint: {hint}" if hint else ""
        lines.append(f"- {label}:{option_text}{suffix}")
    lines.extend(
        [
            "",
            "## Additional context",
            "- Anything else that materially affects risk, effort, validation, cutover, rollback, or delivery:",
            "",
        ]
    )
    return "\n".join(lines)


def _build_agent_discovery_prompt(template: dict) -> str:
    source = _clean(template.get("source")) or "Source"
    target = _clean(template.get("target")) or "Target"
    discovery_commands = template.get("discovery_commands") or []
    parts = [
        f"You are helping create a Keshro migration for {source} -> {target}.",
        "Inspect the current workspace and gather the path-specific migration facts.",
        "Return only the completed markdown template below with the same headings and field labels.",
        "Do not wrap the answer in code fences. Do not add commentary before or after the template.",
        "Use `Unknown` for values you cannot verify from the repository, configs, or local docs.",
    ]
    if discovery_commands:
        parts.append("")
        parts.append(
            "Try running these commands to discover relevant context. "
            "These are best-effort — if a command fails (tool not installed, no access, permission denied, etc.), "
            "note which command failed and why in a single line, then continue with what you can find from files and configs. "
            "Do not stop or error out if a discovery command fails."
        )
        for cmd in discovery_commands:
            parts.append(f"  $ {cmd}")
    parts.append("")
    parts.append(_build_path_discovery_prompt(template))
    return "\n".join(parts)


def _collect_discovery_answer_from_claude(
    template: dict, work_dir: str | None = None
) -> str:
    prompt = _build_agent_discovery_prompt(template)
    return _run_prompt_in_claude(
        prompt,
        missing_env_message=(
            "This command needs to run inside a coding agent so it can scan your codebase.\n"
            "Run it from your agent's terminal, or use the prompt copy/paste path in Keshro instead."
        ),
        missing_binary_message=(
            "Could not find a coding agent binary. Make sure you're running this from within your agent's terminal."
        ),
        failure_message_prefix=("Coding agent returned an error: "),
        empty_message="Claude agent returned no discovery response.",
        work_dir=work_dir,
    )


def _run_prompt_in_claude(
    prompt: str,
    *,
    missing_env_message: str,
    missing_binary_message: str,
    failure_message_prefix: str,
    empty_message: str,
    work_dir: str | None = None,
) -> str:
    if sys.stdout.isatty():
        raise SystemExit(missing_env_message)
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise SystemExit(missing_binary_message)
    resolved_dir = str(Path(work_dir).resolve()) if work_dir else os.getcwd()
    result = subprocess.run(
        [
            claude_bin,
            "-p",
            prompt,
            "--output-format",
            "text",
            "--permission-mode",
            "auto",
            "--add-dir",
            resolved_dir,
            "--no-session-persistence",
        ],
        capture_output=True,
        text=True,
        cwd=resolved_dir,
        check=False,
    )
    if result.returncode != 0:
        detail = (
            _clean(result.stderr) or _clean(result.stdout) or "Coding agent failed."
        )
        raise SystemExit(f"{failure_message_prefix}{detail}")
    answer = _clean(result.stdout)
    if not answer:
        raise SystemExit(empty_message)
    return answer


def _extract_discovery_answers(template: dict, raw: str) -> dict[str, str]:
    parsed = _parse_discovery_key_values(raw)
    answers: dict[str, str] = {}
    for key_name, field_id in (
        ("source version", "source_version"),
        ("target version", "target_version"),
    ):
        value = _clean(parsed.get(key_name))
        if value and value.lower() != "unknown":
            answers[field_id] = value
    for field in template.get("fields") or []:
        field_id = _clean(field.get("id"))
        label = _clean(field.get("label"))
        if not field_id:
            continue
        matched = parsed.get(_normalize_prompt_key(label)) or parsed.get(
            _normalize_prompt_key(field_id)
        )
        value = _clean(matched)
        if not value or value.lower() == "unknown":
            continue
        answers[field_id] = (
            _match_select_option(field, value)
            if str(field.get("type") or "").strip() == "select"
            else value
        )
    return answers


def _get_migration_clarifiers(client: httpx.Client, payload: dict) -> list[dict]:
    response = client.post("/api/v1/migrations/clarifiers", json=payload)
    response.raise_for_status()
    body = response.json() or {}
    return list(body.get("questions") or [])


def _build_clarifier_prompt(
    template: dict, payload: dict, questions: list[dict]
) -> str:
    source = (
        _clean(template.get("source")) or _clean(payload.get("source_type")) or "Source"
    )
    target = (
        _clean(template.get("target")) or _clean(payload.get("target_type")) or "Target"
    )
    existing_fields = dict(payload.get("custom_fields") or {})
    existing_context = _clean(payload.get("context"))
    lines = [
        f"You are helping finalize a Keshro migration draft for {source} -> {target}.",
        "Answer the follow-up questions below using the current workspace and the already-gathered migration context.",
        "Prefer concrete answers grounded in the repository, configs, docs, and runtime clues available locally.",
        "If something still cannot be verified, use the recommended option when one exists; otherwise write `Unknown`.",
        "Return only bullet lines in the exact format `- <question id>: <answer>`.",
        "",
        "Known draft context:",
    ]
    if existing_fields:
        for key, value in existing_fields.items():
            if _clean(str(value)):
                lines.append(f"- {key}: {_clean(str(value))}")
    elif existing_context:
        lines.append("- No structured fields yet.")
    if existing_context:
        lines.extend(["", "Draft context:", existing_context])
    lines.extend(["", "Follow-up questions:"])
    for question in questions:
        prompt_id = _clean(question.get("id"))
        prompt_text = _clean(question.get("question"))
        why = _clean(question.get("why_this_matters"))
        placeholder = _clean(question.get("placeholder"))
        lines.append(f"- {prompt_id}: {prompt_text}")
        if why:
            lines.append(f"  Why it matters: {why}")
        options = list(question.get("answers") or [])
        if options:
            lines.append("  Options:")
            for option in options:
                title = _clean(option.get("answer_title")) or _clean(
                    option.get("value")
                )
                value = _clean(option.get("value"))
                suffix = " [recommended]" if option.get("recommended") else ""
                lines.append(f"  - {title}{suffix}: {value}")
        elif placeholder:
            lines.append(f"  Hint: {placeholder}")
    return "\n".join(lines)


def _collect_clarifier_answers_from_claude(
    template: dict, payload: dict, questions: list[dict], work_dir: str | None = None
) -> dict[str, str]:
    if not questions:
        return {}
    prompt = _build_clarifier_prompt(template, payload, questions)
    raw = _run_prompt_in_claude(
        prompt,
        missing_env_message=(
            "This command needs to run inside a coding agent so it can scan your codebase.\n"
            "Run it from your agent's terminal, or use the prompt copy/paste path in Keshro instead."
        ),
        missing_binary_message=(
            "Could not find a coding agent binary. Make sure you're running this from within your agent's terminal."
        ),
        failure_message_prefix="Coding agent returned an error: ",
        empty_message="Coding agent returned no clarifier answers.",
        work_dir=work_dir,
    )
    parsed = _parse_discovery_key_values(raw)
    answers: dict[str, str] = {}
    for question in questions:
        question_id = _clean(question.get("id"))
        value = _clean(parsed.get(_normalize_prompt_key(question_id)))
        if value and value.lower() != "unknown":
            answers[question_id] = value
            continue
        options = list(question.get("answers") or [])
        recommended = next(
            (option for option in options if option.get("recommended")),
            None,
        )
        if recommended:
            recommended_value = _clean(recommended.get("value"))
            if recommended_value:
                answers[question_id] = recommended_value
    return answers


def _merge_clarifier_answers(
    payload: dict, questions: list[dict], answers: dict[str, str]
) -> dict:
    if not answers:
        return payload
    field_targets = {
        _clean(question.get("id")): _clean(question.get("field_target"))
        for question in questions
        if _clean(question.get("field_target"))
    }
    custom_fields = dict(payload.get("custom_fields") or {})
    for question_id, value in answers.items():
        field_target = field_targets.get(question_id)
        if field_target and _clean(value):
            custom_fields[field_target] = _clean(value)

    clarified_lines = [
        f"- {question_id.replace('_', ' ')}: {value}"
        for question_id, value in answers.items()
        if _clean(value)
    ]
    context = _clean(payload.get("context"))
    if clarified_lines:
        block = "\n".join(["Critical clarifications", *clarified_lines])
        context = "\n\n---\n\n".join([part for part in [context, block] if part])

    next_payload = dict(payload)
    next_payload["context"] = context or None
    next_payload["custom_fields"] = custom_fields or None
    return next_payload


def _app_url_from_api_url(api_url: str) -> str:
    resolved = _clean(api_url).rstrip("/")
    if not resolved:
        return "https://app.keshro.com"
    if "localhost" in resolved or "127.0.0.1" in resolved:
        return resolved.replace("://api.", "://").replace(":8000", ":3000")
    if "api." in resolved:
        return resolved.replace("://api.", "://", 1)
    return resolved


def _encode_prefill_draft(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return encoded.rstrip("=")


def _render_prefill_handoff(
    payload: dict, template: dict, work_dir: str | None = None
) -> None:
    if _state.json:
        print_output(payload, True)
        return
    source = _clean(template.get("source")) or "Unknown source"
    target = _clean(template.get("target")) or "Unknown target"
    print(f"\nPrepared migration draft for {source} -> {target}.")
    query = urlencode(
        {
            "source": source,
            "target": target,
            "draft": _encode_prefill_draft(payload),
            "clarify": "true",
        }
    )
    url = f"{_app_url_from_api_url(_state.api_url)}/new?{query}"
    import webbrowser

    try:
        webbrowser.open(url)
    except Exception:
        pass
    base = f"{_app_url_from_api_url(_state.api_url)}/new"
    print(
        f"\nContinue here to answer follow-up questions and start the analysis:\n{base}?...\n"
    )
    print(f"Full URL (if browser didn't open):\n{url}\n")
    if not work_dir:
        print(
            "Tip: Use --dir to point to your project directory for better auto-discovery."
        )


def _connected_delivery_label_from_plan(plan: dict) -> str | None:
    providers: list[str] = []
    for step in plan.get("plan_steps") or []:
        provider = _clean(step.get("external_issue_provider")).lower()
        if provider == "linear" or step.get("linear_issue_id"):
            providers.append("Linear")
        elif provider == "jira":
            providers.append("Jira")
        elif provider == "github":
            providers.append("GitHub")
    ordered: list[str] = []
    for label in providers:
        if label not in ordered:
            ordered.append(label)
    if not ordered:
        return None
    if len(ordered) == 1:
        return f"linked {ordered[0]} work"
    if len(ordered) == 2:
        return f"linked {ordered[0]} or {ordered[1]} work"
    return f"linked {', '.join(ordered[:-1])}, or {ordered[-1]} work"


def _build_cli_agent_skill_text(
    plan_id: str | None = None,
    connected_delivery_label: str | None = None,
    migration_label: str | None = None,
) -> str:
    resolved_plan_id = plan_id or "<plan-id>"
    resolved_migration_label = migration_label or "a migration"
    return f"""You are executing {resolved_migration_label} tracked in Keshro.

IMPORTANT: Use `keshro` CLI commands to interact with Keshro. Do NOT use Keshro MCP tools — always use the CLI in your terminal instead.

The current task and plan context are provided below. Do not re-fetch them with `keshro plan view` or `keshro task next` — start working directly.

Style:
- Be concise. Do not narrate your thought process — just do the work and report what you did.
- Before running any keshro command or git checkpoint, print one short sentence explaining why (e.g. "Marking task as in progress."). Then run the command.

Treat Keshro as the live execution record. When meaningful task progress happens, write it back while the work is happening rather than waiting until the end.

During execution:
- run `keshro task start <task-id> -p {resolved_plan_id}` as soon as work begins
- use `keshro task note <task-id> -p {resolved_plan_id} -n "..."` for meaningful discoveries, decisions, or validation findings
- use `keshro task artifact <task-id> -p {resolved_plan_id} -l "<url>"` for PRs, commits, dashboards, issues, and runbooks
- use `keshro task block <task-id> -p {resolved_plan_id} -r "..."` the moment a real blocker appears
- use `keshro task unblock <task-id> -p {resolved_plan_id}` when that blocker is cleared
- use `keshro plan replan-notes {resolved_plan_id} "..."` only when the plan itself changed materially
- when you create or modify files, record them with `keshro task note` — list the specific files and what changed

When a task is done:
- record a completion note using this format: `keshro task note <task-id> -p {resolved_plan_id} -n "Files created: ... | Files modified: ... | Key decisions: ... | Acceptance criteria met: ... | Verification: ... | Next task should know: ..."`
- ask the user to confirm the task is complete before running `keshro task done`
- when marking done, report your session cost if available: `keshro task done <task-id> -p {resolved_plan_id} --cost <usd_amount> --tokens <token_count> --model <model_name>` (check your session stats for cost/token info)
- after `keshro task done`, summarize what was accomplished and ask the user if they want to continue to the next task
- do not automatically start the next task without the user's go-ahead

Ask the user first before:
- `keshro task done`
- task deletion
- major replans that change scope, sequencing, or {connected_delivery_label or "linked delivery work"}

If a keshro command fails with a connection error, retry once after 5 seconds. For any other error, tell the user what happened and continue working on the code. Do not retry more than once unless the user asks.

Rules:
- Keep updates concise, factual, and specific.
- Do not silently work around blockers or plan drift.
- Do not assume Keshro is current unless you updated it.
- If you need the full plan for context, use `keshro plan view {resolved_plan_id}`.
- If you need more detail on any task, use `keshro task view <task-id> -p {resolved_plan_id}`."""


def _get_git_state_summary(work_dir: str | None = None) -> str:
    """Detect what changed in the repo since the last keshro checkpoint."""
    cwd = work_dir or None
    try:
        # Find last keshro checkpoint commit
        last_checkpoint = subprocess.run(
            [
                "git",
                "log",
                "--oneline",
                "--grep=keshro: checkpoint",
                "-1",
                "--format=%H",
            ],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
        checkpoint_hash = (last_checkpoint.stdout or "").strip()

        if checkpoint_hash:
            # Get changes since checkpoint
            diff_stat = subprocess.run(
                ["git", "diff", "--stat", checkpoint_hash, "HEAD"],
                capture_output=True,
                text=True,
                cwd=cwd,
                check=False,
            )
            log_result = subprocess.run(
                ["git", "log", "--oneline", f"{checkpoint_hash}..HEAD"],
                capture_output=True,
                text=True,
                cwd=cwd,
                check=False,
            )
            commits = [
                el.strip()
                for el in (log_result.stdout or "").strip().splitlines()
                if el.strip()
            ]
            stat = (diff_stat.stdout or "").strip()

            if not commits and not stat:
                return ""

            lines = ["Changes since last keshro checkpoint:"]
            if commits:
                lines.append(
                    f"- {len(commits)} commit{'s' if len(commits) != 1 else ''}: {', '.join(c.split(' ', 1)[1] if ' ' in c else c for c in commits[:5])}"
                )
            if stat:
                # Get just the summary line (last line of diff --stat)
                summary_line = stat.strip().splitlines()[-1] if stat.strip() else ""
                if summary_line:
                    lines.append(f"- {summary_line.strip()}")
            return "\n".join(lines)
        else:
            # No checkpoint found — show recent status
            status = subprocess.run(
                ["git", "status", "--short"],
                capture_output=True,
                text=True,
                cwd=cwd,
                check=False,
            )
            changed = [
                el.strip()
                for el in (status.stdout or "").strip().splitlines()
                if el.strip()
            ]
            if changed:
                return f"Working tree: {len(changed)} file{'s' if len(changed) != 1 else ''} modified (no prior keshro checkpoint found)"
            return ""
    except Exception:
        return ""


def _extract_topical_context(
    target_task: dict, all_steps: list[dict], max_entries: int = 5, max_chars: int = 500
) -> list[str]:
    """Find learnings from completed tasks that share tags with the target task.

    Returns formatted lines for prompt injection, or empty list if no matches.
    """
    target_tags = {t.lower() for t in (target_task.get("tags") or [])}
    if not target_tags:
        return []

    scored: list[tuple[int, str, str, set[str]]] = []
    target_id = target_task.get("id")
    for step in all_steps:
        if step.get("status") != "completed" or step.get("id") == target_id:
            continue
        notes = (step.get("notes") or "").strip()
        if not notes:
            continue
        step_tags = {t.lower() for t in (step.get("tags") or [])}
        shared = target_tags & step_tags
        if not shared:
            continue

        # Filter out explicit handoff lines (already in the sequential handoff section)
        lines = []
        for line in notes.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            lower = stripped.lower()
            if lower.startswith("next task should know:") or lower.startswith(
                "context for next task:"
            ):
                continue
            lines.append(stripped)
        if not lines:
            continue

        content = "\n".join(lines)
        if len(content) > max_chars:
            content = content[:max_chars] + "..."
        title = step.get("title") or "Untitled"
        scored.append((len(shared), title, content, shared))

    if not scored:
        return []

    scored.sort(key=lambda x: x[0], reverse=True)
    result = []
    for _score, title, content, shared in scored[:max_entries]:
        shared_str = ", ".join(sorted(shared))
        result.append(f'- From "{title}" (shared: {shared_str}):')
        for line in content.split("\n"):
            result.append(f"  {line}")
    return result


def _build_continue_prompt(
    plan: dict,
    task: dict,
    work_dir: str | None = None,
    auto_continue: bool = False,
    session_id: str = "",
) -> str:
    resolved_plan_id = _clean(plan.get("id")) or "<plan-id>"
    connected_delivery_label = _connected_delivery_label_from_plan(plan)
    source = _clean(plan.get("source_type"))
    target = _clean(plan.get("target_type"))
    migration_label = (
        f"the {source} -> {target} migration" if source and target else "a migration"
    )
    base = _build_cli_agent_skill_text(
        plan_id=resolved_plan_id,
        connected_delivery_label=connected_delivery_label,
        migration_label=migration_label,
    )
    task_id = _clean(task.get("id")) or "<task-id>"
    task_title = _clean(task.get("title")) or "Untitled task"
    task_description = _clean(task.get("description")) or "No description provided."
    task_status = _clean(task.get("status") or "todo")
    blocked_reason = _clean(task.get("blocked_reason"))
    notes = _clean(task.get("notes"))
    artifacts = [
        _clean(link) for link in (task.get("artifact_links") or []) if _clean(link)
    ]
    # Build session history from completed/in-progress tasks
    steps = sorted(plan.get("plan_steps") or [], key=lambda s: s.get("order", 0))
    completed_steps = [s for s in steps if s.get("status") == "completed"]
    history_lines: list[str] = []
    handoff_lines: list[str] = []
    if completed_steps:
        history_lines.append("Prior progress:")
        for s in completed_steps:
            title = _clean(s.get("title")) or "Untitled"
            step_notes = _clean(s.get("notes"))
            step_artifacts = [
                _clean(a) for a in (s.get("artifact_links") or []) if _clean(a)
            ]
            line = f"- [done] {title}"
            if step_notes:
                last_note = step_notes.strip().splitlines()[-1].strip()
                if last_note:
                    line += f" — {last_note[:120]}"
                # Extract "Next task should know:" handoff notes
                # Split on both newlines and sentence boundaries to catch inline handoffs
                import re as _re

                note_fragments = _re.split(
                    r"\n|(?<=\.)\s+(?=Next task should know:|Context for next task:)",
                    step_notes,
                )
                for fragment in note_fragments:
                    stripped = fragment.strip()
                    for prefix in ("Next task should know:", "Context for next task:"):
                        idx = stripped.lower().find(prefix.lower())
                        if idx >= 0:
                            handoff = stripped[idx + len(prefix) :].strip().rstrip(".")
                            if handoff:
                                handoff_lines.append(f'- From "{title}": {handoff}')
            history_lines.append(line)
            if step_artifacts:
                history_lines.append(f"  Artifacts: {', '.join(step_artifacts[:3])}")
        remaining = len(
            [s for s in steps if s.get("status") in ("todo", "in_progress")]
        )
        history_lines.append(f"- {len(completed_steps)} done, {remaining} remaining")
        history_lines.append("")
    if handoff_lines:
        history_lines.append("Handoff from previous tasks:")
        history_lines.extend(handoff_lines)
        history_lines.append("")

    # Topical context — learnings from completed tasks that share tags with this task
    topical_lines = _extract_topical_context(task, steps)
    if topical_lines:
        history_lines.append("Related learnings from tasks in the same domain:")
        history_lines.extend(topical_lines)
        history_lines.append("")

    # Git state since last checkpoint
    git_state = _get_git_state_summary(work_dir)
    git_lines: list[str] = []
    if git_state:
        git_lines.append(git_state)
        git_lines.append("")

    # Task context first (this is what shows in the collapsed agent output preview)
    done_count = len(completed_steps)
    total_count = len(steps)
    progress_line = f"[{done_count}/{total_count} done]" if done_count > 0 else ""
    task_block = [
        f"Task: {task_title} {progress_line}".strip(),
        f"Description: {task_description}",
        f"Status: {task_status}",
        f"Plan: {resolved_plan_id} | Task ID: {task_id} | Session: {session_id}",
    ]
    if blocked_reason:
        task_block.append(f"Blocker: {blocked_reason}")
    if notes:
        task_block.append(f"Notes: {notes}")
    if artifacts:
        task_block.append(f"Artifacts: {', '.join(artifacts)}")
    if work_dir:
        task_block.append(f"Project directory: {work_dir}")
    depends_on = task.get("depends_on") or []
    if depends_on:
        dep_titles = []
        for dep_id in depends_on:
            dep_step = next((s for s in steps if s.get("id") == dep_id), None)
            dep_titles.append(
                f"{dep_step.get('title', dep_id)} [{dep_step.get('status', '?')}]"
                if dep_step
                else dep_id
            )
        task_block.append(f"Depends on: {', '.join(dep_titles)}")
    is_parallelizable = task.get("parallelizable", False)
    if is_parallelizable:
        task_block.append("Parallelizable: yes")

    continuation = [
        "",
        "Continue from this task now.",
        f'- When starting this task, use: `keshro task start {task_id} -p {resolved_plan_id} --reason "session:{session_id}"`',
        f'- Before starting work, create a git checkpoint so changes can be rolled back if needed: `git add -A && git commit -m "keshro: checkpoint before {task_title}" --allow-empty`',
        "- Before writing code, briefly tell the user what this task involves and which files you expect to touch.",
        "- Read existing files relevant to this task to understand the current state before making changes.",
        "- If this task is blocked, do not automatically move to the next task unless the plan clearly supports parallel or out-of-order work.",
        "- If you continue execution, keep Keshro updated as you work.",
        "- Before marking a task done, verify your changes: run linters, check syntax, or run relevant tests if they exist. Record the validation result in your completion note under `Verification:`.",
        "- If the task has acceptance criteria, your completion note must explicitly include `Acceptance criteria met:` and `Verification:` before `keshro task done` will succeed.",
    ]
    if auto_continue:
        continuation.append(
            "- AUTO-CONTINUE MODE: After completing each task, automatically pull the next task with "
            f"`keshro task next -p {resolved_plan_id}` and continue working. "
            "Still create checkpoints, record notes, and mark tasks done — but do not pause to ask the user between tasks. "
            "If a task fails (tests don't pass, code doesn't compile, validation fails), mark it blocked with "
            f'`keshro task block <task-id> -p {resolved_plan_id} -r "..."` and stop. '
            "Tell the user what failed and why. Do not skip to the next task."
        )

    if is_parallelizable:
        continuation.extend(
            [
                "",
                "PARALLEL TASK: This task is marked as parallelizable. Before starting the work itself:",
                "1. Tell the user: 'This task can be parallelized. I will split it into sub-tasks that other agents can pick up.'",
                "2. Analyze the task to identify independent units of work (e.g., separate files, independent components, distinct modules).",
                "3. For each independent unit, create a sub-task using:",
                f'   `keshro task plan {resolved_plan_id} --title "<sub-task title>" --description "<what to do>" -o "unassigned"`',
                f"4. Record a note on this parent task: `keshro task note {task_id} -p {resolved_plan_id} "
                f'-n "Split into N sub-tasks: <list sub-task titles>. Other agents can pick these up with keshro continue."`',
                f"5. Mark this parent task as completed: `keshro task done {task_id} -p {resolved_plan_id}`",
                "6. Then pick up and start working on the first sub-task yourself.",
                "",
                "The sub-tasks will appear as new todo items in the plan. Other agents running `keshro continue` will automatically pick them up.",
                "Each sub-task should be independently executable — no sub-task should depend on another sub-task's output.",
            ]
        )

    parts = [
        *task_block,
        "",
        *git_lines,
        *history_lines,
        base,
        *continuation,
    ]
    return "\n".join(parts)


def _task_title_slug(title: str) -> str:
    """Convert a task title to a branch-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:50] if slug else "task"


def _build_parallel_prompt(
    plan: dict, task: dict, total_agents: int, work_dir: str | None = None
) -> str:
    """Build a prompt for an unattended parallel agent working on a single task."""
    base_prompt = _build_continue_prompt(
        plan, task, work_dir=work_dir, auto_continue=False
    )
    resolved_plan_id = _clean(plan.get("id")) or "<plan-id>"
    task_id = _clean(task.get("id")) or "<task-id>"
    task_title = _clean(task.get("title")) or "Untitled task"
    branch_name = f"keshro/{_task_title_slug(task_title)}"

    parallel_context = "\n".join(
        [
            "",
            "PARALLEL EXECUTION MODE:",
            f"- You are one of {total_agents} agents running concurrently in isolated git worktrees.",
            "- You are responsible for exactly ONE task. Complete it, then exit. Do NOT pull the next task.",
            f"- Create your changes on a branch named `{branch_name}`.",
            "- Other agents are working on other tasks simultaneously — note any potential file conflicts in your completion notes.",
            "- Do not ask the user for confirmation — execute autonomously.",
            f"- When done, mark the task complete: `keshro task done {task_id} -p {resolved_plan_id}`",
            f'- If the task fails, mark it blocked: `keshro task block {task_id} -p {resolved_plan_id} -r "reason"`',
        ]
    )
    return base_prompt + parallel_context


# ---------------------------------------------------------------------------
# Parallel execution engine
# ---------------------------------------------------------------------------


@dataclass
class AgentResult:
    task_id: str
    task_title: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    cost_usd: float = 0.0
    tokens_used: int = 0
    model: str = ""


async def _mark_task_status_async(
    client: httpx.AsyncClient,
    plan_id: str,
    task_id: str,
    status: str,
    notes: str | None = None,
    blocked_reason: str | None = None,
) -> None:
    body: dict = {"status": status}
    if notes:
        body["notes"] = notes[:500]
    if blocked_reason:
        body["blocked_reason"] = blocked_reason[:500]
    try:
        res = await client.patch(f"/api/v1/plans/{plan_id}/tasks/{task_id}", json=body)
        res.raise_for_status()
    except Exception:
        pass  # don't crash agents over API errors


async def _launch_single_agent(
    task: dict,
    plan: dict,
    plan_id: str,
    work_dir: str,
    total_agents: int,
    semaphore: asyncio.Semaphore,
    api_client: httpx.AsyncClient,
    session_id: str = "",
) -> AgentResult:
    task_id = _clean(task.get("id")) or "unknown"
    task_title = _clean(task.get("title")) or "Untitled"
    worktree_name = f"keshro-{task_id[:8]}"
    prompt = _build_parallel_prompt(plan, task, total_agents, work_dir=work_dir)

    claude_bin = shutil.which("claude")
    if not claude_bin:
        return AgentResult(
            task_id=task_id,
            task_title=task_title,
            exit_code=127,
            stdout="",
            stderr="claude binary not found",
            duration_seconds=0,
        )

    async with semaphore:
        await _mark_task_status_async(api_client, plan_id, task_id, "in_progress")
        # Report start with session ID via agent API
        try:
            await api_client.post(
                f"/api/v1/agent/plans/{plan_id}/task-event",
                json={
                    "task_id": task_id,
                    "event": "start",
                    "agent_session_id": session_id,
                },
            )
        except Exception:
            pass

        # Register with Collaborator if available
        collab_session_id = f"keshro-{task_id}"
        try:
            from .collaborator import is_available, notify, session_end, session_start

            collab_active = is_available()
            if collab_active:
                session_start(collab_session_id, work_dir)
        except Exception:
            collab_active = False

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                claude_bin,
                "-p",
                prompt,
                "--worktree",
                worktree_name,
                "--output-format",
                "json",
                "--permission-mode",
                "auto",
                "--no-session-persistence",
                "--name",
                f"keshro: {task_title[:40]}",
                "--add-dir",
                work_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir,
            )
            stdout_bytes, stderr_bytes = await proc.communicate()
            exit_code = proc.returncode or 0
        except Exception as exc:
            if collab_active:
                try:
                    session_end(collab_session_id)
                except Exception:
                    pass
            return AgentResult(
                task_id=task_id,
                task_title=task_title,
                exit_code=1,
                stdout="",
                stderr=str(exc),
                duration_seconds=time.monotonic() - start,
            )

        duration = time.monotonic() - start
        stdout_text = (stdout_bytes or b"").decode(errors="replace").strip()
        stderr_text = (stderr_bytes or b"").decode(errors="replace").strip()

        # Parse cost and token data from Claude's JSON output
        cost_usd = 0.0
        tokens_used = 0
        model_name = ""
        try:
            claude_output = json.loads(stdout_text)
            cost_usd = claude_output.get("total_cost_usd", 0) or 0
            usage = claude_output.get("usage", {})
            tokens_used = (
                (usage.get("input_tokens", 0) or 0)
                + (usage.get("cache_read_input_tokens", 0) or 0)
                + (usage.get("cache_creation_input_tokens", 0) or 0)
                + (usage.get("output_tokens", 0) or 0)
            )
            model_usage = claude_output.get("modelUsage", {})
            if model_usage:
                model_name = next(iter(model_usage.keys()), "")
            # Extract the text result for notes
            result_text = claude_output.get("result", "")
        except (json.JSONDecodeError, AttributeError):
            result_text = stdout_text

        # Report cost via the agent API
        cost_event: dict = {}
        if cost_usd > 0 or tokens_used > 0:
            cost_event = {
                "tokens_used": tokens_used,
                "model": model_name,
                "cost_usd": cost_usd,
            }
            try:
                await api_client.post(
                    f"/api/v1/agent/plans/{plan_id}/task-event",
                    json={
                        "task_id": task_id,
                        "event": "note",
                        "note": f"Agent cost: ${cost_usd:.4f} ({tokens_used:,} tokens, {model_name})",
                        "agent_session_id": session_id,
                        **cost_event,
                    },
                )
            except Exception:
                pass

        if exit_code == 0:
            # Build a detailed completion note
            cost_parts = [f"{duration:.0f}s"]
            if tokens_used > 0:
                cost_parts.append(f"{tokens_used:,} tokens")
            if model_name:
                cost_parts.append(model_name)
            if cost_usd > 0:
                cost_parts.append(f"${cost_usd:.4f}")
            note = f"Completed by parallel agent in {' | '.join(cost_parts)}"
            await _mark_task_status_async(
                api_client, plan_id, task_id, "completed", notes=note
            )
            # Report structured metrics via agent API
            try:
                await api_client.post(
                    f"/api/v1/agent/plans/{plan_id}/task-event",
                    json={
                        "task_id": task_id,
                        "event": "done",
                        "agent_session_id": session_id,
                        "duration_seconds": duration,
                        "tokens_used": tokens_used,
                        "cost_usd": cost_usd,
                        "model": model_name,
                    },
                )
            except Exception:
                pass
        else:
            reason = stderr_text[:200] or result_text[:200] or "Agent exited with error"
            await _mark_task_status_async(
                api_client, plan_id, task_id, "blocked", blocked_reason=reason
            )

        # End Collaborator session + notify
        if collab_active:
            try:
                session_end(collab_session_id)
                if exit_code == 0:
                    notify(f"✓ {task_title[:50]} completed ({duration:.0f}s)")
                else:
                    notify(f"✗ {task_title[:50]} blocked")
            except Exception:
                pass

        return AgentResult(
            task_id=task_id,
            task_title=task_title,
            exit_code=exit_code,
            stdout=stdout_text,
            stderr=stderr_text,
            duration_seconds=duration,
            cost_usd=cost_usd,
            tokens_used=tokens_used,
            model=model_name,
        )


def _format_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s" if m > 0 else f"{s}s"


async def _run_parallel(
    plan_id: str,
    work_dir: str | None,
    max_concurrency: int,
    run_all: bool,
    dry_run: bool,
) -> None:
    resolved_plan_id = _require_plan_context(plan_id)
    resolved_dir = str(Path(work_dir).resolve()) if work_dir else os.getcwd()

    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise SystemExit("claude binary not found. Install Claude Code first.")

    with make_client(_state.api_url, _state.token) as client:
        res = client.get(f"/api/v1/plans/{resolved_plan_id}")
        res.raise_for_status()
        plan = res.json()

    wave = 1
    while True:
        actionable = _all_actionable_tasks(plan)
        if not actionable:
            steps = plan.get("plan_steps") or []
            done = len(
                [
                    s
                    for s in steps
                    if _clean(s.get("status") or "").lower() == "completed"
                ]
            )
            total = len(steps)
            if done == total and total > 0:
                print(f"\n{GREEN}All {total} tasks completed.{RESET}")
            else:
                blocked = [
                    s
                    for s in steps
                    if _clean(s.get("status") or "").lower() == "blocked"
                ]
                if blocked:
                    print(
                        f"\n{YELLOW}No actionable tasks. {len(blocked)} task(s) blocked:{RESET}"
                    )
                    for s in blocked:
                        reason = _clean(s.get("blocked_reason")) or "no reason"
                        print(f"  - {_clean(s.get('title'))}: {reason}")
                else:
                    print("No actionable tasks remaining.")
            break

        steps = plan.get("plan_steps") or []
        done_count = len(
            [s for s in steps if _clean(s.get("status") or "").lower() == "completed"]
        )
        total_count = len(steps)

        if wave > 1:
            print(f"\n{'─' * 40}")
        print(
            f"\n{CYAN}Wave {wave}{RESET} — {len(actionable)} task(s) actionable [{done_count}/{total_count} done]"
        )

        for task in actionable:
            title = _clean(task.get("title")) or "Untitled"
            tid = _clean(task.get("id")) or "?"
            deps = task.get("depends_on") or []
            dep_str = f" (depends on: {', '.join(deps[:3])})" if deps else ""
            print(f"  {tid[:8]}  {title}{dep_str}")

        # File conflict detection — warn if parallel tasks touch same files
        _file_conflict_warnings = []
        for i, t1 in enumerate(actionable):
            files1 = set(t1.get("related_files") or [])
            if not files1:
                continue
            for t2 in actionable[i + 1 :]:
                files2 = set(t2.get("related_files") or [])
                shared = files1 & files2
                if shared:
                    _file_conflict_warnings.append(
                        f"  {YELLOW}⚠ {_clean(t1.get('title'))[:30]} + {_clean(t2.get('title'))[:30]} share: {', '.join(sorted(shared)[:3])}{RESET}"
                    )
        if _file_conflict_warnings:
            print(
                f"\n{YELLOW}File conflicts detected (agents may need to merge):{RESET}"
            )
            for w in _file_conflict_warnings[:5]:
                print(w)

        if dry_run:
            print(f"\n{DIM}Dry run — no agents launched.{RESET}")
            break

        print(
            f"\nLaunching {len(actionable)} agent(s) (max concurrency: {max_concurrency})...\n"
        )

        # Notify Collaborator about wave start
        try:
            from .collaborator import (
                is_available as _collab_check,
                notify as _collab_notify,
            )

            if _collab_check():
                task_names = ", ".join(t.get("title", "")[:30] for t in actionable[:3])
                _collab_notify(
                    f"Wave {wave}: {len(actionable)} agents starting — {task_names}"
                )
        except Exception:
            pass

        semaphore = asyncio.Semaphore(max_concurrency)
        async with make_async_client(_state.api_url, _state.token) as api_client:
            agent_tasks = [
                _launch_single_agent(
                    task,
                    plan,
                    resolved_plan_id,
                    resolved_dir,
                    len(actionable),
                    semaphore,
                    api_client,
                    session_id=session_id,
                )
                for task in actionable
            ]
            results = await asyncio.gather(*agent_tasks, return_exceptions=True)

        succeeded = 0
        failed = 0
        wave_cost = 0.0
        wave_tokens = 0
        for r in results:
            if isinstance(r, Exception):
                print(f"  {RED}[error]{RESET}  Agent crashed: {r}")
                failed += 1
            elif r.exit_code == 0:
                cost_label = f" ${r.cost_usd:.2f}" if r.cost_usd > 0 else ""
                print(
                    f"  {GREEN}[done]{RESET}   {r.task_title} ({_format_duration(r.duration_seconds)}{cost_label})"
                )
                succeeded += 1
                wave_cost += r.cost_usd
                wave_tokens += r.tokens_used
            else:
                reason = r.stderr[:100] or r.stdout[:100] or "unknown error"
                print(
                    f"  {RED}[failed]{RESET} {r.task_title} ({_format_duration(r.duration_seconds)}) — {reason}"
                )
                failed += 1
                wave_cost += r.cost_usd
                wave_tokens += r.tokens_used

        cost_summary = (
            f"  cost: ${wave_cost:.2f} ({wave_tokens:,} tokens)"
            if wave_cost > 0
            else ""
        )
        print(
            f"\nWave {wave} complete: {GREEN}{succeeded} succeeded{RESET}, {RED}{failed} failed{RESET}{cost_summary}"
        )

        if not run_all:
            if failed == 0 and succeeded > 0:
                remaining = total_count - done_count - succeeded
                if remaining > 0:
                    print(
                        f"\n{DIM}{remaining} task(s) remaining. Run with --all to auto-continue through waves.{RESET}"
                    )
            break

        # Re-fetch plan for next wave
        with make_client(_state.api_url, _state.token) as client:
            res = client.get(f"/api/v1/plans/{resolved_plan_id}")
            res.raise_for_status()
            plan = res.json()

        wave += 1


def _ensure_authenticated() -> None:
    """Check auth and provide a clear message if not logged in."""
    auth = load_auth()
    token = (auth.get("token") or "").strip()
    if not token:
        raise SystemExit(
            "Not logged in to Keshro.\n"
            "Run this in your terminal first:\n\n"
            "  keshro login <api-token>\n\n"
            "You can create or copy a token from Account -> API in the Keshro app."
        )
    try:
        with make_client() as client:
            res = client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            res.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            raise SystemExit(
                "Your Keshro session has expired.\n"
                "Run this in your terminal:\n\n"
                "  keshro login <api-token>\n\n"
                "You can create or copy a token from Account -> API in the Keshro app."
            ) from exc
        raise


def _continue_with_claude(
    plan_id: str | None,
    work_dir: str | None = None,
    auto_continue: bool = False,
    parallel: bool = True,
    confirm: bool = False,
) -> None:
    import uuid as _uuid

    _ensure_authenticated()
    session_id = f"agent-{_uuid.uuid4().hex[:8]}"
    if not work_dir:
        work_dir = (load_auth().get("default_work_dir") or "").strip() or None
    if work_dir:
        work_dir = str(Path(work_dir).resolve())
    resolved_plan_id = _current_plan_id(plan_id)
    if not resolved_plan_id:
        raise SystemExit(
            "Plan context required. Pass --plan-id <plan-id> or save one with `keshro config set --plan-id <plan-id>`."
        )
    plan = _get_plan_or_exit(resolved_plan_id)

    # Draft plan warning
    plan_status = _clean(plan.get("status") or "draft").lower()
    if plan_status == "draft" and not confirm:
        steps = sorted(plan.get("plan_steps") or [], key=lambda s: s.get("order", 0))
        migration_id = _clean(plan.get("migration_id"))
        app_url = _app_url_from_api_url(_state.api_url)
        print(
            f"\n{YELLOW}This plan is in draft. Review the tasks before executing:{RESET}\n"
        )
        for s in steps:
            order = s.get("order", 0)
            title = _clean(s.get("title")) or "Untitled"
            deps = s.get("depends_on") or []
            dep_nums = []
            for d in deps:
                dep_step = next((x for x in steps if x.get("id") == d), None)
                dep_nums.append(f"#{dep_step.get('order', '?')}" if dep_step else d)
            dep_label = (
                f" {DIM}(depends on {', '.join(dep_nums)}){RESET}" if dep_nums else ""
            )
            parallel_label = (
                f" {GREEN}parallelizable{RESET}" if s.get("parallelizable") else ""
            )
            print(f"  {order}. {title}{dep_label}{parallel_label}")
        print()
        if migration_id:
            print(f"  Review or edit: {app_url}/migrations/{migration_id}?tab=plan")
        print(f"\n  To execute: keshro continue -p {resolved_plan_id} --confirm\n")
        raise SystemExit(0)

    # Mark draft plan as active on first confirmed execution
    if plan_status == "draft" and confirm:
        try:
            with make_client(_state.api_url, _state.token) as client:
                client.patch(
                    f"/api/v1/plans/{resolved_plan_id}",
                    json={"status": "ready"},
                )
        except Exception:
            pass  # Non-fatal — plan still executes

    task = _next_actionable_task(plan, parallel=parallel)
    if not task:
        raise SystemExit("No actionable tasks remain on this plan.")
    prompt = _build_continue_prompt(
        plan,
        task,
        work_dir=work_dir,
        auto_continue=auto_continue,
        session_id=session_id,
    )
    steps = sorted(plan.get("plan_steps") or [], key=lambda s: s.get("order", 0))
    done_count = len([s for s in steps if s.get("status") == "completed"])
    total_count = len(steps)
    if _state.json:
        print_output(
            {
                "plan_id": resolved_plan_id,
                "task_id": _clean(task.get("id")) or None,
                "task_title": _clean(task.get("title")) or None,
                "text": prompt,
            },
            True,
        )
        return
    task_title = _clean(task.get("title")) or "next task"
    is_parallel_task = task.get("parallelizable", False)
    if sys.stdout.isatty():
        progress = f"[{done_count}/{total_count}]"
        parallel_note = (
            f" {GREEN}(parallelizable — will split into sub-tasks){RESET}"
            if is_parallel_task
            else ""
        )
        print(f"{progress} Resuming: {task_title}{parallel_note}")
        print(
            "Run this in your coding agent's terminal for your agent to pick up the task."
        )
    else:
        print(prompt)


def _view_task(plan_id: str | None, task_id: str) -> None:
    resolved_plan_id = _require_plan_context(plan_id)
    with make_client(_state.api_url, _state.token) as client:
        res = client.get(f"/api/v1/plans/{resolved_plan_id}")
        res.raise_for_status()
        plan = res.json()
        if _state.json:
            print_output(plan, True)
            return
        _print_task_detail(plan, task_id=task_id)


def _get_plan_or_exit(plan_id: str | None) -> dict:
    resolved_plan_id = _require_plan_context(plan_id)
    with make_client(_state.api_url, _state.token) as client:
        res = client.get(f"/api/v1/plans/{resolved_plan_id}")
        res.raise_for_status()
        return res.json()


def _find_task(plan: dict, task_id: str) -> dict | None:
    return next(
        (
            step
            for step in (plan.get("plan_steps") or [])
            if _clean(step.get("id")) == _clean(task_id)
        ),
        None,
    )


def _dependencies_met(step: dict, steps: list[dict]) -> bool:
    """Check if all dependencies for a task are completed."""
    depends_on = step.get("depends_on") or []
    if not depends_on:
        return True
    step_statuses = {
        _clean(s.get("id")): _clean(s.get("status") or "todo").lower() for s in steps
    }
    return all(step_statuses.get(dep_id) == "completed" for dep_id in depends_on)


def _next_actionable_task(plan: dict, parallel: bool = False) -> dict | None:
    steps = sorted(plan.get("plan_steps") or [], key=lambda step: step.get("order", 0))
    if parallel:
        # In parallel mode: skip in_progress tasks (another agent owns them),
        # only pick up todo tasks whose dependencies are met
        for step in steps:
            if _clean(step.get("status") or "todo").lower() != "todo":
                continue
            if not _dependencies_met(step, steps):
                continue
            # Also skip if any earlier task is blocked (fallback when no explicit deps)
            has_explicit_deps = bool(step.get("depends_on"))
            if not has_explicit_deps:
                blocked_earlier = any(
                    _clean(s.get("status")).lower() == "blocked"
                    for s in steps
                    if s.get("order", 0) < step.get("order", 0)
                )
                if blocked_earlier:
                    continue
            return step
        return None
    # Default: pick up in_progress first (resume), then first todo
    for desired_status in ("in_progress", "todo"):
        match = next(
            (
                step
                for step in steps
                if _clean(step.get("status") or "todo").lower() == desired_status
            ),
            None,
        )
        if match:
            return match
    return None


def _all_actionable_tasks(plan: dict) -> list[dict]:
    """Return all tasks whose dependencies are satisfied and status is todo or in_progress."""
    steps = sorted(plan.get("plan_steps") or [], key=lambda s: s.get("order", 0))
    completed_ids = {
        _clean(s.get("id"))
        for s in steps
        if _clean(s.get("status") or "todo").lower() == "completed"
    }
    actionable = []
    for step in steps:
        status = _clean(step.get("status") or "todo").lower()
        if status not in ("todo", "in_progress"):
            continue
        deps = step.get("depends_on") or []
        if all(_clean(dep) in completed_ids for dep in deps):
            actionable.append(step)
    return actionable


def _append_replan_summary(existing_summary: str | None, note: str) -> str:
    base = _clean(existing_summary)
    lines = [
        f"Replan notes ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}):",
        note.strip(),
    ]
    addition = "\n".join(lines)
    if not base:
        return addition
    return f"{base}\n\n{addition}"


def _delete_task(
    plan_id: str | None,
    task_id: str,
    feedback_reason: str | None = None,
    assume_yes: bool = False,
) -> None:
    resolved_plan_id = _require_plan_context(plan_id)
    with make_client(_state.api_url, _state.token) as client:
        plan_res = client.get(f"/api/v1/plans/{resolved_plan_id}")
        plan_res.raise_for_status()
        plan = plan_res.json()
        task = next(
            (
                step
                for step in (plan.get("plan_steps") or [])
                if _clean(step.get("id")) == task_id
            ),
            None,
        )
        if task and not _state.json:
            provider = _clean(task.get("external_issue_provider"))
            issue_ref = _clean(task.get("external_issue_key")) or _clean(
                task.get("linear_issue_id")
            )
            if provider and issue_ref and not assume_yes:
                typer.confirm(
                    f"Delete task {task_id} and linked {provider.title()} issue {issue_ref}?",
                    abort=True,
                )
        payload = (
            {"feedback_reason": feedback_reason}
            if feedback_reason is not None
            else None
        )
        res = client.request(
            "DELETE",
            f"/api/v1/plans/{resolved_plan_id}/tasks/{task_id}",
            json=payload,
        )
        res.raise_for_status()
        plan = res.json()
        if _state.json:
            print_output(plan, True)
            return
        print(f"Deleted task {task_id} from plan {resolved_plan_id}.")


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
# App callback – sets global state
# ---------------------------------------------------------------------------


@app.callback(invoke_without_command=True)
def _app_callback(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", help="Show version.")] = False,
    api_url: Annotated[str, typer.Option("--api-url", help="Keshro API URL.")] = "",
    token: Annotated[Optional[str], typer.Option(help="Bearer token.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="JSON output.")] = False,
):
    _state.api_url = api_url or load_auth().get("api_url", DEFAULT_API_URL)
    _state.token = token
    _state.json = json_output
    if version:
        print(__version__)
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        print(__version__)
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Auth commands
# ---------------------------------------------------------------------------


def _do_login(
    token: str | None = None,
):
    cmd_auth_login(
        api_url=_state.api_url,
        token=token,
        json_output=_state.json,
    )


def _do_logout():
    cmd_auth_logout(json_output=_state.json)


@auth_app.command("login")
def _auth_login(
    token_value: Annotated[
        Optional[str], typer.Argument(help="Personal access token.")
    ] = None,
):
    """Authenticate with Keshro"""
    _do_login(token=token_value)


@auth_app.command("logout")
def _auth_logout():
    """Clear locally stored credentials."""
    _do_logout()


@app.command("login")
def _login_alias(
    token_value: Annotated[
        Optional[str], typer.Argument(help="Personal access token.")
    ] = None,
):
    """Authenticate with Keshro"""
    _do_login(token=token_value)


@app.command("logout")
def _logout_alias():
    """Clear local credentials"""
    _do_logout()


# ---------------------------------------------------------------------------
# Migration commands
# ---------------------------------------------------------------------------


@app.command("create")
def _create_migration(
    path: Annotated[
        str,
        typer.Option(
            "--path",
            help="Migration path key, for example aws-batch-to-airflow.",
        ),
    ],
    field: Annotated[
        Optional[list[str]],
        typer.Option(
            "--field",
            "-f",
            help="Set a template field as field_id=value. Repeat for multiple fields.",
        ),
    ] = None,
    context: Annotated[
        Optional[str], typer.Option("--context", "-c", help="Additional context.")
    ] = None,
    github_url: Annotated[
        Optional[str], typer.Option("--github-url", help="GitHub URL to attach.")
    ] = None,
    resource_url: Annotated[
        Optional[str], typer.Option("--resource-url", help="Reference URL to attach.")
    ] = None,
    org_id: Annotated[
        Optional[str], typer.Option("--org-id", "-o", help="Create under an org.")
    ] = None,
    work_dir: Annotated[
        Optional[str],
        typer.Option(
            "--dir",
            "-d",
            help="Path to the project codebase. Defaults to current directory.",
        ),
    ] = None,
    repo_url: Annotated[
        Optional[str],
        typer.Option(
            "--repo",
            "-r",
            help="Git repo URL to clone and scan. Cloned to a temp directory.",
        ),
    ] = None,
):
    """Create a migration project from a stable path key. Requires a coding agent that can run shell commands."""
    if sys.stdout.isatty():
        raise SystemExit(
            "This command needs to run inside a coding agent so it can scan your codebase.\n"
            "Run it from your agent's terminal, or use the prompt copy/paste path in Keshro instead."
        )
    import tempfile

    answers = _parse_field_assignments(field)
    clone_dir = None
    if repo_url and not work_dir:
        clone_dir = tempfile.mkdtemp(prefix="keshro-clone-")
        if not _state.json:
            print(f"Cloning {repo_url}...")
        clone_result = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, clone_dir],
            capture_output=True,
            text=True,
            check=False,
        )
        if clone_result.returncode != 0:
            raise SystemExit(
                f"Failed to clone {repo_url}: {_clean(clone_result.stderr)}"
            )
        work_dir = clone_dir

    try:
        return _create_migration_inner(
            path,
            answers,
            context,
            github_url,
            resource_url,
            org_id,
            work_dir,
        )
    finally:
        if clone_dir:
            import shutil as _shutil

            _shutil.rmtree(clone_dir, ignore_errors=True)


def _create_migration_inner(
    path: str,
    answers: dict,
    context: str | None,
    github_url: str | None,
    resource_url: str | None,
    org_id: str | None,
    work_dir: str | None,
) -> None:
    with make_client(_state.api_url, _state.token) as client:
        template_res = client.get(
            "/api/v1/migrations/path-template/lookup", params={"template_key": path}
        )
        template_res.raise_for_status()
        template = template_res.json()
        source = _clean(template.get("source"))
        target = _clean(template.get("target"))

        if not _state.json:
            print(f"Collecting migration context for {source} -> {target}...")

        resolved_work_dir = str(Path(work_dir).resolve()) if work_dir else None
        discovered_answer = _collect_discovery_answer_from_claude(
            template, work_dir=resolved_work_dir
        )
        extracted = _extract_discovery_answers(template, discovered_answer)
        # Don't overwrite manually provided -f values with empty extracted values
        for key, value in extracted.items():
            if key not in answers or not answers[key]:
                answers[key] = value

        required_fields = [
            _clean(item.get("label")) or _clean(item.get("id"))
            for item in (template.get("fields") or [])
            if item.get("required") and not answers.get(_clean(item.get("id")))
        ]
        if required_fields and not _state.json:
            print(
                f"Some fields couldn't be discovered automatically: {', '.join(required_fields)}"
            )

        merged_context = f"CLI bootstrap for {source} -> {target}."
        if _clean(context):
            merged_context = (
                f"{merged_context}\n\n{_clean(context)}"
                if merged_context
                else _clean(context)
            )

        custom_fields = dict(answers)
        if discovered_answer:
            custom_fields["__keshro_discovered_context"] = discovered_answer

        payload = {
            "source_type": source,
            "target_type": target,
            "input_method": "cli_agent",
            "context": merged_context or f"CLI bootstrap for {source} -> {target}.",
            "files": [],
            "github_url": _clean(github_url) or None,
            "resource_url": _clean(resource_url) or None,
            "org_id": _clean(org_id) or None,
            "input_fields": [
                {
                    "field_id": _clean(item.get("id")),
                    "label": _clean(item.get("label")) or _clean(item.get("id")),
                }
                for item in (template.get("fields") or [])
                if _clean(item.get("id")) in answers
            ]
            + (
                [
                    {
                        "field_id": "__keshro_discovered_context",
                        "label": "Discovered migration context",
                    }
                ]
                if discovered_answer
                else []
            ),
            "custom_fields": custom_fields or None,
        }
        if not _state.json:
            print("Checking for high-impact follow-up questions...")
        clarifier_questions = _get_migration_clarifiers(client, payload)
        if clarifier_questions:
            if not _state.json:
                print("Collecting follow-up answers...")
            clarifier_answers = _collect_clarifier_answers_from_claude(
                template, payload, clarifier_questions, work_dir=resolved_work_dir
            )
            payload = _merge_clarifier_answers(
                payload, clarifier_questions, clarifier_answers
            )
        elif not _state.json:
            print("No additional follow-up questions needed.")
    _render_prefill_handoff(payload, template, work_dir=resolved_work_dir)


@migration_app.command("list")
def _migration_list(
    org_id: Annotated[
        Optional[str], typer.Option("--org-id", "-o", help="Filter by org.")
    ] = None,
    status: Annotated[
        Optional[str],
        typer.Option("--status", "-s", help="Filter by migration status."),
    ] = None,
    latest_count: Annotated[
        Optional[int],
        typer.Option("--latest", "-n", min=1, help="Show only the N latest results."),
    ] = None,
    verbose: Annotated[
        bool, typer.Option("-v", "--verbose", help="Verbose output.")
    ] = False,
):
    """List migration projects, optionally filtered by workspace or status."""
    params: dict = {}
    resolved_org = _current_org_id(org_id)
    context_label = _current_context_label() if resolved_org else None
    if resolved_org:
        params["org_id"] = resolved_org
    if status:
        params["status"] = status
    with make_client(_state.api_url, _state.token) as client:
        res = client.get("/api/v1/migrations", params=params)
        res.raise_for_status()
        migrations = sorted(
            res.json(), key=lambda m: _clean(m.get("created_at")), reverse=True
        )
        if latest_count is not None:
            migrations = migrations[:latest_count]
        if _state.json:
            print_output(migrations, True)
            return
        if not migrations:
            if context_label:
                print(f"No migrations found for org {context_label}.")
            else:
                print("No migrations found.")
            return
        if verbose:
            for migration in migrations:
                _print_migration_summary(
                    migration, verbose=True, context_label=context_label
                )
            return
        rows = [
            [
                _clean(migration.get("id")),
                f"{migration.get('source_type') or 'Unknown source'} -> {migration.get('target_type') or 'Unknown target'}",
                _clean(migration.get("status") or "pending") or "pending",
                _clean(migration.get("created_at")),
            ]
            for migration in migrations
        ]
        _print_table(["ID", "PATH", "STATUS", "CREATED"], rows)


@migration_app.command("view")
def _migration_view(
    migration_id: Annotated[str, typer.Argument(help="Migration ID.")],
):
    """Show full details for a migration project."""
    with make_client(_state.api_url, _state.token) as client:
        res = client.get(f"/api/v1/migrations/{migration_id}")
        res.raise_for_status()
        migration = res.json()
        if _state.json:
            print_output(migration, True)
            return
        context_label = _current_context_label() if _current_org_id() else None
        _print_migration_detail(migration, context_label=context_label)


@migration_app.command("history")
def _migration_history(
    migration_id: Annotated[str, typer.Argument(help="Migration ID.")],
):
    """Show the execution history / audit trail for a migration."""
    with make_client(_state.api_url, _state.token) as client:
        migration_res = client.get(f"/api/v1/migrations/{migration_id}")
        migration_res.raise_for_status()
        migration = migration_res.json()
        plan_res = client.get(f"/api/v1/migrations/{migration_id}/plan")
        plan_res.raise_for_status()
        plan = plan_res.json()
        if _state.json:
            print_output(
                {
                    "migration_id": migration.get("id"),
                    "plan_id": plan.get("id"),
                    "task_feedback_events": plan.get("task_feedback_events") or [],
                },
                True,
            )
            return
        context_label = _current_context_label() if _current_org_id() else None
        _print_migration_summary(migration, context_label=context_label)
        _print_task_feedback_events(plan)


@migration_app.command("delete")
def _migration_delete(
    migration_id: Annotated[str, typer.Argument(help="Migration ID.")],
):
    """Delete a migration project."""
    with make_client(_state.api_url, _state.token) as client:
        res = client.delete(f"/api/v1/migrations/{migration_id}")
        res.raise_for_status()
        if _state.json:
            print_output(res.json(), True)
            return
        print(f"Deleted migration {migration_id}.")


# ---------------------------------------------------------------------------
# Config commands
# ---------------------------------------------------------------------------


def _config_show():
    auth = load_auth()
    orgs: list[dict] = []
    authenticated = False
    if auth.get("token"):
        try:
            with make_client(
                auth.get("api_url") or DEFAULT_API_URL, auth.get("token")
            ) as client:
                me_res = client.get("/api/v1/auth/me")
                me_res.raise_for_status()
                authenticated = True
                res = client.get("/api/v1/orgs")
                res.raise_for_status()
                orgs = res.json() or []
        except Exception:
            orgs = []
    payload = {
        "api_url": auth.get("api_url") or DEFAULT_API_URL,
        "authenticated": authenticated,
        "default_org_id": auth.get("default_org_id"),
        "default_org_name": auth.get("default_org_name"),
        "default_plan_id": auth.get("default_plan_id"),
        "default_plan_title": auth.get("default_plan_title"),
        "user": auth.get("user") or {},
        "orgs": orgs,
    }
    if _state.json:
        print_output(payload, True)
        return
    user = payload["user"] or {}
    print(f"{DIM}API URL:{RESET} {CYAN}{payload['api_url']}{RESET}")
    print(
        f"{DIM}Authenticated:{RESET} "
        f"{GREEN if payload['authenticated'] else CYAN}{'yes' if payload['authenticated'] else 'no'}{RESET}"
    )
    default_context = (
        payload["default_org_name"] or payload["default_org_id"] or "personal"
    )
    print(f"{DIM}Default context:{RESET} " f"{YELLOW}{default_context}{RESET}")
    default_plan = payload["default_plan_title"] or payload["default_plan_id"]
    if default_plan:
        print(f"{DIM}Default plan:{RESET} " f"{YELLOW}{default_plan}{RESET}")
    if user.get("email"):
        print(f"{DIM}User:{RESET} {CYAN}{user['email']}{RESET}")
    if user.get("name"):
        print(f"{DIM}Name:{RESET} {user['name']}")
    if payload["orgs"]:
        org_names = ", ".join(
            org.get("name") or org.get("id") or "Unknown org" for org in payload["orgs"]
        )
        print(f"{DIM}Organizations:{RESET} {org_names}")


@config_app.callback(invoke_without_command=True)
def _config_callback(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        _config_show()


@config_app.command("set")
def _config_set(
    org_id: Annotated[
        Optional[str], typer.Option("--org-id", "-i", help="Org ID.")
    ] = None,
    org: Annotated[Optional[str], typer.Option("--org", "-o", help="Org name.")] = None,
    plan_id: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
    api_url: Annotated[
        Optional[str], typer.Option("--api-url", "-u", help="Keshro API URL.")
    ] = None,
    personal: Annotated[
        bool, typer.Option("--personal", help="Use personal context.")
    ] = False,
    work_dir: Annotated[
        Optional[str], typer.Option("--dir", "-d", help="Default project directory.")
    ] = None,
    clear_plan: Annotated[
        bool, typer.Option("--clear-plan", help="Clear saved plan context.")
    ] = False,
):
    """Set default workspace context."""
    updates: dict = {}
    if work_dir is not None:
        updates["default_work_dir"] = (
            str(Path(work_dir).resolve()) if work_dir else None
        )
    if api_url is not None:
        updates["api_url"] = _clean(api_url) or DEFAULT_API_URL
    if personal:
        updates["default_org_id"] = None
        updates["default_org_name"] = None
    elif org_id is not None or org is not None:
        resolved_id, resolved_name = _resolve_org_context(org_id, org)
        updates["default_org_id"] = resolved_id
        updates["default_org_name"] = resolved_name
    if clear_plan:
        updates["default_plan_id"] = None
        updates["default_plan_title"] = None
    elif plan_id is not None:
        resolved_plan_id, resolved_plan_title = _resolve_plan_context(plan_id)
        updates["default_plan_id"] = resolved_plan_id
        updates["default_plan_title"] = resolved_plan_title
    auth = update_auth(updates)
    payload = {
        "api_url": auth.get("api_url") or DEFAULT_API_URL,
        "default_org_id": auth.get("default_org_id"),
        "default_org_name": auth.get("default_org_name"),
        "default_plan_id": auth.get("default_plan_id"),
        "default_plan_title": auth.get("default_plan_title"),
    }
    if _state.json:
        print_output(payload, True)
        return
    org_label = auth.get("default_org_name") or auth.get("default_org_id") or "personal"
    print(f"Saved default context: {org_label}")
    if api_url is not None:
        print(f"Saved API URL: {auth.get('api_url') or DEFAULT_API_URL}")
    plan_label = auth.get("default_plan_title") or auth.get("default_plan_id")
    if plan_label:
        print(f"Saved default plan: {plan_label}")
    elif clear_plan:
        print("Cleared default plan context.")


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def _cmd_plan_templates(
    template_name: str | None = None,
    name: str | None = None,
    verbose: bool = False,
):
    with make_client(_state.api_url, _state.token) as client:
        res = client.get("/api/v1/plans/templates")
        res.raise_for_status()
        templates = res.json()
        effective_name = template_name or name
        if effective_name == "list":
            effective_name = None
        if effective_name:
            match = next(
                (item for item in templates if item.get("key") == effective_name), None
            )
            if not match:
                raise SystemExit(f"Template not found: {effective_name}")
            if _state.json:
                print_output(match, True)
                return
            print(f"{CYAN}{match['key']}{RESET}")
            if match.get("title"):
                print(f"{DIM}Title:{RESET} {match['title']}")
            if match.get("summary"):
                print(f"{DIM}Summary:{RESET} {match['summary']}")
            if match.get("why_use_it"):
                print(f"{DIM}Why use it:{RESET} {match['why_use_it']}")
            steps = match.get("plan_steps") or []
            if steps:
                print(f"{DIM}Plan steps:{RESET}")
                for step in steps:
                    print(f"  - {step.get('title', 'Untitled step')}")
            return
        if _state.json:
            print_output(templates, True)
            return
        if verbose:
            for template in templates:
                print(f"{CYAN}{template['key']}{RESET}")
                if template.get("title"):
                    print(f"  {template['title']}")
                if template.get("summary"):
                    print(f"  {template['summary']}")
            return
        for template in templates:
            print(template["key"])


@app.command("templates")
def _templates_alias(
    template_name: Annotated[
        Optional[str], typer.Argument(help="Template key.")
    ] = None,
    name: Annotated[
        Optional[str], typer.Option("-n", "--name", help="Template key.")
    ] = None,
    verbose: Annotated[
        bool, typer.Option("-v", "--verbose", help="Verbose output.")
    ] = False,
):
    """List available plan templates, or show details for one"""
    _cmd_plan_templates(template_name, name, verbose)


@plan_app.command("templates")
def _plan_templates(
    template_name: Annotated[
        Optional[str], typer.Argument(help="Template key.")
    ] = None,
    name: Annotated[
        Optional[str], typer.Option("-n", "--name", help="Template key.")
    ] = None,
    verbose: Annotated[
        bool, typer.Option("-v", "--verbose", help="Verbose output.")
    ] = False,
):
    """List available plan templates, or show details for one"""
    _cmd_plan_templates(template_name, name, verbose)


@app.command("continue")
def _continue_command(
    plan_id: Annotated[
        Optional[str],
        typer.Option(
            "--plan-id", "-p", help="Plan ID. Uses saved plan context if omitted."
        ),
    ] = None,
    work_dir: Annotated[
        Optional[str],
        typer.Option(
            "--dir",
            "-d",
            help="Path to the codebase. Use when the project lives in a different directory.",
        ),
    ] = None,
    auto_continue: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Auto-continue through tasks without asking after each one.",
        ),
    ] = False,
    confirm: Annotated[
        bool,
        typer.Option(
            "--confirm",
            help="Confirm execution of a draft plan.",
        ),
    ] = False,
    no_parallel: Annotated[
        bool,
        typer.Option(
            "--no-parallel",
            help="Disable parallel execution and resume a single task (used inside an agent).",
        ),
    ] = False,
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency",
            "-c",
            help="Max parallel agents (default 5, max 30).",
        ),
    ] = 5,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show what would be launched without actually running agents.",
        ),
    ] = False,
):
    """Resume execution of a plan. Launches parallel agents by default."""
    # Inside a coding agent (piped stdout), always single-task mode.
    # In user's terminal, default to parallel unless --no-parallel is passed.
    use_parallel = not no_parallel and (sys.stdout.isatty() or dry_run)
    if not use_parallel:
        _continue_with_claude(
            plan_id,
            work_dir=work_dir,
            auto_continue=auto_continue,
            parallel=not no_parallel,
            confirm=confirm,
        )
    else:
        _ensure_authenticated()
        concurrency = max(1, min(concurrency, 30))
        asyncio.run(
            _run_parallel(
                plan_id,
                work_dir=work_dir,
                max_concurrency=concurrency,
                run_all=auto_continue,
                dry_run=dry_run,
            )
        )


CLAUDE_COMMANDS_DIR = Path.home() / ".claude" / "commands"

KESHRO_SLASH_COMMAND = """\
When the user asks you to run keshro commands, run them as bash commands in the terminal.

The `keshro` CLI manages migration execution plans. Common commands:

- `keshro continue -p <plan-id>` — resume the next task from a migration plan (start here)
- `keshro task start <task-id> -p <plan-id>` — mark a task as in progress
- `keshro task done <task-id> -p <plan-id>` — mark a task as complete
- `keshro task note <task-id> -p <plan-id> "note"` — add a note to a task
- `keshro task block <task-id> -p <plan-id> "reason"` — flag a blocker
- `keshro plan view <plan-id>` — view the full execution plan

Run `keshro` commands via Bash, not as chat messages. Do not use Keshro MCP tools.
"""


@app.command("setup-claude")
def _setup_claude():
    """Install a global Claude Code slash command for Keshro"""
    CLAUDE_COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    target = CLAUDE_COMMANDS_DIR / "keshro.md"
    target.write_text(KESHRO_SLASH_COMMAND)
    if _state.json:
        print_output({"status": "ok", "path": str(target)}, True)
    else:
        print(f"Installed Claude Code slash command at {target}")
        print("You can now use /keshro in any Claude Code session.")


# ---------------------------------------------------------------------------
# Plan commands
# ---------------------------------------------------------------------------


@plan_app.command("create")
def _plan_create(
    migration_id: Annotated[Optional[str], typer.Argument(help="Migration ID.")] = None,
    title: Annotated[
        Optional[str], typer.Option("--title", "-t", help="Plan title.")
    ] = None,
    summary: Annotated[
        Optional[str], typer.Option("--summary", "-u", help="Plan summary.")
    ] = None,
    status: Annotated[
        str, typer.Option("--status", "-s", help="Plan status.")
    ] = "draft",
    steps_file: Annotated[
        Optional[str],
        typer.Option("--steps-file", "-f", help="JSON file with plan steps."),
    ] = None,
    link: Annotated[
        Optional[list[str]], typer.Option("--link", "-l", help="External link.")
    ] = None,
    from_template: Annotated[
        Optional[str],
        typer.Option("--from-template", "-T", help="Create from template."),
    ] = None,
    from_file: Annotated[
        Optional[str], typer.Option("--from-file", "-F", help="Import from file.")
    ] = None,
    from_claude: Annotated[
        Optional[str],
        typer.Option("--from-claude", "-c", help="Import from Claude output."),
    ] = None,
):
    """Create the execution plan attached to a migration."""
    resolved_migration_id = migration_id
    if from_file and from_claude:
        raise typer.BadParameter("Cannot use both --from-file and --from-claude.")
    if not resolved_migration_id:
        raise typer.BadParameter(
            "Migration ID is required. Run `keshro plan create <migration-id>`."
        )

    if from_template:
        payload = {
            "template_key": from_template,
            "title": title,
            "summary": summary,
            "migration_id": resolved_migration_id,
            "import_source": "template",
        }
        with make_client(_state.api_url, _state.token) as client:
            res = client.post("/api/v1/plans/from-template", json=payload)
            res.raise_for_status()
            created = res.json()
            print_output(created, _state.json)
            _set_default_plan_after_create(created)
        return

    from_path = from_file or from_claude
    if from_path:
        import_mode = "claude" if from_claude else "file"
        payload = _plan_payload_from_file(
            from_path,
            import_mode,
            title=title,
            summary=summary,
            status=status,
            migration_id=resolved_migration_id,
            link=link,
        )
    else:
        payload = {
            "title": title,
            "summary": summary,
            "status": status,
            "import_source": "analysis",
            "migration_id": resolved_migration_id,
            "plan_steps": _read_json_file(steps_file),
            "external_links": link or [],
        }
    _validate_plan_payload(payload)
    with make_client(_state.api_url, _state.token) as client:
        res = client.post("/api/v1/plans", json=payload)
        res.raise_for_status()
        created = res.json()
        print_output(created, _state.json)
        _set_default_plan_after_create(created)


@plan_app.command("list")
def _plan_list(
    org_id: Annotated[
        Optional[str], typer.Option("--org-id", "-o", help="Filter by org.")
    ] = None,
    latest_count: Annotated[
        Optional[int],
        typer.Option("--latest", "-n", min=1, help="Show only the N latest results."),
    ] = None,
    verbose: Annotated[
        bool, typer.Option("-v", "--verbose", help="Verbose output.")
    ] = False,
):
    """List plans, optionally filtered by workspace."""
    params: dict = {}
    resolved_org = _current_org_id(org_id)
    context_label = _current_context_label() if resolved_org else None
    if resolved_org:
        params["org_id"] = resolved_org
    with make_client(_state.api_url, _state.token) as client:
        res = client.get("/api/v1/plans", params=params)
        res.raise_for_status()
        plans = res.json()
        plans = sorted(plans, key=lambda p: _clean(p.get("updated_at")), reverse=True)
        if latest_count is not None:
            plans = plans[:latest_count]
        if _state.json:
            print_output(plans, True)
            return
        if not plans:
            if context_label:
                print(f"No plans found for org {context_label}.")
            else:
                print("No plans found.")
            return
        if verbose:
            for plan in plans:
                _print_plan_summary(plan, verbose=True, context_label=context_label)
            return
        rows = [
            [
                _clean(plan.get("id")),
                _clean(plan.get("title")) or "Untitled plan",
                _clean(plan.get("status") or "draft") or "draft",
                f"{plan.get('source_type') or 'Unknown source'} -> {plan.get('target_type') or 'Unknown target'}",
                _clean(plan.get("updated_at")),
            ]
            for plan in plans
        ]
        _print_table(["ID", "TITLE", "STATUS", "PATH", "UPDATED"], rows)


@plan_app.command("view")
def _plan_view(
    plan_id: Annotated[str, typer.Argument(help="Plan ID.")],
):
    """Show full details for a plan including steps."""
    with make_client(_state.api_url, _state.token) as client:
        res = client.get(f"/api/v1/plans/{plan_id}")
        res.raise_for_status()
        plan = res.json()
        if _state.json:
            print_output(plan, True)
            return
        context_label = _current_context_label() if _current_org_id() else None
        _print_plan_detail(plan, context_label=context_label)


def _print_plan_status(plan: dict) -> None:
    """Print a compact live-status dashboard for a plan."""
    from datetime import datetime, timezone

    title = _clean(plan.get("title")) or "Untitled plan"
    source = _clean(plan.get("source_type")) or ""
    target = _clean(plan.get("target_type")) or ""
    path_label = f"{source} → {target}" if source and target else ""
    steps = sorted(plan.get("plan_steps") or [], key=lambda s: s.get("order", 0))
    events = plan.get("task_feedback_events") or []

    done = [s for s in steps if _clean(s.get("status")).lower() == "completed"]
    in_progress = [s for s in steps if _clean(s.get("status")).lower() == "in_progress"]
    blocked = [s for s in steps if _clean(s.get("status")).lower() == "blocked"]
    todo = [s for s in steps if _clean(s.get("status")).lower() == "todo"]

    # Header
    print(
        f"\n{CYAN}{title}{RESET} {DIM}{path_label}{RESET} [{len(done)}/{len(steps)} done]"
    )
    print()

    # Status symbols
    STATUS_ICON = {
        "completed": f"{GREEN}✓{RESET}",
        "in_progress": f"{YELLOW}●{RESET}",
        "blocked": f"{RED}✗{RESET}",
        "todo": f"{DIM}○{RESET}",
    }

    # Find latest event per task for agent/timing info
    task_latest_event: dict[str, dict] = {}
    for event in events:
        tid = event.get("task_id") or ""
        existing = task_latest_event.get(tid)
        if not existing or (event.get("created_at") or "") > (
            existing.get("created_at") or ""
        ):
            task_latest_event[tid] = event

    now = datetime.now(timezone.utc)

    for step in steps:
        status = _clean(step.get("status") or "todo").lower()
        icon = STATUS_ICON.get(status, "?")
        order = step.get("order", 0)
        step_title = _clean(step.get("title")) or "Untitled"
        step_id = step.get("id") or ""

        # Build right-side info
        info_parts: list[str] = []
        latest = task_latest_event.get(step_id)
        if latest:
            source_label = latest.get("source") or ""
            if source_label:
                info_parts.append(source_label)
            created = latest.get("created_at") or ""
            if created:
                try:
                    event_time = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    delta = now - event_time
                    if delta.days > 0:
                        info_parts.append(f"{delta.days}d ago")
                    elif delta.seconds > 3600:
                        info_parts.append(f"{delta.seconds // 3600}h ago")
                    elif delta.seconds > 60:
                        info_parts.append(f"{delta.seconds // 60}m ago")
                    else:
                        info_parts.append("just now")
                except Exception:
                    pass

        if status == "in_progress" and latest:
            created = latest.get("created_at") or ""
            if created:
                try:
                    event_time = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    elapsed = now - event_time
                    if elapsed.seconds > 60:
                        info_parts.append(f"({elapsed.seconds // 60}m)")
                except Exception:
                    pass

        if status == "blocked":
            reason = _clean(step.get("blocked_reason"))
            if reason:
                info_parts.append(reason[:50])

        info = f" {DIM}· {' · '.join(info_parts)}{RESET}" if info_parts else ""
        print(f"  {icon} {order}. {step_title}{info}")

    # Footer
    print()
    summary_parts = []
    if in_progress:
        summary_parts.append(f"{len(in_progress)} active")
    if blocked:
        summary_parts.append(f"{RED}{len(blocked)} blocked{RESET}")
    if todo:
        summary_parts.append(f"{len(todo)} remaining")

    # Detect unique active agents from recent in_progress events
    active_sources = set()
    for step in in_progress:
        latest = task_latest_event.get(step.get("id") or "")
        if latest and latest.get("source"):
            active_sources.add(latest["source"])

    if active_sources:
        summary_parts.append(
            f"{len(active_sources)} agent{'s' if len(active_sources) != 1 else ''}"
        )

    updated = _clean(plan.get("updated_at"))
    if updated:
        try:
            update_time = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            delta = now - update_time
            if delta.seconds < 60:
                summary_parts.append("updated just now")
            elif delta.seconds < 3600:
                summary_parts.append(f"updated {delta.seconds // 60}m ago")
            elif delta.days == 0:
                summary_parts.append(f"updated {delta.seconds // 3600}h ago")
            else:
                summary_parts.append(f"updated {delta.days}d ago")
        except Exception:
            pass

    if summary_parts:
        print(f"  {DIM}{' · '.join(summary_parts)}{RESET}")
    print()


def _run_status(plan_id: str | None, watch: bool = False, tui: bool = False) -> None:
    import time as _time

    resolved_plan_id = _current_plan_id(plan_id)
    if not resolved_plan_id:
        raise SystemExit(
            "Plan context required. Pass --plan-id <plan-id> or save one with `keshro config set --plan-id <plan-id>`."
        )

    if tui:
        try:
            from .tui import run_tui
        except ImportError:
            print(
                f"{RED}TUI requires textual. Install with: pip install textual{RESET}",
                file=sys.stderr,
            )
            raise typer.Exit(1)

        api_url = _state.api_url
        token = _state.token
        run_tui(api_url=api_url, token=token, plan_id=resolved_plan_id)
        return

    plan = _get_plan_or_exit(resolved_plan_id)
    if _state.json:
        print_output(plan, True)
        return

    if not watch:
        _print_plan_status(plan)
        # Also show dependency graph
        from .graph import render_plan_summary

        print()
        print(render_plan_summary(plan))
        return

    try:
        while True:
            # Clear screen and redraw
            print("\033[2J\033[H", end="")
            plan = _get_plan_or_exit(resolved_plan_id)
            _print_plan_status(plan)
            print(f"  {DIM}Watching · refreshes every 10s · Ctrl+C to stop{RESET}")
            _time.sleep(10)
    except KeyboardInterrupt:
        print("\nStopped watching.")


@plan_app.command("status")
def _plan_status(
    plan_id: Annotated[
        Optional[str],
        typer.Option(
            "--plan-id", "-p", help="Plan ID. Uses saved plan context if omitted."
        ),
    ] = None,
    watch: Annotated[
        bool,
        typer.Option("--watch", "-w", help="Poll every 10 seconds and redraw."),
    ] = False,
    tui: Annotated[
        bool,
        typer.Option("--tui", help="Launch interactive Textual TUI dashboard."),
    ] = False,
):
    """Live status dashboard for a plan. Shows all tasks, active agents, and blockers."""
    _run_status(plan_id, watch=watch, tui=tui)


@app.command("status")
def _status_alias(
    plan_id: Annotated[
        Optional[str],
        typer.Option(
            "--plan-id", "-p", help="Plan ID. Uses saved plan context if omitted."
        ),
    ] = None,
    watch: Annotated[
        bool,
        typer.Option("--watch", "-w", help="Poll every 10 seconds and redraw."),
    ] = False,
    tui: Annotated[
        bool,
        typer.Option("--tui", help="Launch interactive Textual TUI dashboard."),
    ] = False,
):
    """Live status dashboard for the current plan."""
    _run_status(plan_id, watch=watch, tui=tui)


@plan_app.command("next")
def _plan_next(
    plan_id: Annotated[
        Optional[str],
        typer.Argument(help="Plan ID. Uses saved plan context if omitted."),
    ] = None,
):
    """Show the next actionable task in a plan."""
    resolved_plan_id = _require_plan_context(plan_id)
    plan = _get_plan_or_exit(resolved_plan_id)
    task = _next_actionable_task(plan)
    if _state.json:
        print_output(
            {
                "plan_id": resolved_plan_id,
                "task": task,
            },
            True,
        )
        return
    if not task:
        print(f"No actionable tasks found for plan {resolved_plan_id}.")
        return
    _print_task_detail(plan, task_id=_clean(task.get("id")))


@task_app.command("next")
def _task_next(
    plan_id: Annotated[
        Optional[str],
        typer.Option(
            "--plan-id", "-p", help="Plan ID. Uses saved plan context if omitted."
        ),
    ] = None,
):
    """Show the next actionable task in the current migration plan."""
    resolved_plan_id = _require_plan_context(plan_id)
    plan = _get_plan_or_exit(resolved_plan_id)
    task = _next_actionable_task(plan)
    if _state.json:
        print_output(
            {
                "plan_id": resolved_plan_id,
                "task": task,
            },
            True,
        )
        return
    if not task:
        print(f"No actionable tasks found for plan {resolved_plan_id}.")
        return
    _print_task_detail(plan, task_id=_clean(task.get("id")))


@plan_app.command("update")
def _plan_update(
    plan_id: Annotated[str, typer.Argument(help="Plan ID.")],
    title: Annotated[
        Optional[str], typer.Option("--title", "-t", help="Plan title.")
    ] = None,
    summary: Annotated[
        Optional[str], typer.Option("--summary", "-u", help="Plan summary.")
    ] = None,
    status: Annotated[
        Optional[str], typer.Option("--status", "-s", help="Plan status.")
    ] = None,
    steps_file: Annotated[
        Optional[str],
        typer.Option("--steps-file", "-f", help="JSON file with plan steps."),
    ] = None,
    link: Annotated[
        Optional[list[str]], typer.Option("--link", "-l", help="External link.")
    ] = None,
):
    """Update an existing plan's metadata or steps."""
    payload: dict = {}
    for key, value in [
        ("title", title),
        ("summary", summary),
        ("status", status),
    ]:
        if value is not None:
            payload[key] = value
    if steps_file:
        payload["plan_steps"] = _read_json_file(steps_file)
    if link is not None:
        payload["external_links"] = link
    with make_client(_state.api_url, _state.token) as client:
        res = client.patch(f"/api/v1/plans/{plan_id}", json=payload)
        res.raise_for_status()
        print_output(res.json(), _state.json)


@plan_app.command("replan-notes")
def _plan_replan_notes(
    note: Annotated[
        str, typer.Argument(help="Replan note to append to the plan summary.")
    ],
    plan_id: Annotated[
        Optional[str],
        typer.Option(
            "--plan-id", "-p", help="Plan ID. Uses saved plan context if omitted."
        ),
    ] = None,
):
    """Append a replan note to the current plan summary."""
    resolved_plan_id = _require_plan_context(plan_id)
    plan = _get_plan_or_exit(resolved_plan_id)
    payload = {
        "summary": _append_replan_summary(plan.get("summary"), note),
    }
    with make_client(_state.api_url, _state.token) as client:
        res = client.patch(f"/api/v1/plans/{resolved_plan_id}", json=payload)
        res.raise_for_status()
        updated = res.json()
        if _state.json:
            print_output(updated, True)
            return
        print(f"Saved replan notes on plan {resolved_plan_id}.")


@plan_app.command("delete")
def _plan_delete(
    plan_id: Annotated[str, typer.Argument(help="Plan ID.")],
):
    """Delete a plan."""
    with make_client(_state.api_url, _state.token) as client:
        res = client.delete(f"/api/v1/plans/{plan_id}")
        res.raise_for_status()
        saved_plan_id = _current_plan_id()
        if saved_plan_id == plan_id:
            update_auth({"default_plan_id": None, "default_plan_title": None})
        if _state.json:
            print_output(res.json(), True)
            return
        print(f"Deleted plan {plan_id}.")


# ---------------------------------------------------------------------------
# Task commands
# ---------------------------------------------------------------------------


def _do_task_add(
    plan_id: str | None,
    title: str,
    description: str,
    status: str = "todo",
    owner: str | None = None,
    notes: str | None = None,
    linear_issue_id: str | None = None,
    blocked_reason: str | None = None,
    link: list[str] | None = None,
):
    plan_id = _require_plan_context(plan_id)
    payload = {
        "title": title,
        "description": description,
        "status": status,
        "owner": owner,
        "notes": notes,
        "linear_issue_id": linear_issue_id,
        "blocked_reason": blocked_reason,
        "artifact_links": link or [],
    }
    with make_client(_state.api_url, _state.token) as client:
        res = client.post(f"/api/v1/plans/{plan_id}/tasks", json=payload)
        res.raise_for_status()
        plan = res.json()
        if _state.json:
            print_output(plan, True)
            return
        _print_task_detail(plan, title_hint=title)


def _do_task_update(
    plan_id: str | None,
    task_id: str,
    title: str | None = None,
    description: str | None = None,
    status: str | None = None,
    owner: str | None = None,
    notes: str | None = None,
    linear_issue_id: str | None = None,
    blocked_reason: str | None = None,
    feedback_reason: str | None = None,
    link: list[str] | None = None,
):
    plan_id = _require_plan_context(plan_id)
    payload: dict = {}
    for key, value in [
        ("title", title),
        ("description", description),
        ("status", status),
        ("owner", owner),
        ("notes", notes),
        ("linear_issue_id", linear_issue_id),
        ("blocked_reason", blocked_reason),
        ("feedback_reason", feedback_reason),
    ]:
        if value is not None:
            payload[key] = value
    if link is not None:
        payload["artifact_links"] = link
    with make_client(_state.api_url, _state.token) as client:
        res = client.patch(
            f"/api/v1/plans/{plan_id}/tasks/{task_id}",
            json=payload,
        )
        res.raise_for_status()
        plan = res.json()
        if _state.json:
            print_output(plan, True)
            return
        _print_task_update_summary(plan, task_id=task_id, payload=payload)


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


# Shared option definitions for task commands
_task_add_options = dict(
    title=typer.Option(..., "--title", "-t", help="Task title."),
    description=typer.Option(..., "--description", "-d", help="Task description."),
    status=typer.Option("todo", "--status", "-s", help="Task status."),
    owner=typer.Option(None, "--owner", "-o", help="Task owner."),
    notes=typer.Option(None, "--notes", "-n", help="Task notes."),
    linear_issue_id=typer.Option(
        None, "--linear-issue-id", "-i", help="Linear issue ID."
    ),
    blocked_reason=typer.Option(
        None, "--blocked-reason", "-b", "-r", help="Blocked reason."
    ),
    link=typer.Option(None, "--link", "-l", help="Artifact link."),
)


@task_app.command("plan")
def _task_plan(
    plan_id: Annotated[
        Optional[str],
        typer.Argument(help="Plan ID. Uses saved plan context if omitted."),
    ] = None,
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
    title: Annotated[str, typer.Option("--title", "-t", help="Task title.")] = ...,
    description: Annotated[
        str, typer.Option("--description", "-d", help="Task description.")
    ] = ...,
    status: Annotated[
        str, typer.Option("--status", "-s", help="Task status.")
    ] = "todo",
    owner: Annotated[
        Optional[str], typer.Option("--owner", "-o", help="Task owner.")
    ] = None,
    notes: Annotated[
        Optional[str], typer.Option("--notes", "-n", help="Task notes.")
    ] = None,
    linear_issue_id: Annotated[
        Optional[str], typer.Option("--linear-issue-id", "-i", help="Linear issue ID.")
    ] = None,
    blocked_reason: Annotated[
        Optional[str],
        typer.Option("--blocked-reason", "-b", "-r", help="Blocked reason."),
    ] = None,
    link: Annotated[
        Optional[list[str]], typer.Option("--link", "-l", help="Artifact link.")
    ] = None,
):
    """Add a new task to a plan."""
    _do_task_add(
        plan_id_option or plan_id,
        title,
        description,
        status,
        owner,
        notes,
        linear_issue_id,
        blocked_reason,
        link,
    )


@plan_task_app.command("add")
def _plan_task_add(
    plan_id: Annotated[
        Optional[str],
        typer.Argument(help="Plan ID. Uses saved plan context if omitted."),
    ] = None,
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
    title: Annotated[str, typer.Option("--title", "-t", help="Task title.")] = ...,
    description: Annotated[
        str, typer.Option("--description", "-d", help="Task description.")
    ] = ...,
    status: Annotated[
        str, typer.Option("--status", "-s", help="Task status.")
    ] = "todo",
    owner: Annotated[
        Optional[str], typer.Option("--owner", "-o", help="Task owner.")
    ] = None,
    notes: Annotated[
        Optional[str], typer.Option("--notes", "-n", help="Task notes.")
    ] = None,
    linear_issue_id: Annotated[
        Optional[str], typer.Option("--linear-issue-id", "-i", help="Linear issue ID.")
    ] = None,
    blocked_reason: Annotated[
        Optional[str],
        typer.Option("--blocked-reason", "-b", "-r", help="Blocked reason."),
    ] = None,
    link: Annotated[
        Optional[list[str]], typer.Option("--link", "-l", help="Artifact link.")
    ] = None,
):
    """Add a new task to a plan."""
    _do_task_add(
        plan_id_option or plan_id,
        title,
        description,
        status,
        owner,
        notes,
        linear_issue_id,
        blocked_reason,
        link,
    )


@task_app.command("view")
def _task_view(
    plan_id_or_task_id: Annotated[
        str, typer.Argument(help="Plan ID, or Task ID if a default plan is saved.")
    ],
    task_id: Annotated[
        Optional[str],
        typer.Argument(help="Task ID. Optional when a default plan is saved."),
    ] = None,
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
):
    """Show task details for a plan task."""
    if plan_id_option:
        _view_task(plan_id_option, task_id or plan_id_or_task_id)
        return
    if task_id is None:
        _view_task(None, plan_id_or_task_id)
        return
    _view_task(plan_id_or_task_id, task_id)


@plan_task_app.command("view")
def _plan_task_view(
    plan_id_or_task_id: Annotated[
        str, typer.Argument(help="Plan ID, or Task ID if a default plan is saved.")
    ],
    task_id: Annotated[
        Optional[str],
        typer.Argument(help="Task ID. Optional when a default plan is saved."),
    ] = None,
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
):
    """Show task details for a plan task."""
    if plan_id_option:
        _view_task(plan_id_option, task_id or plan_id_or_task_id)
        return
    if task_id is None:
        _view_task(None, plan_id_or_task_id)
        return
    _view_task(plan_id_or_task_id, task_id)


@task_app.command("delete")
def _task_delete(
    plan_id_or_task_id: Annotated[
        str, typer.Argument(help="Plan ID, or Task ID if a default plan is saved.")
    ],
    task_id: Annotated[
        Optional[str],
        typer.Argument(help="Task ID. Optional when a default plan is saved."),
    ] = None,
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
    feedback_reason: Annotated[
        Optional[str], typer.Option("--reason", help="Why this task was removed.")
    ] = None,
    assume_yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Delete without confirmation.")
    ] = False,
):
    """Delete a task from a plan."""
    if plan_id_option:
        _delete_task(
            plan_id_option,
            task_id or plan_id_or_task_id,
            feedback_reason,
            assume_yes,
        )
        return
    if task_id is None:
        _delete_task(None, plan_id_or_task_id, feedback_reason, assume_yes)
        return
    _delete_task(plan_id_or_task_id, task_id, feedback_reason, assume_yes)


@plan_task_app.command("delete")
def _plan_task_delete(
    plan_id_or_task_id: Annotated[
        str, typer.Argument(help="Plan ID, or Task ID if a default plan is saved.")
    ],
    task_id: Annotated[
        Optional[str],
        typer.Argument(help="Task ID. Optional when a default plan is saved."),
    ] = None,
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
    feedback_reason: Annotated[
        Optional[str], typer.Option("--reason", help="Why this task was removed.")
    ] = None,
    assume_yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Delete without confirmation.")
    ] = False,
):
    """Delete a task from a plan."""
    if plan_id_option:
        _delete_task(
            plan_id_option,
            task_id or plan_id_or_task_id,
            feedback_reason,
            assume_yes,
        )
        return
    if task_id is None:
        _delete_task(None, plan_id_or_task_id, feedback_reason, assume_yes)
        return
    _delete_task(plan_id_or_task_id, task_id, feedback_reason, assume_yes)


@task_app.command("edit")
def _task_edit(
    plan_id_or_task_id: Annotated[
        str, typer.Argument(help="Plan ID, or Task ID if a default plan is saved.")
    ],
    task_id: Annotated[
        Optional[str],
        typer.Argument(help="Task ID. Optional when a default plan is saved."),
    ] = None,
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
    title: Annotated[
        Optional[str], typer.Option("--title", "-t", help="Task title.")
    ] = None,
    description: Annotated[
        Optional[str], typer.Option("--description", "-d", help="Task description.")
    ] = None,
    status: Annotated[
        Optional[str], typer.Option("--status", "-s", help="Task status.")
    ] = None,
    owner: Annotated[
        Optional[str], typer.Option("--owner", "-o", help="Task owner.")
    ] = None,
    notes: Annotated[
        Optional[str], typer.Option("--notes", "-n", help="Task notes.")
    ] = None,
    linear_issue_id: Annotated[
        Optional[str], typer.Option("--linear-issue-id", "-i", help="Linear issue ID.")
    ] = None,
    blocked_reason: Annotated[
        Optional[str],
        typer.Option("--blocked-reason", "-b", "-r", help="Blocked reason."),
    ] = None,
    feedback_reason: Annotated[
        Optional[str], typer.Option("--reason", help="Why this task changed.")
    ] = None,
    link: Annotated[
        Optional[list[str]], typer.Option("--link", "-l", help="Artifact link.")
    ] = None,
):
    """Update an existing task's status, owner, or details."""
    resolved_plan_id: str | None
    resolved_task_id: str
    if plan_id_option:
        resolved_plan_id = plan_id_option
        resolved_task_id = task_id or plan_id_or_task_id
    elif task_id is None:
        resolved_plan_id = _require_plan_context(None)
        resolved_task_id = plan_id_or_task_id
    else:
        resolved_plan_id = plan_id_or_task_id
        resolved_task_id = task_id
    _do_task_update(
        resolved_plan_id,
        resolved_task_id,
        title,
        description,
        status,
        owner,
        notes,
        linear_issue_id,
        blocked_reason,
        feedback_reason,
        link,
    )


@task_app.command("start")
def _task_start(
    plan_id_or_task_id: Annotated[
        str, typer.Argument(help="Plan ID, or Task ID if a default plan is saved.")
    ],
    task_id: Annotated[
        Optional[str],
        typer.Argument(help="Task ID. Optional when a default plan is saved."),
    ] = None,
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
    owner: Annotated[
        Optional[str], typer.Option("--owner", "-o", help="Task owner.")
    ] = None,
    notes: Annotated[
        Optional[str], typer.Option("--notes", "-n", help="Start note.")
    ] = None,
    feedback_reason: Annotated[
        Optional[str], typer.Option("--reason", help="Why this task is starting now.")
    ] = None,
    link: Annotated[
        Optional[list[str]], typer.Option("--link", "-l", help="Artifact link.")
    ] = None,
):
    """Mark a task as in progress."""
    resolved_plan_id: str | None
    resolved_task_id: str
    if plan_id_option:
        resolved_plan_id = plan_id_option
        resolved_task_id = task_id or plan_id_or_task_id
    elif task_id is None:
        resolved_plan_id = _require_plan_context(None)
        resolved_task_id = plan_id_or_task_id
    else:
        resolved_plan_id = plan_id_or_task_id
        resolved_task_id = task_id
    _do_task_start(
        resolved_plan_id,
        resolved_task_id,
        owner=owner,
        notes=notes,
        feedback_reason=feedback_reason,
        link=link,
    )


@task_app.command("done")
def _task_done(
    plan_id_or_task_id: Annotated[
        str, typer.Argument(help="Plan ID, or Task ID if a default plan is saved.")
    ],
    task_id: Annotated[
        Optional[str],
        typer.Argument(help="Task ID. Optional when a default plan is saved."),
    ] = None,
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
    notes: Annotated[
        Optional[str], typer.Option("--notes", "-n", help="Completion note.")
    ] = None,
    feedback_reason: Annotated[
        Optional[str],
        typer.Option("--reason", help="Why this task is considered done."),
    ] = None,
    link: Annotated[
        Optional[list[str]], typer.Option("--link", "-l", help="Artifact link.")
    ] = None,
    cost: Annotated[
        Optional[float], typer.Option("--cost", help="Session cost in USD.")
    ] = None,
    tokens: Annotated[
        Optional[int], typer.Option("--tokens", help="Total tokens used.")
    ] = None,
    model_name: Annotated[
        Optional[str],
        typer.Option("--model", help="Model used (e.g., claude-sonnet-4-20250514)."),
    ] = None,
):
    """Mark a task as completed. Use --cost/--tokens to report agent session cost."""
    resolved_plan_id: str | None
    resolved_task_id: str
    if plan_id_option:
        resolved_plan_id = plan_id_option
        resolved_task_id = task_id or plan_id_or_task_id
    elif task_id is None:
        resolved_plan_id = _require_plan_context(None)
        resolved_task_id = plan_id_or_task_id
    else:
        resolved_plan_id = plan_id_or_task_id
        resolved_task_id = task_id
    _do_task_done(
        resolved_plan_id,
        resolved_task_id,
        notes=notes,
        feedback_reason=feedback_reason,
        link=link,
    )
    # Report cost via agent API
    if cost is not None or tokens is not None:
        try:
            with make_client(_state.api_url, _state.token) as c:
                c.post(
                    f"/api/v1/agent/plans/{resolved_plan_id}/task-event",
                    json={
                        "task_id": resolved_task_id,
                        "event": "note",
                        "note": f"Session cost: ${cost or 0:.4f} ({tokens or 0:,} tokens, {model_name or 'unknown'})",
                        "tokens_used": tokens or 0,
                        "model": model_name or "",
                        "cost_usd": cost or 0,
                    },
                    timeout=10,
                )
        except Exception:
            pass


@task_app.command("block")
def _task_block(
    plan_id_or_task_id: Annotated[
        str, typer.Argument(help="Plan ID, or Task ID if a default plan is saved.")
    ],
    task_id: Annotated[
        Optional[str],
        typer.Argument(help="Task ID. Optional when a default plan is saved."),
    ] = None,
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
    blocked_reason: Annotated[
        str, typer.Option("--reason", "-r", help="Why the task is blocked.")
    ] = ...,
    feedback_reason: Annotated[
        Optional[str],
        typer.Option("--feedback-reason", help="Extra context for the audit trail."),
    ] = None,
):
    """Mark a task as blocked."""
    resolved_plan_id: str | None
    resolved_task_id: str
    if plan_id_option:
        resolved_plan_id = plan_id_option
        resolved_task_id = task_id or plan_id_or_task_id
    elif task_id is None:
        resolved_plan_id = _require_plan_context(None)
        resolved_task_id = plan_id_or_task_id
    else:
        resolved_plan_id = plan_id_or_task_id
        resolved_task_id = task_id
    _do_task_block(
        resolved_plan_id,
        resolved_task_id,
        blocked_reason=blocked_reason,
        feedback_reason=feedback_reason,
    )


@task_app.command("unblock")
def _task_unblock(
    plan_id_or_task_id: Annotated[
        str, typer.Argument(help="Plan ID, or Task ID if a default plan is saved.")
    ],
    task_id: Annotated[
        Optional[str],
        typer.Argument(help="Task ID. Optional when a default plan is saved."),
    ] = None,
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
    notes: Annotated[
        Optional[str],
        typer.Option(
            "--notes", "-n", help="Short note about how the blocker was resolved."
        ),
    ] = None,
    status: Annotated[
        str, typer.Option("--status", "-s", help="Status to use after unblocking.")
    ] = "in_progress",
    feedback_reason: Annotated[
        Optional[str],
        typer.Option("--reason", help="Why the task is being unblocked now."),
    ] = None,
):
    """Clear a task blocker and resume work."""
    resolved_plan_id: str | None
    resolved_task_id: str
    if plan_id_option:
        resolved_plan_id = plan_id_option
        resolved_task_id = task_id or plan_id_or_task_id
    elif task_id is None:
        resolved_plan_id = _require_plan_context(None)
        resolved_task_id = plan_id_or_task_id
    else:
        resolved_plan_id = plan_id_or_task_id
        resolved_task_id = task_id
    _do_task_unblock(
        resolved_plan_id,
        resolved_task_id,
        notes=notes,
        feedback_reason=feedback_reason,
        status=status,
    )


@task_app.command("note")
def _task_note(
    plan_id_or_task_id: Annotated[
        str, typer.Argument(help="Plan ID, or Task ID if a default plan is saved.")
    ],
    task_id: Annotated[
        Optional[str],
        typer.Argument(help="Task ID. Optional when a default plan is saved."),
    ] = None,
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
    note: Annotated[
        str, typer.Option("--note", "-n", help="Note to append to the task.")
    ] = ...,
    feedback_reason: Annotated[
        Optional[str], typer.Option("--reason", help="Why this note matters.")
    ] = None,
):
    """Append a timestamped note to a task."""
    resolved_plan_id: str | None
    resolved_task_id: str
    if plan_id_option:
        resolved_plan_id = plan_id_option
        resolved_task_id = task_id or plan_id_or_task_id
    elif task_id is None:
        resolved_plan_id = _require_plan_context(None)
        resolved_task_id = plan_id_or_task_id
    else:
        resolved_plan_id = plan_id_or_task_id
        resolved_task_id = task_id
    _append_task_note(
        resolved_plan_id,
        resolved_task_id,
        note=note,
        feedback_reason=feedback_reason,
    )


@task_app.command("artifact")
def _task_artifact(
    plan_id_or_task_id: Annotated[
        str, typer.Argument(help="Plan ID, or Task ID if a default plan is saved.")
    ],
    task_id: Annotated[
        Optional[str],
        typer.Argument(help="Task ID. Optional when a default plan is saved."),
    ] = None,
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
    link: Annotated[
        str, typer.Option("--link", "-l", help="Artifact URL to attach to the task.")
    ] = ...,
    feedback_reason: Annotated[
        Optional[str], typer.Option("--reason", help="Why this artifact matters.")
    ] = None,
):
    """Attach an artifact link to a task without overwriting existing links."""
    resolved_plan_id: str | None
    resolved_task_id: str
    if plan_id_option:
        resolved_plan_id = plan_id_option
        resolved_task_id = task_id or plan_id_or_task_id
    elif task_id is None:
        resolved_plan_id = _require_plan_context(None)
        resolved_task_id = plan_id_or_task_id
    else:
        resolved_plan_id = plan_id_or_task_id
        resolved_task_id = task_id
    _add_task_artifact(
        resolved_plan_id,
        resolved_task_id,
        artifact_link=link,
        feedback_reason=feedback_reason,
    )


# Legacy aliases


@app.command("plan_task", hidden=True)
def _plan_task_alias(
    plan_id: Annotated[
        Optional[str],
        typer.Argument(help="Plan ID. Uses saved plan context if omitted."),
    ] = None,
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
    title: Annotated[str, typer.Option("--title", "-t", help="Task title.")] = ...,
    description: Annotated[
        str, typer.Option("--description", "-d", help="Task description.")
    ] = ...,
    status: Annotated[
        str, typer.Option("--status", "-s", help="Task status.")
    ] = "todo",
    owner: Annotated[
        Optional[str], typer.Option("--owner", "-o", help="Task owner.")
    ] = None,
    notes: Annotated[
        Optional[str], typer.Option("--notes", "-n", help="Task notes.")
    ] = None,
    linear_issue_id: Annotated[
        Optional[str], typer.Option("--linear-issue-id", "-i", help="Linear issue ID.")
    ] = None,
    blocked_reason: Annotated[
        Optional[str],
        typer.Option("--blocked-reason", "-b", "-r", help="Blocked reason."),
    ] = None,
    link: Annotated[
        Optional[list[str]], typer.Option("--link", "-l", help="Artifact link.")
    ] = None,
):
    _do_task_add(
        plan_id_option or plan_id,
        title,
        description,
        status,
        owner,
        notes,
        linear_issue_id,
        blocked_reason,
        link,
    )


@app.command("edit_task", hidden=True)
def _edit_task_alias(
    plan_id_or_task_id: Annotated[
        str, typer.Argument(help="Plan ID, or Task ID if a default plan is saved.")
    ],
    task_id: Annotated[
        Optional[str],
        typer.Argument(help="Task ID. Optional when a default plan is saved."),
    ] = None,
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
    title: Annotated[
        Optional[str], typer.Option("--title", "-t", help="Task title.")
    ] = None,
    description: Annotated[
        Optional[str], typer.Option("--description", "-d", help="Task description.")
    ] = None,
    status: Annotated[
        Optional[str], typer.Option("--status", "-s", help="Task status.")
    ] = None,
    owner: Annotated[
        Optional[str], typer.Option("--owner", "-o", help="Task owner.")
    ] = None,
    notes: Annotated[
        Optional[str], typer.Option("--notes", "-n", help="Task notes.")
    ] = None,
    linear_issue_id: Annotated[
        Optional[str], typer.Option("--linear-issue-id", "-i", help="Linear issue ID.")
    ] = None,
    blocked_reason: Annotated[
        Optional[str],
        typer.Option("--blocked-reason", "-b", "-r", help="Blocked reason."),
    ] = None,
    link: Annotated[
        Optional[list[str]], typer.Option("--link", "-l", help="Artifact link.")
    ] = None,
):
    resolved_plan_id: str | None
    resolved_task_id: str
    if plan_id_option:
        resolved_plan_id = plan_id_option
        resolved_task_id = task_id or plan_id_or_task_id
    elif task_id is None:
        resolved_plan_id = _require_plan_context(None)
        resolved_task_id = plan_id_or_task_id
    else:
        resolved_plan_id = plan_id_or_task_id
        resolved_task_id = task_id
    _do_task_update(
        resolved_plan_id,
        resolved_task_id,
        title,
        description,
        status,
        owner,
        notes,
        linear_issue_id,
        blocked_reason,
        feedback_reason,
        link,
    )


# ---------------------------------------------------------------------------
# Rollback command
# ---------------------------------------------------------------------------


@app.command("rollback")
def _rollback(
    task_id: Annotated[
        str,
        typer.Argument(
            help="Task ID to rollback to (reverts to the checkpoint before this task)."
        ),
    ],
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", "-f", help="Skip confirmation.")
    ] = False,
):
    """Rollback to the git checkpoint before a task was started."""
    resolved_plan_id = _require_plan_context(plan_id_option)

    # Check for uncommitted changes
    status_result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if (status_result.stdout or "").strip():
        if not force:
            print(
                f"{YELLOW}Warning: You have uncommitted changes that will be lost.{RESET}"
            )
            confirm = typer.confirm("Continue with rollback?", default=False)
            if not confirm:
                print("Rollback cancelled.")
                raise typer.Exit(0)

    # Find the checkpoint commit for this task
    grep_pattern = f"keshro: checkpoint before {task_id}"
    log_result = subprocess.run(
        ["git", "log", "--oneline", "--grep", grep_pattern, "-1", "--format=%H %s"],
        capture_output=True,
        text=True,
        check=False,
    )
    checkpoint_line = (log_result.stdout or "").strip()
    if not checkpoint_line:
        # Try alternative checkpoint format
        grep_pattern2 = "keshro: checkpoint"
        log_result2 = subprocess.run(
            ["git", "log", "--oneline", "--grep", grep_pattern2, "--format=%H %s"],
            capture_output=True,
            text=True,
            check=False,
        )
        checkpoints = [
            el.strip() for el in (log_result2.stdout or "").splitlines() if el.strip()
        ]
        if not checkpoints:
            print(
                f"{RED}No checkpoint commit found for task '{task_id}'.{RESET}",
                file=sys.stderr,
            )
            print(
                "Checkpoints are created automatically when 'keshro continue' starts a task.",
                file=sys.stderr,
            )
            raise typer.Exit(1)
        # Show available checkpoints
        print(
            f"{YELLOW}No checkpoint specifically for task '{task_id}'. Available checkpoints:{RESET}"
        )
        for cp in checkpoints[:10]:
            parts = cp.split(" ", 1)
            print(f"  {DIM}{parts[0][:8]}{RESET}  {parts[1] if len(parts) > 1 else ''}")
        raise typer.Exit(1)

    commit_hash = checkpoint_line.split(" ", 1)[0]
    commit_msg = checkpoint_line.split(" ", 1)[1] if " " in checkpoint_line else ""

    if not force:
        print(f"Will rollback to: {DIM}{commit_hash[:8]}{RESET} {commit_msg}")
        confirm = typer.confirm("Proceed?", default=True)
        if not confirm:
            print("Rollback cancelled.")
            raise typer.Exit(0)

    # Perform the rollback
    result = subprocess.run(
        ["git", "reset", "--hard", commit_hash],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"{RED}Rollback failed: {result.stderr}{RESET}", file=sys.stderr)
        raise typer.Exit(1)

    print(f"{GREEN}Rolled back to checkpoint {commit_hash[:8]}{RESET}")

    # Record rollback in Keshro audit trail — resets task to todo
    try:
        client = make_client()
        client.post(
            f"/api/v1/agent/plans/{resolved_plan_id}/task-event",
            json={
                "task_id": task_id,
                "event": "rollback",
                "reason": f"Rolled back to checkpoint {commit_hash[:8]}",
            },
            timeout=10,
        )
        print(f"  {DIM}Task '{task_id}' reset to todo in plan.{RESET}")
    except Exception:
        print(
            f"  {YELLOW}Could not update plan (API unreachable). Task status unchanged.{RESET}"
        )

    if _state.json:
        print_output(
            {
                "status": "ok",
                "action": "rollback",
                "commit": commit_hash,
                "task_id": task_id,
            }
        )


# ---------------------------------------------------------------------------
# Explain command
# ---------------------------------------------------------------------------


@app.command("explain")
def _explain(
    task_id: Annotated[str, typer.Argument(help="Task ID to explain decisions for.")],
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
):
    """Show the decision audit trail for a task."""
    resolved_plan_id = _require_plan_context(plan_id_option)
    client = make_client()

    try:
        resp = client.get(f"/api/v1/plans/{resolved_plan_id}")
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _print_http_error(exc)
        raise typer.Exit(1) from exc

    plan = resp.json()
    task = _find_task(plan, task_id)
    if not task:
        print(f"{RED}Task '{task_id}' not found in plan.{RESET}", file=sys.stderr)
        raise typer.Exit(1)

    decisions = task.get("decisions", [])
    if not decisions:
        print(f"{DIM}No decisions recorded for task '{task_id}'.{RESET}")
        print(
            "Agents record decisions via 'keshro task decide' or the /api/agent/decide endpoint."
        )
        if _state.json:
            print_output({"task_id": task_id, "decisions": []})
        raise typer.Exit(0)

    if _state.json:
        print_output(
            {"task_id": task_id, "title": task.get("title", ""), "decisions": decisions}
        )
        raise typer.Exit(0)

    print(f"\n{CYAN}Decisions for: {task.get('title', task_id)}{RESET}")
    print(f"{DIM}{'─' * 60}{RESET}\n")

    for i, decision in enumerate(decisions, 1):
        ts = decision.get("timestamp", "")
        if ts:
            ts = ts[:19].replace("T", " ")
        print(f"  {YELLOW}Decision #{i}{RESET}  {DIM}{ts}{RESET}")
        print(f"  {CYAN}Context:{RESET}  {decision.get('context', 'N/A')}")

        alternatives = decision.get("alternatives", [])
        if alternatives:
            print(f"  {CYAN}Alternatives considered:{RESET}")
            for alt in alternatives:
                print(f"    • {alt}")

        print(f"  {GREEN}Choice:{RESET}  {decision.get('choice', 'N/A')}")
        print(f"  {CYAN}Reasoning:{RESET}  {decision.get('reasoning', 'N/A')}")
        print()


# ---------------------------------------------------------------------------
# Plan create (generic) command
# ---------------------------------------------------------------------------


@plan_app.command("generate")
def _plan_generate(
    description: Annotated[
        str, typer.Argument(help="Description of the project/work to plan.")
    ],
    plan_type: Annotated[
        str, typer.Option("--type", "-t", help="Project type: generic, migration.")
    ] = "generic",
    title: Annotated[
        Optional[str],
        typer.Option("--title", help="Plan title. Auto-generated if omitted."),
    ] = None,
    confirm: Annotated[
        bool, typer.Option("--confirm", help="Auto-confirm the draft plan.")
    ] = False,
):
    """Generate a plan from a description using AI."""
    _ensure_authenticated()
    client = make_client()

    print(f"{CYAN}Generating plan from description...{RESET}")

    # For migration type, use existing flow
    if plan_type == "migration":
        print(
            f"{YELLOW}For migration plans, use 'keshro create' with source/target types.{RESET}"
        )
        raise typer.Exit(0)

    # Call the plan generation endpoint
    payload = {
        "description": description,
        "project_type": plan_type,
        "title": title,
    }

    try:
        resp = client.post("/api/v1/plans/generate", json=payload, timeout=120)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            # Endpoint not yet available — provide helpful message
            print(f"{YELLOW}Plan generation endpoint not yet available.{RESET}")
            print(
                "The /api/plans/generate endpoint will be added in the next backend update."
            )
            print("\nIn the meantime, create plans manually:")
            print("  keshro plan create --from-file plan.json")
            raise typer.Exit(0) from exc
        _print_http_error(exc)
        raise typer.Exit(1) from exc

    plan = resp.json()
    plan_id = plan.get("id", "")
    steps = plan.get("plan_steps", [])

    _set_default_plan_after_create(plan)

    print(f"\n{GREEN}Plan created: {plan.get('title', 'Untitled')}{RESET}")
    print(f"  ID: {plan_id}")
    print(f"  Tasks: {len(steps)}")
    print(f"  Status: {plan.get('status', 'draft')}")

    if steps:
        print(f"\n{CYAN}Tasks:{RESET}")
        for step in steps:
            dep_info = ""
            if step.get("depends_on"):
                dep_info = f" {DIM}(depends on: {', '.join(step['depends_on'])}){RESET}"
            print(
                f"  {step.get('order', 0):2d}. {step.get('title', 'Untitled')}{dep_info}"
            )

    if not confirm and plan.get("status") == "draft":
        print(
            f"\n{DIM}Plan is in draft. Run 'keshro continue -p {plan_id} --confirm' to start execution.{RESET}"
        )

    if _state.json:
        print_output(plan)


# ---------------------------------------------------------------------------
# Plan import command
# ---------------------------------------------------------------------------


@plan_app.command("import")
def _plan_import(
    provider: Annotated[
        str, typer.Argument(help="Provider to import from: linear, jira, github.")
    ],
    project_id: Annotated[
        Optional[str], typer.Option("--project", "-p", help="Project or repo ID.")
    ] = None,
    title: Annotated[
        Optional[str], typer.Option("--title", "-t", help="Plan title.")
    ] = None,
    skip_questions: Annotated[
        bool, typer.Option("--skip-questions", help="Skip clarifying questions.")
    ] = False,
):
    """Import issues from Linear, Jira, or GitHub and generate a plan.

    Interactive flow: fetches issues → asks clarifying questions → generates plan.
    Use --skip-questions to go straight to plan generation.
    """
    _ensure_authenticated()
    client = make_client()

    provider = provider.lower().strip()
    if provider not in ("linear", "jira", "github"):
        print(
            f"{RED}Unsupported provider: {provider}. Use: linear, jira, github{RESET}",
            file=sys.stderr,
        )
        raise typer.Exit(1)

    # Step 1: Fetch issues + enrichment + questions
    print(f"{CYAN}Fetching issues from {provider}...{RESET}")
    preview_payload: dict = {"provider": provider}
    if project_id:
        preview_payload["project_id"] = project_id

    try:
        resp = client.post(
            "/api/v1/plans/import/preview", json=preview_payload, timeout=60
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _print_http_error(exc)
        raise typer.Exit(1) from exc

    preview = resp.json()
    issue_count = preview.get("issue_count", 0)
    questions = preview.get("questions", [])
    enrichment = preview.get("enrichment_context", "")

    print(f"  {GREEN}Found {issue_count} issues{RESET}")
    if enrichment:
        print(f"  {DIM}Enriched with repo context + web research{RESET}")

    # Step 2: Ask clarifying questions (interactive)
    answers: dict[str, str] = {}
    if questions and not skip_questions and sys.stdout.isatty():
        print(f"\n{CYAN}Clarifying questions ({len(questions)}):{RESET}")
        print(
            f"{DIM}Your answers help produce a better plan. Press Enter to skip any question.{RESET}\n"
        )

        for q in questions:
            qid = q.get("id", "")
            question_text = q.get("question", "")
            why = q.get("why_this_matters", "")
            options = q.get("answers", [])
            placeholder = q.get("placeholder", "")

            print(f"  {YELLOW}{question_text}{RESET}")
            if why:
                print(f"  {DIM}{why}{RESET}")

            if options:
                for idx, opt in enumerate(options, 1):
                    rec = " (recommended)" if opt.get("recommended") else ""
                    print(
                        f"    {idx}. {opt.get('answer_title', opt.get('value', ''))}{rec}"
                    )
                raw = input(
                    f"  {DIM}Enter number or type answer [{placeholder or 'skip'}]: {RESET}"
                ).strip()
                if raw:
                    # Check if it's a number selecting an option
                    try:
                        choice_idx = int(raw) - 1
                        if 0 <= choice_idx < len(options):
                            answers[qid] = options[choice_idx].get("value", raw)
                        else:
                            answers[qid] = raw
                    except ValueError:
                        answers[qid] = raw
            else:
                raw = input(f"  {DIM}Answer [{placeholder or 'skip'}]: {RESET}").strip()
                if raw:
                    answers[qid] = raw
            print()

        answered = len(answers)
        print(f"  {DIM}{answered}/{len(questions)} questions answered.{RESET}\n")

    # Step 3: Generate plan
    print(f"{CYAN}Generating plan...{RESET}")
    import_payload: dict = {
        "provider": provider,
        "issues_text": preview.get("issues_text", ""),
        "enrichment_context": enrichment,
    }
    if title:
        import_payload["title"] = title
    if project_id:
        import_payload["project_id"] = project_id
    if answers:
        import_payload["answers"] = answers

    try:
        resp = client.post("/api/v1/plans/import", json=import_payload, timeout=120)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _print_http_error(exc)
        raise typer.Exit(1) from exc

    plan = resp.json()
    plan_id = plan.get("id", "")
    steps = plan.get("plan_steps", [])

    _set_default_plan_after_create(plan)

    print(f"\n{GREEN}Plan created: {plan.get('title', 'Untitled')}{RESET}")
    print(f"  ID: {plan_id}")
    print(f"  Tasks: {len(steps)}")
    print(f"  Status: {plan.get('status', 'draft')}")

    if steps:
        print(f"\n{CYAN}Tasks:{RESET}")
        for step in steps:
            dep_info = ""
            if step.get("depends_on"):
                dep_info = f" {DIM}(depends on: {', '.join(step['depends_on'])}){RESET}"
            refs = step.get("source_refs", [])
            ref_info = f" {DIM}[{', '.join(refs)}]{RESET}" if refs else ""
            print(
                f"  {step.get('order', 0):2d}. {step.get('title', 'Untitled')}{ref_info}{dep_info}"
            )

    print(
        f"\n{DIM}Run 'keshro continue -p {plan_id} --confirm' to start execution.{RESET}"
    )

    if _state.json:
        print_output(plan)


# ---------------------------------------------------------------------------
# Task decide command
# ---------------------------------------------------------------------------


@task_app.command("decide")
def _task_decide(
    plan_id_or_task_id: Annotated[
        str, typer.Argument(help="Plan ID, or Task ID if a default plan is saved.")
    ],
    task_id: Annotated[
        Optional[str],
        typer.Argument(help="Task ID. Optional when a default plan is saved."),
    ] = None,
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
    context: Annotated[
        str, typer.Option("--context", "-c", help="Decision context.")
    ] = "",
    choice: Annotated[str, typer.Option("--choice", help="What was decided.")] = "",
    reasoning: Annotated[
        str, typer.Option("--reasoning", "-r", help="Why this choice was made.")
    ] = "",
    alternatives: Annotated[
        Optional[list[str]],
        typer.Option("--alt", "-a", help="Alternative considered (repeatable)."),
    ] = None,
):
    """Record a structured decision for a task."""
    resolved_plan_id: str | None
    resolved_task_id: str
    if plan_id_option:
        resolved_plan_id = plan_id_option
        resolved_task_id = task_id or plan_id_or_task_id
    elif task_id is None:
        resolved_plan_id = _require_plan_context(None)
        resolved_task_id = plan_id_or_task_id
    else:
        resolved_plan_id = plan_id_or_task_id
        resolved_task_id = task_id

    if not context or not choice or not reasoning:
        print(
            f"{RED}All of --context, --choice, and --reasoning are required.{RESET}",
            file=sys.stderr,
        )
        raise typer.Exit(1)

    client = make_client()
    payload = {
        "task_id": resolved_task_id,
        "context": context,
        "alternatives": alternatives or [],
        "choice": choice,
        "reasoning": reasoning,
    }

    try:
        resp = client.post(f"/api/v1/agent/plans/{resolved_plan_id}/decide", json=payload)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _print_http_error(exc)
        raise typer.Exit(1) from exc

    result = resp.json()
    print(
        f"{GREEN}Decision recorded for task '{resolved_task_id}' ({result.get('decisions_count', 0)} total).{RESET}"
    )

    if _state.json:
        print_output(result)


# ---------------------------------------------------------------------------
# Error handlers
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
    url = str(exc.request.url) if exc.request else _state.api_url
    detail = f"Could not reach Keshro at {url}. Check that the API is running and your --api-url is correct."
    payload = {"status": "error", "detail": detail}
    if _state.json:
        print_output(payload, True)
        return
    print(f"{RED}{detail}{RESET}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__version__)
        return 0
    # Allow --json anywhere in the command line by hoisting it to the front
    if "--json" in argv[1:]:
        argv = [arg for arg in argv if arg != "--json"]
        argv = ["--json", *argv]
    try:
        app(argv, standalone_mode=False)
        return 0
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(f"{RED}{exc.code}{RESET}", file=sys.stderr)
            return 1
        return exc.code if isinstance(exc.code, int) and exc.code != 0 else 0
    except click.exceptions.UsageError as exc:
        print(f"{RED}Error: {exc.format_message()}{RESET}", file=sys.stderr)
        return 2
    except httpx.HTTPStatusError as exc:
        _print_http_error(exc)
        return 1
    except httpx.RequestError as exc:
        _print_request_error(exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
