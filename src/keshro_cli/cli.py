import json
import re
import sys
from dataclasses import dataclass
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
outcome_app = typer.Typer(help="Outcome tracking")
config_app = typer.Typer(help="Configuration", invoke_without_command=True)
plan_task_app = typer.Typer(help="Plan task management")

app.add_typer(auth_app, name="auth")
app.add_typer(plan_app, name="plan")
app.add_typer(task_app, name="task")
app.add_typer(outcome_app, name="outcome")
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


def _delete_task(plan_id: str | None, task_id: str) -> None:
    resolved_plan_id = _require_plan_context(plan_id)
    with make_client(_state.api_url, _state.token) as client:
        res = client.delete(f"/api/plans/{resolved_plan_id}/tasks/{task_id}")
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
        key
        for key in ["title", "migration_id"]
        if not str(payload.get(key) or "").strip()
    ]
    if missing:
        raise SystemExit(
            f"Missing required plan fields: {', '.join(missing)}. "
            "Create the plan from a migration context and provide them directly or include them in the imported file."
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
    email: str | None = None,
    password: str | None = None,
    token: str | None = None,
):
    cmd_auth_login(
        api_url=_state.api_url,
        token=token,
        email=email,
        password=password,
        json_output=_state.json,
    )


def _do_logout():
    cmd_auth_logout(json_output=_state.json)


@auth_app.command("login")
def _auth_login(
    token_value: Annotated[
        Optional[str], typer.Argument(help="Personal access token.")
    ] = None,
    email: Annotated[Optional[str], typer.Option(help="Email address.")] = None,
    password: Annotated[Optional[str], typer.Option(help="Password.")] = None,
    token: Annotated[Optional[str], typer.Option(help="Personal access token.")] = None,
):
    """Authenticate with Keshro."""
    _do_login(email=email, password=password, token=token or token_value)


@auth_app.command("logout")
def _auth_logout():
    """Clear locally stored credentials."""
    _do_logout()


@app.command("login")
def _login_alias(
    token_value: Annotated[
        Optional[str], typer.Argument(help="Personal access token.")
    ] = None,
    email: Annotated[Optional[str], typer.Option(help="Email address.")] = None,
    password: Annotated[Optional[str], typer.Option(help="Password.")] = None,
    token: Annotated[Optional[str], typer.Option(help="Personal access token.")] = None,
):
    """Authenticate with Keshro."""
    _do_login(email=email, password=password, token=token or token_value)


@app.command("logout")
def _logout_alias():
    """Clear local credentials."""
    _do_logout()


# ---------------------------------------------------------------------------
# Config commands
# ---------------------------------------------------------------------------


def _config_show():
    auth = load_auth()
    payload = {
        "api_url": auth.get("api_url") or DEFAULT_API_URL,
        "authenticated": bool(auth.get("token")),
        "default_org_id": auth.get("default_org_id"),
        "default_org_name": auth.get("default_org_name"),
        "default_plan_id": auth.get("default_plan_id"),
        "default_plan_title": auth.get("default_plan_title"),
        "user": auth.get("user") or {},
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
    personal: Annotated[
        bool, typer.Option("--personal", help="Use personal context.")
    ] = False,
    clear_plan: Annotated[
        bool, typer.Option("--clear-plan", help="Clear saved plan context.")
    ] = False,
):
    """Set default workspace context."""
    updates: dict = {}
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
    """List available plan templates, or show details for one."""
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
    """List available plan templates, or show details for one."""
    _cmd_plan_templates(template_name, name, verbose)


# ---------------------------------------------------------------------------
# Plan commands
# ---------------------------------------------------------------------------


