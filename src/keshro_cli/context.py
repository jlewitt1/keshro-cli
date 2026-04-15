"""Repo/context discovery and plan/migration resolution helpers.

Extracted from cli.py to keep the top-level CLI module focused on command
definitions and their immediate orchestration logic.
"""

import os
import subprocess
import sys
from pathlib import Path

import httpx
import typer

from ._state import CYAN, DIM, GREEN, RED, RESET, YELLOW, _clean, _state
from .client import get_default_org_id, make_client, print_output
from .config import DEFAULT_API_URL, load_auth, update_auth


# ---------------------------------------------------------------------------
# URL helpers (needed by _execution_dashboard_url)
# ---------------------------------------------------------------------------


def _app_url_from_api_url(api_url: str) -> str:
    resolved = _clean(api_url).rstrip("/")
    if not resolved:
        return "https://keshro.com"
    if "localhost" in resolved or "127.0.0.1" in resolved:
        return resolved.replace("://api.", "://").replace(":8000", ":3000")
    if "api." in resolved:
        return resolved.replace("://api.", "://", 1)
    return resolved


def _current_app_url() -> str:
    auth = load_auth()
    api_url = _clean(_state.api_url) or _clean(auth.get("api_url")) or DEFAULT_API_URL
    return _app_url_from_api_url(api_url)


# ---------------------------------------------------------------------------
# Repo discovery
# ---------------------------------------------------------------------------


def _discover_repo_root(work_dir: str | None = None) -> Path | None:
    candidate = (
        _clean(work_dir) or _clean(load_auth().get("default_work_dir")) or os.getcwd()
    )
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=candidate,
            capture_output=True,
            text=True,
            check=True,
        )
        resolved = _clean(result.stdout)
        return Path(resolved) if resolved else None
    except Exception:
        return None


def _discover_git_remote_url(repo_root: Path | None) -> str | None:
    if repo_root is None:
        return None
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return None
    return _clean(result.stdout) or None


def _resolve_repo_linked_plan(
    work_dir: str | None = None,
) -> tuple[str | None, str | None]:
    repo_root = _discover_repo_root(work_dir)
    if repo_root is None:
        return None, None
    git_remote_url = _discover_git_remote_url(repo_root)
    try:
        with make_client(_state.api_url, _state.token) as client:
            res = client.get(
                "/v1/plans/repo-link/resolve",
                params={
                    "repo_root": str(repo_root),
                    "git_remote_url": git_remote_url,
                },
            )
            res.raise_for_status()
            body = res.json() or {}
    except Exception:
        return None, None

    plan_id = _clean(body.get("plan_id"))
    if not plan_id:
        return None, None

    plan_title = ""
    try:
        with make_client(_state.api_url, _state.token) as client:
            res = client.get(f"/v1/plans/{plan_id}")
            if res.status_code == 404:
                return None, None
            res.raise_for_status()
            plan = res.json() or {}
            plan_title = _clean(plan.get("title"))
            # If the plan is migration-scoped but the migration is soft-deleted,
            # treat the link as stale and ignore it.
            linked_migration_id = _clean(plan.get("migration_id"))
            if linked_migration_id and not _migration_exists(linked_migration_id):
                return None, None
    except Exception:
        pass
    return plan_id, plan_title or plan_id


def _repo_link_points_at_deleted_migration(work_dir: str | None = None) -> bool:
    """Return True if this repo has a server-side link to a plan whose migration was soft-deleted."""
    repo_root = _discover_repo_root(work_dir)
    if repo_root is None:
        return False
    git_remote_url = _discover_git_remote_url(repo_root)
    try:
        with make_client(_state.api_url, _state.token) as client:
            res = client.get(
                "/v1/plans/repo-link/resolve",
                params={
                    "repo_root": str(repo_root),
                    "git_remote_url": git_remote_url,
                },
            )
            res.raise_for_status()
            body = res.json() or {}
            plan_id = _clean(body.get("plan_id"))
            if not plan_id:
                return False
            plan_res = client.get(f"/v1/plans/{plan_id}")
            if plan_res.status_code != 200:
                return False
            plan = plan_res.json() or {}
    except Exception:
        return False
    linked_migration_id = _clean(plan.get("migration_id"))
    if not linked_migration_id:
        return False
    return not _migration_exists(linked_migration_id)


