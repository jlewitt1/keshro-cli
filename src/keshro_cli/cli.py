import argparse
import json
import re
import sys
from pathlib import Path

from . import __version__
from .auth import cmd_auth_login, cmd_auth_logout
from .client import make_client, print_output
from .config import DEFAULT_API_URL, load_auth


RESET = "\033[0m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"


def _read_json_file(path: str | None):
    if not path:
        return []
    return json.loads(Path(path).read_text())


def cmd_config(args):
    auth = load_auth()
    payload = {
        "api_url": auth.get("api_url") or DEFAULT_API_URL,
        "authenticated": bool(auth.get("token")),
        "user": auth.get("user") or {},
    }
    if args.json:
        print_output(payload, True)
        return
    user = payload["user"] or {}
    print(f"{DIM}API URL:{RESET} {CYAN}{payload['api_url']}{RESET}")
    print(
        f"{DIM}Authenticated:{RESET} "
        f"{GREEN if payload['authenticated'] else CYAN}{'yes' if payload['authenticated'] else 'no'}{RESET}"
    )
    if user.get("email"):
        print(f"{DIM}User:{RESET} {CYAN}{user['email']}{RESET}")
    if user.get("name"):
        print(f"{DIM}Name:{RESET} {user['name']}")


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


def _plan_payload_from_file(args, import_source: str) -> dict:
    body = Path(args.from_path).read_text()
    parsed = json.loads(body) if args.from_path.endswith(".json") else None
    if isinstance(parsed, dict):
        title = args.title or parsed.get("title") or ""
        source_type = args.source_type or parsed.get("source_type") or ""
        target_type = args.target_type or parsed.get("target_type") or ""
        inferred_source, inferred_target = _infer_types_from_title(title)
        return {
            "title": title,
            "source_type": source_type or inferred_source or "",
            "target_type": target_type or inferred_target or "",
            "summary": args.summary or parsed.get("summary"),
            "status": args.status,
            "template_key": parsed.get("template_key"),
            "import_source": import_source,
            "org_id": args.org_id,
            "migration_id": args.migration_id,
            "plan_steps": parsed.get("plan_steps") or parsed.get("steps") or [],
            "external_links": parsed.get("external_links") or args.link or [],
        }
    if isinstance(parsed, list):
        title = args.title or "Imported plan"
        source_type = args.source_type or ""
        target_type = args.target_type or ""
        inferred_source, inferred_target = _infer_types_from_title(title)
        return {
            "title": title,
            "source_type": source_type or inferred_source or "",
            "target_type": target_type or inferred_target or "",
            "summary": args.summary,
            "status": args.status,
            "import_source": import_source,
            "org_id": args.org_id,
            "migration_id": args.migration_id,
            "plan_steps": parsed,
            "external_links": args.link or [],
        }
    title, steps = _parse_text_steps(body)
    chosen_title = args.title or title or "Imported plan"
    source_type = args.source_type or ""
    target_type = args.target_type or ""
    inferred_source, inferred_target = _infer_types_from_title(chosen_title)
    return {
        "title": chosen_title,
        "source_type": source_type or inferred_source or "",
        "target_type": target_type or inferred_target or "",
        "summary": args.summary,
        "status": args.status,
        "import_source": import_source,
        "org_id": args.org_id,
        "migration_id": args.migration_id,
        "plan_steps": steps,
        "external_links": args.link or [],
    }


def _validate_plan_payload(payload: dict) -> None:
    missing = [
        key
        for key in ["title", "source_type", "target_type"]
        if not str(payload.get(key) or "").strip()
    ]
    if missing:
        raise SystemExit(
            f"Missing required plan fields: {', '.join(missing)}. "
            "Provide them directly or include them in the imported file."
        )


def cmd_plan_templates(args):
    with make_client(args) as client:
        res = client.get("/api/plans/templates")
        res.raise_for_status()
        templates = res.json()
        template_name = getattr(args, "template_name", None) or getattr(
            args, "name", None
        )
        if template_name:
            match = next(
                (item for item in templates if item.get("key") == template_name), None
            )
            if not match:
                raise SystemExit(f"Template not found: {template_name}")
            if args.json:
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
        if args.json:
            print_output(templates, True)
            return
        if getattr(args, "verbose", False):
            for template in templates:
                print(f"{CYAN}{template['key']}{RESET}")
                if template.get("title"):
                    print(f"  {template['title']}")
                if template.get("summary"):
                    print(f"  {template['summary']}")
            return
        for template in templates:
            print(template["key"])