@plan_app.command("create")
def _plan_create(
    migration_id_arg: Annotated[
        Optional[str], typer.Argument(help="Migration ID.")
    ] = None,
    title: Annotated[
        Optional[str], typer.Option("--title", "-t", help="Plan title.")
    ] = None,
    summary: Annotated[
        Optional[str], typer.Option("--summary", "-u", help="Plan summary.")
    ] = None,
    status: Annotated[
        str, typer.Option("--status", "-s", help="Plan status.")
    ] = "draft",
    migration_id: Annotated[
        Optional[str], typer.Option("--migration-id", "-m", help="Migration ID.")
    ] = None,
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
    """Create a new migration plan from scratch, a template, or a file."""
    resolved_migration_id = migration_id or migration_id_arg
    if from_file and from_claude:
        raise typer.BadParameter("Cannot use both --from-file and --from-claude.")

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
        if _state.json:
            print_output(plans, True)
            return
        if not plans:
            if context_label:
                print(f"No plans found for org {context_label}.")
            else:
                print("No plans found.")
            return
        for plan in plans:
            _print_plan_summary(plan, verbose=verbose, context_label=context_label)


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


@plan_app.command("delete")
def _plan_delete(
    plan_id: Annotated[str, typer.Argument(help="Plan ID.")],
):
    """Delete a plan and its saved plan outcome."""
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
        _print_task_detail(plan, task_id=task_id)


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
):
    """Delete a task from a plan."""
    if plan_id_option:
        _delete_task(plan_id_option, task_id or plan_id_or_task_id)
        return
    if task_id is None:
        _delete_task(None, plan_id_or_task_id)
        return
    _delete_task(plan_id_or_task_id, task_id)


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
):
    """Delete a task from a plan."""
    if plan_id_option:
        _delete_task(plan_id_option, task_id or plan_id_or_task_id)
        return
    if task_id is None:
        _delete_task(None, plan_id_or_task_id)
        return
    _delete_task(plan_id_or_task_id, task_id)


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
        link,
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
        link,
    )


# ---------------------------------------------------------------------------
# Outcome commands
# ---------------------------------------------------------------------------


@outcome_app.command("view")
def _outcome_view(
    plan_id: Annotated[
        Optional[str],
        typer.Argument(help="Plan ID. Uses saved plan context if omitted."),
    ] = None,
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
):
    """View the outcome record for a plan."""
    plan_id = _require_plan_context(plan_id_option or plan_id)
    with make_client(_state.api_url, _state.token) as client:
        res = client.get(f"/api/plans/{plan_id}/outcome")
        res.raise_for_status()
        print_output(res.json(), _state.json)


@outcome_app.command("save")
def _outcome_save(
    plan_id: Annotated[
        Optional[str],
        typer.Argument(help="Plan ID. Uses saved plan context if omitted."),
    ] = None,
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
    status: Annotated[
        str, typer.Option("--status", "-s", help="Outcome status.")
    ] = ...,
    summary: Annotated[
        Optional[str], typer.Option("--summary", "-m", help="Outcome summary.")
    ] = None,
    notes: Annotated[
        Optional[str], typer.Option("--notes", "-n", help="Outcome notes.")
    ] = None,
    actual_hours: Annotated[
        Optional[int], typer.Option("--actual-hours", "-H", help="Actual hours.")
    ] = None,
    actual_cost: Annotated[
        Optional[int], typer.Option("--actual-cost", "-c", help="Actual cost.")
    ] = None,
):
    """Save the outcome of a completed migration plan."""
    plan_id = _require_plan_context(plan_id_option or plan_id)
    payload = {
        "status": status,
        "summary": summary,
        "notes": notes,
        "actual_hours": actual_hours,
        "actual_cost": actual_cost,
    }
    with make_client(_state.api_url, _state.token) as client:
        res = client.post(f"/api/plans/{plan_id}/outcome", json=payload)
        res.raise_for_status()
        print_output(res.json(), _state.json)


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
    print(f"Keshro API error ({status}): {detail}", file=sys.stderr)


def _print_request_error(exc: httpx.RequestError) -> None:
    url = str(exc.request.url) if exc.request else _state.api_url
    detail = f"Could not reach Keshro at {url}. Check that the API is running and your --api-url is correct."
    payload = {"status": "error", "detail": detail}
    if _state.json:
        print_output(payload, True)
        return
    print(detail, file=sys.stderr)


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
            print(exc.code, file=sys.stderr)
            return 1
        return exc.code if isinstance(exc.code, int) and exc.code != 0 else 0
    except click.exceptions.UsageError as exc:
        print(f"Error: {exc.format_message()}", file=sys.stderr)
        return 2
    except httpx.HTTPStatusError as exc:
        _print_http_error(exc)
        return 1
    except httpx.RequestError as exc:
        _print_request_error(exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