def _link_current_repo_to_plan(
    plan_id: str,
    *,
    plan_title: str | None = None,
    work_dir: str | None = None,
) -> bool:
    repo_root = _discover_repo_root(work_dir)
    if repo_root is None:
        return False
    try:
        with make_client(_state.api_url, _state.token) as client:
            res = client.put(
                f"/v1/plans/{plan_id}/repo-link",
                json={
                    "repo_root": str(repo_root),
                    "git_remote_url": _discover_git_remote_url(repo_root),
                    "repo_name": repo_root.name,
                },
            )
            res.raise_for_status()
    except Exception:
        return False

    if plan_title:
        update_auth({"default_plan_id": plan_id, "default_plan_title": plan_title})
    return True


# ---------------------------------------------------------------------------
# Context resolution
# ---------------------------------------------------------------------------


def _current_org_id(org_id: str | None = None) -> str | None:
    resolved = _clean(get_default_org_id(org_id))
    return resolved or None


def _current_plan_id(
    plan_id: str | None = None, work_dir: str | None = None
) -> str | None:
    explicit = _clean(plan_id)
    if explicit:
        resolved_id, _ = _resolve_plan_or_migration_context(explicit)
        return resolved_id
    env_plan_id = _clean(os.environ.get("KESHRO_ACTIVE_PLAN_ID"))
    if env_plan_id:
        return env_plan_id
    repo_plan_id, repo_plan_title = _resolve_repo_linked_plan(work_dir)
    if repo_plan_id:
        update_auth(
            {"default_plan_id": repo_plan_id, "default_plan_title": repo_plan_title}
        )
        return repo_plan_id
    auth = load_auth()
    cached_plan_id = _clean(auth.get("default_plan_id"))
    if cached_plan_id:
        return cached_plan_id
    return None


def _current_context_label() -> str | None:
    auth = load_auth()
    return _clean(auth.get("default_org_name") or auth.get("default_org_id")) or None


def _resolve_creation_scope(
    cli_org_id: str | None,
    cli_org_name: str | None = None,
    force_personal: bool = False,
) -> tuple[str | None, str]:
    """Pick the org_id under which a new project/migration will be created.

    Resolution priority:
    1. `--personal` flag -> personal scope, ignore everything else.
    2. `--org-id` flag -> use it directly (cheapest, no API roundtrip).
    3. `--org NAME` flag -> resolve name to id via /v1/orgs.
    4. Saved default from auth.json -> last fallback.

    Returns (org_id_or_None, human_readable_label). The label powers the scope
    banner so the user always sees, before any work happens, exactly where
    this project will land, same level of clarity as the web UI's org switcher.
    """
    if force_personal:
        return None, "personal"
    explicit_id = _clean(cli_org_id)
    if explicit_id:
        auth = load_auth()
        # If the explicit id matches the saved default, reuse the cached name
        # so we don't have to round-trip the API just for a banner.
        if explicit_id == _clean(auth.get("default_org_id")):
            label = _clean(auth.get("default_org_name")) or explicit_id
        else:
            label = explicit_id
        return explicit_id, label
    explicit_name = _clean(cli_org_name)
    if explicit_name:
        resolved_id, resolved_name = _resolve_org_context(None, explicit_name)
        return resolved_id, resolved_name or explicit_name
    auth = load_auth()
    saved_id = _clean(auth.get("default_org_id"))
    if saved_id:
        label = _clean(auth.get("default_org_name")) or saved_id
        return saved_id, label
    return None, "personal"


def _print_creation_scope_banner(scope_label: str) -> None:
    """Tell the user where this project/migration is being created. Mirrors
    the org switcher pill in the web UI so CLI users aren't guessing whether
    something landed in their personal scope or under an org."""
    if _state.json:
        return
    if scope_label == "personal":
        print(f"{DIM}Creating as:{RESET} {YELLOW}personal{RESET}")
    else:
        print(f"{DIM}Creating in org:{RESET} {YELLOW}{scope_label}{RESET}")