def cmd_plan_create(args):
    if args.from_template:
        payload = {
            "template_key": args.from_template,
            "title": args.title,
            "summary": args.summary,
            "org_id": args.org_id,
            "migration_id": args.migration_id,
            "import_source": "template",
        }
        with make_client(args) as client:
            res = client.post("/api/plans/from-template", json=payload)
            res.raise_for_status()
            print_output(res.json(), args.json)
        return

    if args.from_path:
        payload = _plan_payload_from_file(
            args,
            "claude" if args.import_mode == "claude" else "file",
        )
    else:
        payload = {
            "title": args.title,
            "source_type": args.source_type,
            "target_type": args.target_type,
            "summary": args.summary,
            "status": args.status,
            "import_source": "manual",
            "org_id": args.org_id,
            "migration_id": args.migration_id,
            "plan_steps": _read_json_file(args.steps_file),
            "external_links": args.link or [],
        }
    _validate_plan_payload(payload)
    with make_client(args) as client:
        res = client.post("/api/plans", json=payload)
        res.raise_for_status()
        print_output(res.json(), args.json)


def cmd_plan_list(args):
    params = {}
    if args.org_id:
        params["org_id"] = args.org_id
    with make_client(args) as client:
        res = client.get("/api/plans", params=params)
        res.raise_for_status()
        print_output(res.json(), args.json)


def cmd_plan_view(args):
    with make_client(args) as client:
        res = client.get(f"/api/plans/{args.plan_id}")
        res.raise_for_status()
        print_output(res.json(), args.json)


def cmd_plan_update(args):
    payload = {}
    for key in [
        "title",
        "source_type",
        "target_type",
        "summary",
        "status",
        "migration_id",
    ]:
        value = getattr(args, key, None)
        if value is not None:
            payload[key] = value
    if args.steps_file:
        payload["plan_steps"] = _read_json_file(args.steps_file)
    if args.link is not None:
        payload["external_links"] = args.link
    with make_client(args) as client:
        res = client.patch(f"/api/plans/{args.plan_id}", json=payload)
        res.raise_for_status()
        print_output(res.json(), args.json)


def cmd_plan_task(args):
    payload = {
        "title": args.title,
        "description": args.description,
        "status": args.status,
        "owner": args.owner,
        "notes": args.notes,
        "linear_issue_id": args.linear_issue_id,
    }
    with make_client(args) as client:
        res = client.post(f"/api/plans/{args.plan_id}/tasks", json=payload)
        res.raise_for_status()
        print_output(res.json(), args.json)


def cmd_edit_task(args):
    payload = {}
    for key in ["title", "description", "status", "owner", "notes", "linear_issue_id"]:
        value = getattr(args, key, None)
        if value is not None:
            payload[key] = value
    with make_client(args) as client:
        res = client.patch(
            f"/api/plans/{args.plan_id}/tasks/{args.task_id}",
            json=payload,
        )
        res.raise_for_status()
        print_output(res.json(), args.json)


def cmd_outcome_view(args):
    with make_client(args) as client:
        res = client.get(f"/api/plans/{args.plan_id}/outcome")
        res.raise_for_status()
        print_output(res.json(), args.json)


def cmd_outcome_save(args):
    payload = {
        "status": args.status,
        "summary": args.summary,
        "notes": args.notes,
        "actual_hours": args.actual_hours,
        "actual_cost": args.actual_cost,
    }
    with make_client(args) as client:
        res = client.post(f"/api/plans/{args.plan_id}/outcome", json=payload)
        res.raise_for_status()
        print_output(res.json(), args.json)


def _add_create_args(create):
    create.add_argument("--title")
    create.add_argument("--source-type")
    create.add_argument("--target-type")
    create.add_argument("--summary")
    create.add_argument("--status", default="draft")
    create.add_argument("--org-id")
    create.add_argument("--migration-id")
    create.add_argument("--steps-file")
    create.add_argument("--link", action="append")
    create.add_argument("--from-template")
    mode = create.add_mutually_exclusive_group()
    mode.add_argument("--from-file", dest="from_path")
    mode.add_argument("--from-claude", dest="from_path")


