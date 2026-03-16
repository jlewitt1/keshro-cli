import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import click
import httpx
import typer

from . import __version__
from .auth import cmd_auth_login, cmd_auth_logout
from .client import get_default_org_id, make_client, print_output
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
        res = client.get(f"/api/plans/{explicit_id}")
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
        res = client.get("/api/orgs")
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


def _view_task(plan_id: str | None, task_id: str) -> None:
    resolved_plan_id = _require_plan_context(plan_id)
    with make_client(_state.api_url, _state.token) as client:
        res = client.get(f"/api/plans/{resolved_plan_id}")
        res.raise_for_status()
        plan = res.json()
        if _state.json:
            print_output(plan, True)
            return
        _print_task_detail(plan, task_id=task_id)


def _get_plan_or_exit(plan_id: str | None) -> dict:
    resolved_plan_id = _require_plan_context(plan_id)
    with make_client(_state.api_url, _state.token) as client:
        res = client.get(f"/api/plans/{resolved_plan_id}")
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


def _next_actionable_task(plan: dict) -> dict | None:
    steps = sorted(plan.get("plan_steps") or [], key=lambda step: step.get("order", 0))
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
        plan_res = client.get(f"/api/plans/{resolved_plan_id}")
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
            f"/api/plans/{resolved_plan_id}/tasks/{task_id}",
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
        res = client.get("/api/migrations", params=params)
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
        res = client.get(f"/api/migrations/{migration_id}")
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
        migration_res = client.get(f"/api/migrations/{migration_id}")
        migration_res.raise_for_status()
        migration = migration_res.json()
        plan_res = client.get(f"/api/migrations/{migration_id}/plan")
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
        res = client.delete(f"/api/migrations/{migration_id}")
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
                me_res = client.get("/api/auth/me")
                me_res.raise_for_status()
                authenticated = True
                res = client.get("/api/orgs")
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
    clear_plan: Annotated[
        bool, typer.Option("--clear-plan", help="Clear saved plan context.")
    ] = False,
):
    """Set default workspace context."""
    updates: dict = {}
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
        res = client.get("/api/plans/templates")
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
            res = client.post("/api/plans/from-template", json=payload)
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
        res = client.post("/api/plans", json=payload)
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
        res = client.get("/api/plans", params=params)
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
        res = client.get(f"/api/plans/{plan_id}")
        res.raise_for_status()
        plan = res.json()
        if _state.json:
            print_output(plan, True)
            return
        context_label = _current_context_label() if _current_org_id() else None
        _print_plan_detail(plan, context_label=context_label)


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
        res = client.patch(f"/api/plans/{plan_id}", json=payload)
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
        res = client.patch(f"/api/plans/{resolved_plan_id}", json=payload)
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
        res = client.delete(f"/api/plans/{plan_id}")
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
        res = client.post(f"/api/plans/{plan_id}/tasks", json=payload)
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
            f"/api/plans/{plan_id}/tasks/{task_id}",
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
):
    """Mark a task as completed."""
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