def _print_applied_context_banner(applied: dict | None) -> None:
    """Surface that the clarifier auto-applied saved team/cost context so the
    user knows exactly which questions were skipped and where to edit them.
    Without this the rollup-skip is silent and feels like Keshro 'forgot' to
    ask, undermining trust in the answers Keshro reaches downstream."""
    if _state.json or not applied:
        return
    parts: list[str] = []
    if applied.get("applied_team_context"):
        parts.append("team context")
    if applied.get("applied_cost_context"):
        parts.append("cost context")
    if not parts:
        return
    scope = applied.get("scope") or "user"
    label = _clean(applied.get("scope_label")) or (
        "your profile" if scope == "user" else "your org"
    )
    edit_cmd = "keshro user context" if scope == "user" else "keshro org context"
    print(
        f"{GREEN}✓{RESET} Using saved {' + '.join(parts)} from {YELLOW}{label}{RESET}. "
        f"{DIM}Edit:{RESET} {CYAN}{edit_cmd}{RESET}"
    )


def _current_plan_label(work_dir: str | None = None) -> str | None:
    repo_plan_id, repo_plan_title = _resolve_repo_linked_plan(work_dir)
    if repo_plan_id:
        return repo_plan_title
    auth = load_auth()
    return _clean(auth.get("default_plan_title") or auth.get("default_plan_id")) or None


def _require_plan_context(
    plan_id: str | None = None, work_dir: str | None = None
) -> str:
    resolved = _current_plan_id(plan_id, work_dir=work_dir)
    if resolved:
        return resolved
    raise SystemExit(
        "Execution context or migration ID required. Pass -p <id> or run `keshro config set --plan-id <id>` from the repo you want to link."
    )


# ---------------------------------------------------------------------------
# Plan/migration resolution
# ---------------------------------------------------------------------------


def _execution_context_arg(plan: dict | None = None, plan_id: str | None = None) -> str:
    migration_id = _clean((plan or {}).get("migration_id"))
    if migration_id:
        return migration_id
    return _clean(plan_id) or _clean((plan or {}).get("id")) or ""


def _execution_dashboard_url(
    plan: dict | None = None, plan_id: str | None = None
) -> str:
    migration_id = _clean((plan or {}).get("migration_id"))
    if migration_id:
        return f"{_current_app_url()}/migrations/{migration_id}"
    resolved_plan_id = _clean(plan_id) or _clean((plan or {}).get("id"))
    return f"{_current_app_url()}/plans/{resolved_plan_id}" if resolved_plan_id else ""


def _execution_context_label(plan: dict | None = None) -> str:
    return "migration" if _clean((plan or {}).get("migration_id")) else "project"


def _fetch_and_display_completion_audit(plan_id: str, plan: dict | None = None) -> None:
    """Fetch the completion audit from the API and print a summary."""
    try:
        with make_client(_state.api_url, _state.token) as client:
            res = client.get(f"/v1/agent/plans/{plan_id}/completion-audit")
            res.raise_for_status()
            audit = res.json()
    except Exception:
        return

    if not isinstance(audit, dict):
        return

    status = _clean(audit.get("status"))
    if not status or status == "pending":
        return

    confidence = audit.get("completion_confidence") or 0
    summary = _clean(audit.get("summary"))
    unresolved_risks = audit.get("unresolved_risks") or []
    unresolved_unknowns = audit.get("unresolved_unknowns") or []
    coverage_gaps = audit.get("coverage_gaps") or []

    print(f"\n{'─' * 40}")
    if status == "passed":
        print(f"{GREEN}Completion audit: PASSED ({confidence:.0f}% confidence){RESET}")
    else:
        print(f"{YELLOW}Completion audit: NEEDS ATTENTION ({confidence:.0f}% confidence){RESET}")

    if summary:
        print(f"{DIM}{summary}{RESET}")

    if unresolved_risks:
        print(f"\n{YELLOW}Unresolved risks ({len(unresolved_risks)}):{RESET}")
        for r in unresolved_risks[:5]:
            sev = _clean(r.get("severity")) or "medium"
            title = _clean(r.get("title")) or "Untitled"
            reason = _clean(r.get("reason")) or ""
            color = RED if sev in ("critical", "high") else YELLOW
            print(f"  {color}[{sev}]{RESET} {title}")
            if reason:
                print(f"    {DIM}{reason}{RESET}")

    if unresolved_unknowns:
        print(f"\n{YELLOW}Unresolved questions ({len(unresolved_unknowns)}):{RESET}")
        for u in unresolved_unknowns[:5]:
            question = _clean(u.get("question")) or "?"
            print(f"  - {question}")
            reason = _clean(u.get("reason")) or ""
            if reason:
                print(f"    {DIM}{reason}{RESET}")

    if coverage_gaps:
        print(f"\n{YELLOW}Coverage gaps ({len(coverage_gaps)}):{RESET}")
        for g in coverage_gaps[:5]:
            title = _clean(g.get("task_title")) or "Untitled"
            issue = _clean(g.get("issue")) or ""
            print(f"  - {title}: {issue}")

    dashboard = _execution_dashboard_url(plan=plan, plan_id=plan_id)
    if dashboard:
        print(f"\n{DIM}Full audit:{RESET} {dashboard}?tab=audit")