def _add_task_create_args(parser):
    parser.add_argument("plan_id")
    parser.add_argument("--title", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--status", default="todo")
    parser.add_argument("--owner")
    parser.add_argument("--notes")
    parser.add_argument("--linear-issue-id")
    parser.set_defaults(func=cmd_plan_task)


def _add_task_update_args(parser):
    parser.add_argument("plan_id")
    parser.add_argument("task_id")
    parser.add_argument("--title")
    parser.add_argument("--description")
    parser.add_argument("--status")
    parser.add_argument("--owner")
    parser.add_argument("--notes")
    parser.add_argument("--linear-issue-id")
    parser.set_defaults(func=cmd_edit_task)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="keshro")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--api-url", default=load_auth().get("api_url", DEFAULT_API_URL)
    )
    parser.add_argument("--token")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)

    login = auth_sub.add_parser("login")
    login.add_argument("--email")
    login.add_argument("--password")
    login.add_argument("--token")
    login.set_defaults(func=cmd_auth_login)

    logout = auth_sub.add_parser("logout")
    logout.set_defaults(func=cmd_auth_logout)

    login_alias = sub.add_parser("login")
    login_alias.add_argument("--email")
    login_alias.add_argument("--password")
    login_alias.add_argument("--token")
    login_alias.set_defaults(func=cmd_auth_login)

    logout_alias = sub.add_parser("logout")
    logout_alias.set_defaults(func=cmd_auth_logout)

    config_cmd = sub.add_parser("config")
    config_cmd.set_defaults(func=cmd_config)

    templates_alias = sub.add_parser("templates")
    templates_alias.add_argument("template_name", nargs="?")
    templates_alias.add_argument("-n", "--name")
    templates_alias.add_argument("-v", "--verbose", action="store_true")
    templates_alias.set_defaults(func=cmd_plan_templates)

    plan = sub.add_parser("plan")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True)

    templates = plan_sub.add_parser("templates")
    templates.add_argument("template_name", nargs="?")
    templates.add_argument("-n", "--name")
    templates.add_argument("-v", "--verbose", action="store_true")
    templates.set_defaults(func=cmd_plan_templates)

    create = plan_sub.add_parser("create")
    _add_create_args(create)
    create.set_defaults(
        func=cmd_plan_create,
        import_mode="file",
    )

    listing = plan_sub.add_parser("list")
    listing.add_argument("--org-id")
    listing.set_defaults(func=cmd_plan_list)

    view = plan_sub.add_parser("view")
    view.add_argument("plan_id")
    view.set_defaults(func=cmd_plan_view)

    update = plan_sub.add_parser("update")
    update.add_argument("plan_id")
    update.add_argument("--title")
    update.add_argument("--source-type")
    update.add_argument("--target-type")
    update.add_argument("--summary")
    update.add_argument("--status")
    update.add_argument("--migration-id")
    update.add_argument("--steps-file")
    update.add_argument("--link", action="append")
    update.set_defaults(func=cmd_plan_update)

    task_add = plan_sub.add_parser("task-add")
    _add_task_create_args(task_add)

    task_update = plan_sub.add_parser("task-update")
    _add_task_update_args(task_update)

    legacy_task_add = sub.add_parser("plan_task")
    _add_task_create_args(legacy_task_add)

    legacy_task_update = sub.add_parser("edit_task")
    _add_task_update_args(legacy_task_update)

    outcome = sub.add_parser("outcome")
    outcome_sub = outcome.add_subparsers(dest="outcome_command", required=True)

    outcome_view = outcome_sub.add_parser("view")
    outcome_view.add_argument("plan_id")
    outcome_view.set_defaults(func=cmd_outcome_view)

    outcome_save = outcome_sub.add_parser("save")
    outcome_save.add_argument("plan_id")
    outcome_save.add_argument("--status", required=True)
    outcome_save.add_argument("--summary")
    outcome_save.add_argument("--notes")
    outcome_save.add_argument("--actual-hours", type=int)
    outcome_save.add_argument("--actual-cost", type=int)
    outcome_save.set_defaults(func=cmd_outcome_save)

    return parser


def main(argv: list[str] | None = None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__version__)
        return
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "from_path", None):
        args.import_mode = "claude" if "--from-claude" in argv else "file"
    args.func(args)


if __name__ == "__main__":
    main()
