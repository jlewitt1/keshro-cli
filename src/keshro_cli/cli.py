import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .auth import cmd_auth_login, cmd_auth_logout, cmd_auth_whoami
from .client import make_client, print_output
from .config import DEFAULT_API_URL, load_auth


def _read_steps_file(path: str | None):
    if not path:
        return []
    return json.loads(Path(path).read_text())


def cmd_plan_create(args):
    payload = {
        "title": args.title,
        "source_type": args.source_type,
        "target_type": args.target_type,
        "summary": args.summary,
        "status": args.status,
        "org_id": args.org_id,
        "migration_id": args.migration_id,
        "plan_steps": _read_steps_file(args.steps_file),
        "external_links": args.link or [],
    }
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
    for key in ["title", "source_type", "target_type", "summary", "status", "migration_id"]:
        value = getattr(args, key, None)
        if value is not None:
            payload[key] = value
    if args.steps_file:
        payload["plan_steps"] = _read_steps_file(args.steps_file)
    if args.link is not None:
        payload["external_links"] = args.link
    with make_client(args) as client:
        res = client.patch(f"/api/plans/{args.plan_id}", json=payload)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="keshro")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--api-url", default=load_auth().get("api_url", DEFAULT_API_URL))
    parser.add_argument("--token")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)

    login = auth_sub.add_parser("login")
    login.add_argument("--email", required=True)
    login.add_argument("--password", required=True)
    login.set_defaults(func=cmd_auth_login)

    whoami = auth_sub.add_parser("whoami")
    whoami.set_defaults(func=cmd_auth_whoami)

    logout = auth_sub.add_parser("logout")
    logout.set_defaults(func=cmd_auth_logout)

    plan = sub.add_parser("plan")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True)

    create = plan_sub.add_parser("create")
    create.add_argument("--title", required=True)
    create.add_argument("--source-type", required=True)
    create.add_argument("--target-type", required=True)
    create.add_argument("--summary")
    create.add_argument("--status", default="draft")
    create.add_argument("--org-id")
    create.add_argument("--migration-id")
    create.add_argument("--steps-file")
    create.add_argument("--link", action="append")
    create.set_defaults(func=cmd_plan_create)

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
    args.func(args)


if __name__ == "__main__":
    main()