def _set_default_plan_after_create(plan: dict, *, announce: bool = True) -> None:
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
    _link_current_repo_to_plan(plan_id, plan_title=plan_title)
    if announce:
        print(f"Saved default execution context: {plan_title}")


def _resolve_plan_context(plan_id: str | None) -> tuple[str | None, str | None]:
    explicit_id = _clean(plan_id)
    if not explicit_id:
        return None, None
    with make_client(_state.api_url, _state.token) as client:
        res = client.get(f"/v1/plans/{explicit_id}")
        res.raise_for_status()
        plan = res.json()
    return explicit_id, _clean(plan.get("title")) or explicit_id


def _load_plan_context_details(plan_id: str | None) -> dict[str, str | None]:
    resolved_plan_id = _clean(plan_id)
    if not resolved_plan_id:
        return {
            "plan_id": None,
            "plan_title": None,
            "migration_id": None,
            "kind": None,
        }
    try:
        with make_client(_state.api_url, _state.token) as client:
            res = client.get(f"/v1/plans/{resolved_plan_id}")
            if res.status_code == 404:
                return {
                    "plan_id": None,
                    "plan_title": None,
                    "migration_id": None,
                    "kind": None,
                }
            res.raise_for_status()
            plan = res.json()
    except Exception:
        return {
            "plan_id": resolved_plan_id,
            "plan_title": None,
            "migration_id": None,
            "kind": "plan",
        }
    migration_id = _clean(plan.get("migration_id"))
    # If the plan claims a migration, verify it still exists (migrations are soft-deleted,
    # so the plan's migration_id can point at a removed migration).
    if migration_id and not _migration_exists(migration_id):
        migration_id = ""
    return {
        "plan_id": resolved_plan_id,
        "plan_title": _clean(plan.get("title")) or resolved_plan_id,
        "migration_id": migration_id or None,
        "kind": "migration" if migration_id else "plan",
    }


def _plan_exists(plan_id: str) -> bool:
    """Return True if the plan exists server-side. 404 returns False, all other errors return True (don't spuriously clear)."""
    pid = _clean(plan_id)
    if not pid:
        return False
    try:
        with make_client(_state.api_url, _state.token) as client:
            res = client.get(f"/v1/plans/{pid}")
            return res.status_code != 404
    except Exception:
        return True


def _migration_exists(migration_id: str) -> bool:
    """Return True if the migration exists server-side. 404 returns False."""
    mid = _clean(migration_id)
    if not mid:
        return False
    try:
        with make_client(_state.api_url, _state.token) as client:
            res = client.get(f"/v1/migrations/{mid}")
            return res.status_code != 404
    except Exception:
        return True


def _resolve_plan_or_migration_context(
    value: str | None,
) -> tuple[str | None, str | None]:
    explicit_id = _clean(value)
    if not explicit_id:
        return None, None
    with make_client(_state.api_url, _state.token) as client:
        try:
            res = client.get(f"/v1/plans/{explicit_id}")
            res.raise_for_status()
            plan = res.json()
            return explicit_id, _clean(plan.get("title")) or explicit_id
        except Exception:
            pass
        try:
            res = client.get(f"/v1/migrations/{explicit_id}/plan")
            res.raise_for_status()
            plan = res.json()
            plan_id = _clean(plan.get("id")) or explicit_id
            return plan_id, _clean(plan.get("title")) or plan_id
        except Exception:
            pass
    raise SystemExit(
        f"Could not resolve '{explicit_id}' to an execution context or migration-linked context."
    )


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
        res = client.get("/v1/orgs")
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
