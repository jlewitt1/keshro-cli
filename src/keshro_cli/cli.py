import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Optional

import click
import httpx
import typer

from . import __version__
from ._state import (
    CYAN, DIM, GREEN, RED, RESET, YELLOW,
    TaskStatus, _clean, _coding_agent_name, _current_coding_agent_preference,
    _default_agent_preference, _enable_line_buffered_output, _inside_coding_agent,
    _Spinner, _sanitize_json_payload, _State, _state, _stdout_is_tty,
)
from .auth import cmd_auth_login, cmd_auth_logout
from .client import get_default_org_id, make_async_client, make_client, print_output
from .config import DEFAULT_API_URL, load_auth, update_auth
from .context import (
    _app_url_from_api_url, _current_app_url, _current_context_label,
    _current_org_id, _current_plan_id, _current_plan_label,
    _discover_git_remote_url, _discover_repo_root,
    _execution_context_arg, _execution_context_label, _execution_dashboard_url,
    _fetch_and_display_completion_audit, _link_current_repo_to_plan,
    _load_plan_context_details, _migration_exists, _plan_exists,
    _print_applied_context_banner, _print_creation_scope_banner,
    _repo_link_points_at_deleted_migration, _require_plan_context,
    _resolve_creation_scope, _resolve_org_context,
    _resolve_plan_context, _resolve_plan_or_migration_context,
    _resolve_repo_linked_plan, _set_default_plan_after_create,
)
from .execution_policies import (
    DEFAULT_PR_POLICY,
    DEFAULT_WORKTREE_POLICY,
    normalize_pr_policy,
    normalize_worktree_policy,
    resolve_pr_policy,
    resolve_worktree_policy,
    should_use_isolated_worktree,
)
from .formatting import (
    _clear_saved_context, _COST_CONTEXT_LABELS, _elapsed_runtime_from_events,
    _event_status, _extract_error_detail, _extract_source_titles,
    _format_agent_phase_label, _format_duration_compact,
    _format_plan_timestamp, _format_task_note_for_terminal, _format_verbose_timestamp,
    _mark_agent_session_stopped_async, _parse_timestamp, _plan_analysis,
    _plan_execution_snapshot, _print_http_error, _print_migration_detail,
    _print_migration_summary, _print_plan_analysis, _print_plan_detail,
    _print_plan_enrichment, _print_plan_summary, _print_request_error,
    _print_table, _print_task_detail, _print_task_feedback_events,
    _print_task_update_summary, _print_wrapped_block, _show_saved_context,
    _summarize_agent_session_line, _summarize_plan_progress,
    _TEAM_CONTEXT_LABELS, _truncate_text,
)
from .git_ops import (
    _classify_file_edit_from_diff, _cleanup_worktree, _collect_git_changed_files,
    _create_codex_worktree, _create_pr_via_gh, _create_pr_via_github_api,
    _create_task_pr, _find_existing_pr, _git_changed_files, _git_stdout,
    _mark_task_status_async, _parse_git_status_changed_files, _parse_github_remote,
    _post_agent_note_async, _resolve_default_branch, _summarize_file_edits,
)
from .integrations import (
    KESHRO_SLASH_COMMAND, _install_agent_integrations,
    _install_claude_integration, _install_codex_integration,
    _maybe_refresh_claude, _maybe_refresh_codex,
    _should_refresh_agent_integrations,
)
from .tasks import (
    _add_task_artifact, _append_task_note, _build_appended_task_notes,
    _collect_task_outcome, _collect_task_outcome_async,
    _collect_task_runtime_context, _collect_task_runtime_context_for,
    _do_task_block, _do_task_done, _do_task_reopen, _do_task_start,
    _do_task_unblock, _do_task_update,
    _ensure_completion_note_covers_requirements, _extract_session_id,
    _find_task, _get_plan_or_exit, _git_branch_for_runtime,
    _infer_agent_client, _infer_model_name, _infer_task_done_context,
    _parse_dependency_ids, _parse_text_steps, _plan_payload_from_file,
    _post_agent_task_event, _python_runtime_details, _read_json_file,
    _task_completion_requirements, _validate_plan_payload,
)


_codex_merge_lock = asyncio.Lock()
_LIVE_CONFLICT_POLL_SECONDS = 3
_LIVE_CONFLICT_WAIT_TIMEOUT_SECONDS = 15 * 60
_LIVE_CONFLICT_MAX_RETRIES = 3
_ANSWERS_FILE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_ANSWERS_FILE_MAX_COUNT = 10


def _resolve_task_target(
    task_id: str,
    plan_id_option: str | None,
) -> tuple[str | None, str]:
    return _require_plan_context(plan_id_option), task_id


# ---------------------------------------------------------------------------
# Typer apps
# ---------------------------------------------------------------------------

app = typer.Typer(add_completion=False, no_args_is_help=False)
auth_app = typer.Typer(help="Authentication")
plan_app = typer.Typer(help="Advanced plan internals")
task_app = typer.Typer(help="Task management")
migration_app = typer.Typer(help="Migration project management")
config_app = typer.Typer(help="Configuration", invoke_without_command=True)
plan_task_app = typer.Typer(help="Plan task management")
org_app = typer.Typer(help="Organization context (saved team / cost defaults)")
user_app = typer.Typer(help="Personal context (saved team / cost defaults)")

app.add_typer(plan_app, name="plan", hidden=True)
app.add_typer(task_app, name="task")
app.add_typer(migration_app, name="migration", hidden=True)
app.add_typer(config_app, name="config")
app.add_typer(org_app, name="org")
app.add_typer(user_app, name="user")
plan_app.add_typer(plan_task_app, name="task")


# ---------------------------------------------------------------------------
# Helpers (logic unchanged from argparse version)
# ---------------------------------------------------------------------------


def _read_context_file(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return Path(path).read_text().strip() or None
    except OSError as exc:
        raise typer.BadParameter(f"Could not read context file {path}: {exc}") from exc


def _interactive_cli_prompts_allowed() -> bool:
    return not _state.json and sys.stdout.isatty() and not _inside_coding_agent()


def _interactive_cli_prompts_allowed() -> bool:
    return not _state.json and sys.stdout.isatty() and not _inside_coding_agent()


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


def _load_answer_file_bundle(
    path: str | None,
) -> tuple[dict[str, str], list[dict], str, dict[str, str]]:
    raw_path = _clean(path)
    if not raw_path:
        return ({}, [], "", {})
    try:
        payload = json.loads(Path(raw_path).read_text())
    except OSError as exc:
        raise SystemExit(f"Could not read --answers-file {raw_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in --answers-file {raw_path}: {exc}") from exc
    questions = []
    enrichment_context = ""
    initial_answers: dict[str, str] = {}
    if isinstance(payload, dict) and isinstance(payload.get("questions"), list):
        questions = [q for q in payload.get("questions") or [] if isinstance(q, dict)]
    if isinstance(payload, dict):
        enrichment_context = _clean(payload.get("enrichment_context"))
    if isinstance(payload, dict) and isinstance(payload.get("initial_answers"), dict):
        for key, value in payload["initial_answers"].items():
            answer_key = _clean(str(key))
            answer_value = _clean(str(value))
            if answer_key and answer_value:
                initial_answers[answer_key] = answer_value
    if isinstance(payload, dict) and isinstance(payload.get("answers"), dict):
        source = payload["answers"]
    elif isinstance(payload, dict):
        source = payload
    else:
        raise SystemExit(
            f"Invalid --answers-file {raw_path}: expected a JSON object of question_id -> answer."
        )
    parsed: dict[str, str] = {}
    for key, value in source.items():
        question_id = _clean(str(key))
        answer_value = _clean(str(value))
        if question_id and answer_value:
            parsed[question_id] = answer_value
    parsed = _normalize_answers_for_questions(questions, parsed)
    # Note: temp answer files (keshro-answers-*.json) are intentionally NOT
    # deleted here. The agent flow may need to re-read and amend the file when
    # validation fails for missing answers (e.g. an unfilled question). The OS
    # cleans /tmp on its own schedule.
    return parsed, questions, enrichment_context, initial_answers


def _load_answer_file(path: str | None) -> dict[str, str]:
    answers, _questions, _enrichment_context, _initial_answers = _load_answer_file_bundle(path)
    return answers


def _write_agent_answers_file(
    *,
    heading: str,
    questions: list[dict],
    suggested_answers: dict[str, str],
    enrichment_context: str = "",
    initial_answers: dict[str, str] | None = None,
) -> str:
    """Write the clarifier handoff payload to a user-level file. The agent
    Reads / Edits this file to capture the user's answers, then re-invokes
    the CLI with `--answers-file <path>`.

    Store these under `~/.keshro/answers/` so they never pollute a user's
    working tree or git status, while still remaining stable across retries."""
    payload = {
        "heading": heading,
        "answers": suggested_answers,
        "questions": questions,
        "enrichment_context": enrichment_context,
        "initial_answers": initial_answers or {},
    }
    target_dir = Path.home() / ".keshro" / "answers"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"keshro-answers-{uuid.uuid4().hex[:8]}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n")
    _prune_agent_answers_files(target_dir, keep_path=path)
    return str(path)


def _prune_agent_answers_files(
    target_dir: Path,
    *,
    keep_path: Path | None = None,
) -> None:
    try:
        candidates = [
            item
            for item in target_dir.glob("keshro-answers-*.json")
            if item.is_file()
        ]
    except OSError:
        return

    now = time.time()
    protected = str(keep_path.resolve()) if keep_path else ""

    for candidate in list(candidates):
        try:
            resolved = str(candidate.resolve())
            stat = candidate.stat()
        except OSError:
            continue
        if protected and resolved == protected:
            continue
        if now - stat.st_mtime <= _ANSWERS_FILE_RETENTION_SECONDS:
            continue
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass

    try:
        remaining = [
            item
            for item in target_dir.glob("keshro-answers-*.json")
            if item.is_file()
        ]
    except OSError:
        return

    sortable: list[tuple[float, Path]] = []
    for candidate in remaining:
        try:
            sortable.append((candidate.stat().st_mtime, candidate))
        except OSError:
            continue
    sortable.sort(reverse=True)

    for _, candidate in sortable[_ANSWERS_FILE_MAX_COUNT:]:
        try:
            resolved = str(candidate.resolve())
        except OSError:
            resolved = ""
        if protected and resolved == protected:
            continue
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass


def _missing_question_ids(questions: list[dict], answers: dict[str, str]) -> list[str]:
    normalized_answers = _normalize_answers_for_questions(questions, answers)
    missing: list[str] = []
    for index, question in enumerate(questions, 1):
        question_id = _clean(question.get("id")) or f"q{index}"
        if not _clean(normalized_answers.get(question_id)):
            missing.append(question_id)
    return missing


def _normalize_answers_for_questions(
    questions: list[dict], answers: dict[str, str]
) -> dict[str, str]:
    if not questions or not answers:
        return dict(answers)

    normalized = {
        _clean(str(key)): _clean(str(value))
        for key, value in answers.items()
        if _clean(str(key)) and _clean(str(value))
    }
    if not normalized:
        return {}

    alias_to_question: dict[str, str] = {}
    duplicate_aliases: set[str] = set()

    def _register_alias(alias: str, question_id: str) -> None:
        alias = _clean(alias)
        if not alias or alias == question_id:
            return
        existing = alias_to_question.get(alias)
        if existing and existing != question_id:
            duplicate_aliases.add(alias)
            alias_to_question.pop(alias, None)
            return
        if alias not in duplicate_aliases:
            alias_to_question[alias] = question_id

    for index, question in enumerate(questions, 1):
        question_id = _clean(question.get("id")) or f"q{index}"
        if not question_id:
            continue
        field_target = _clean(question.get("field_target"))
        _register_alias(field_target, question_id)
        if question_id.startswith("required_"):
            _register_alias(question_id.removeprefix("required_"), question_id)

    remapped = dict(normalized)
    for answer_key, answer_value in normalized.items():
        if answer_key in remapped and answer_key in alias_to_question:
            target_question = alias_to_question[answer_key]
            remapped.setdefault(target_question, answer_value)
    return remapped


def _delete_answers_file(path: str | None) -> None:
    raw_path = _clean(path)
    if not raw_path:
        return
    try:
        Path(raw_path).unlink(missing_ok=True)
    except Exception:
        pass


def _exit_for_agent_clarifier_feedback(
    *,
    heading: str,
    questions: list[dict],
    suggested_answers: dict[str, str],
    rerun_command: str,
    enrichment_context: str = "",
    initial_answers: dict[str, str] | None = None,
) -> None:
    answers_file = _write_agent_answers_file(
        heading=heading,
        questions=questions,
        suggested_answers=suggested_answers,
        enrichment_context=enrichment_context,
        initial_answers=initial_answers,
    )
    rerun_command = f"{rerun_command} --answers-file {shlex.quote(answers_file)}"
    if _state.json:
        print_output(
            {
                "status": "needs_input",
                "heading": heading,
                "questions": questions,
                "suggested_answers": suggested_answers,
                "answers_file": answers_file,
                "rerun_command": rerun_command,
            },
            True,
        )
        raise typer.Exit(0)
    print(f"\n{YELLOW}{heading}{RESET}")
    print(
        f"{DIM}Ask the user these follow-up questions conversationally, update the generated answers file, then rerun the command.{RESET}"
    )
    for index, question in enumerate(questions, 1):
        question_id = _clean(question.get("id")) or f"q{index}"
        prompt_text = _clean(question.get("question")) or question_id
        why = _clean(question.get("why_this_matters"))
        print(f"\n  {CYAN}{index}.{RESET} {prompt_text}")
        print(f"     {DIM}id: {question_id}{RESET}")
        if why:
            print(f"     {DIM}{why}{RESET}")
        suggested = _clean(suggested_answers.get(question_id))
        options = list(question.get("answers") or [])
        input_mode = question.get("input_mode", "select")
        if options:
            for option_index, option in enumerate(options):
                title = _clean(option.get("answer_title")) or _clean(
                    option.get("value")
                )
                rec = _format_recommended_suffix(title, bool(option.get("recommended")))
                marker = (
                    " (suggested)"
                    if suggested and suggested == _clean(option.get("value"))
                    else ""
                )
                # Letter markers in parens — `(a)`, `(b)`, `(c)` — so option
                # labels are visually distinct from the parent question's
                # `1.` numeric label. Avoids the "1." question / "1." option
                # collision that made rendered output ambiguous in agent
                # transcripts, and the parens read more clearly than `a.`
                # next to a `1.` heading.
                letter = chr(ord("a") + option_index)
                print(f"     {DIM}({letter}) {title}{rec}{marker}{RESET}")
            if input_mode != "free_text":
                print(f"     {DIM}Or: type a custom answer{RESET}")
        if input_mode == "free_text":
            placeholder = _clean(question.get("placeholder"))
            if placeholder:
                print(f"     {DIM}{placeholder}{RESET}")
        elif not options and suggested:
            print(f"     {DIM}Suggested answer: {suggested}{RESET}")
    print(f"\n{DIM}Answers file:{RESET} {CYAN}{answers_file}{RESET}")
    print(f"{DIM}Then rerun:{RESET}")
    print(f"  {CYAN}{rerun_command}{RESET}")
    raise typer.Exit(0)


def _exit_for_agent_migration_confirmation(
    *,
    source_tech: str,
    target_tech: str,
    template_key: str | None,
    context: str,
    agent: str,
) -> None:
    migration_command = (
        f"keshro create --template {shlex.quote(template_key)} --context {shlex.quote(context)}"
        if template_key
        else f"keshro create -m --context {shlex.quote(context)}"
    )
    general_command = f"keshro create --context {shlex.quote(context)}"
    if agent != "auto":
        migration_command += f" -a {shlex.quote(agent)}"
        general_command += f" -a {shlex.quote(agent)}"
    if _state.json:
        print_output(
            {
                "status": "needs_input",
                "kind": "migration_confirmation",
                "question": f"This looks like a migration ({source_tech} -> {target_tech}). Should Keshro treat it as a migration or a general project?",
                "recommended": "migration",
                "options": [
                    {
                        "id": "migration",
                        "label": "Treat as migration",
                        "rerun_command": migration_command,
                    },
                    {
                        "id": "general",
                        "label": "Treat as a general project",
                        "rerun_command": general_command,
                    },
                ],
            },
            True,
        )
        raise typer.Exit(0)
    print(
        f"\n{YELLOW}This looks like a migration ({source_tech} -> {target_tech}). Keshro needs user confirmation before continuing.{RESET}"
    )
    print(
        f"{DIM}Ask the user whether to treat it as a migration or a general project, then rerun one of these commands.{RESET}"
    )
    print(f"\n  1. {CYAN}Treat as migration{RESET} {GREEN}(recommended){RESET}")
    print(f"     {DIM}{migration_command}{RESET}")
    print(f"\n  2. {CYAN}Treat as a general project{RESET}")
    print(f"     {DIM}{general_command}{RESET}")
    raise typer.Exit(0)


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


def _resolve_menu_choice(
    value: str,
    options: list[str],
    *,
    aliases: dict[str, str] | None = None,
) -> str:
    """Resolve a menu answer to a canonical option label when possible."""
    raw = value.strip()
    if not raw:
        return raw
    normalized_options = [
        str(option).strip() for option in options if str(option).strip()
    ]
    if not normalized_options:
        return raw
    if raw.isdigit():
        option_index = int(raw) - 1
        if 0 <= option_index < len(normalized_options):
            return normalized_options[option_index]
    lowered = raw.lower()
    if aliases and lowered in aliases:
        alias_target = aliases[lowered]
        for option in normalized_options:
            if option == alias_target:
                return option
    for option in normalized_options:
        option_lower = option.lower()
        if lowered == option_lower:
            return option
    for option in normalized_options:
        option_lower = option.lower()
        if lowered in option_lower or option_lower in lowered:
            return option
    return raw


def _format_preview_lines(
    value: str, *, width: int = 88, max_lines: int = 4
) -> tuple[list[str], bool]:
    """Wrap preview text for interactive prompts without breaking words."""
    preview = value.replace("\n", " ").strip()
    if not preview:
        return ([], False)
    chunks = [
        chunk.strip() for chunk in re.split(r"(?<=[.;!?])\s+", preview) if chunk.strip()
    ]
    wrapped: list[str] = []
    for chunk in chunks or [preview]:
        wrapped.extend(
            textwrap.wrap(
                chunk,
                width=width,
                break_long_words=False,
                break_on_hyphens=False,
            )
            or [chunk]
        )
    if len(wrapped) <= max_lines:
        return (wrapped, False)
    trimmed = wrapped[: max_lines - 1]
    remainder = " ".join(wrapped[max_lines - 1 :]).strip()
    trimmed.append(textwrap.shorten(remainder, width=width, placeholder="..."))
    return (trimmed, True)


def _format_full_value_lines(value: str, *, width: int = 88) -> list[str]:
    """Wrap a full value for terminal display without truncating it."""
    text = value.strip()
    if not text:
        return []
    lines: list[str] = []
    for raw_line in text.splitlines() or [text]:
        wrapped = textwrap.wrap(
            raw_line,
            width=width,
            break_long_words=False,
            break_on_hyphens=False,
        )
        lines.extend(wrapped or [""])
    return lines


def _format_recommended_suffix(title: str, recommended: bool) -> str:
    if not recommended:
        return ""
    if "recommended" in title.lower():
        return ""
    return f" {GREEN}(recommended){RESET}"


_EXPERIENCE_LEVEL_OPTIONS = ["Junior", "Mid-level", "Senior", "Mixed"]
_FAMILIARITY_OPTIONS = ["None", "Basic", "Proficient", "Expert"]

_COST_SIGNAL_FIELDS = [
    (
        "Planned dual-run / overlap period",
        "Helps estimate temporary vendor overlap and parallel-run costs.",
    ),
    (
        "Current platform monthly spend",
        "Used to model the cost of staying put and temporary overlap during migration.",
    ),
    (
        "Expected target platform monthly spend",
        "Used to model destination infra or service costs during rollout and steady state.",
    ),
    (
        "Backfill / data transfer volume",
        "Helps estimate data transfer, egress, and backfill-related migration costs.",
    ),
    (
        "Expected tooling / outside-services cost",
        "Use this for paid migration tooling, temporary licenses, or consulting support.",
    ),
]

_EXECUTION_SIGNAL_FIELDS = [
    (
        "What the migration actually means",
        "Clarify whether this is full replacement, orchestration moving elsewhere, staged coexistence, or a narrower workflow change.",
    ),
    (
        "Workloads / assets in scope",
        "How many jobs, workflows, services, tables, or other assets are actually in scope?",
    ),
    (
        "Current triggers / scheduling model",
        "What triggers the current workloads, and how often do they run?",
    ),
    (
        "Retry and failure-handling requirements",
        "What retry policies, alerts, or failure-handling behavior must be preserved?",
    ),
    (
        "Target execution model",
        "Will the target orchestrate the current runtime, replace it, or use a hybrid model?",
    ),
    (
        "Deployment / delivery model",
        "Will deployment happen through CI/CD, scripted uploads, manual UI changes, or some other path?",
    ),
    (
        "Infrastructure as code / Terraform setup",
        "Is Terraform or another IaC system already in place for the target environment and permissions model?",
    ),
    (
        "Cloud permissions / IAM setup",
        "How will permissions, service roles, and deployment access be managed in the target setup?",
    ),
    (
        "Target hosting / deployment environment",
        "Where will the target system run?",
    ),
    (
        "Cutover window / go-live target",
        "When can the migration go live, and how much downtime or disruption is acceptable?",
    ),
    (
        "Rollback strategy and triggers",
        "What would trigger rollback, and what is the fallback path?",
    ),
    (
        "Dependency / ordering constraints",
        "What dependencies or execution ordering requirements must be preserved?",
    ),
    (
        "Resource / runtime requirements",
        "What compute, memory, GPU, or environment requirements matter for migration design?",
    ),
]

_STANDARD_ROLE_SUGGESTIONS = [
    "Backend Engineer",
    "Frontend Engineer",
    "Full-Stack Engineer",
    "QA Engineer",
    "Senior Software Engineer",
    "Software Engineer",
    "Data Platform Engineer",
    "DevOps Engineer",
    "Platform Engineer",
]


def _build_path_discovery_prompt(template: dict, currency: str = "USD") -> str:
    source = _clean(template.get("source")) or "Source"
    target = _clean(template.get("target")) or "Target"
    raw_fields = list(template.get("fields") or [])

    def _field_label(field: dict) -> str:
        return _clean(field.get("label")) or _clean(field.get("id")) or "Detail"

    all_fields = [f for f in raw_fields if _field_label(f) != "Detail" or f.get("id")]
    required_fields = [f for f in all_fields if f.get("required")]

    tips = [
        _clean(tip) for tip in (template.get("tips") or []) if _clean(tip)
    ]
    required_outputs = [
        _clean(item) for item in (template.get("required_outputs") or []) if _clean(item)
    ]

    # Derive inspectFirst from required fields, with a source/target-aware fallback.
    if required_fields:
        inspect_first = [_field_label(f) for f in required_fields[:6]]
    else:
        inspect_first = [
            f"Current {source} runtime or platform version",
            f"Dependencies and integrations tied to {source} that must keep working",
            f"Production workloads on {source} that cannot regress",
            f"Current deployment shape and rollback boundary for {source}",
            f"The smallest representative workload you can safely validate on {target} first",
        ]

    # Derive discoverFromEnvironment from the full field list, with hints.
    if all_fields:
        discover_from_env = []
        for f in all_fields[:8]:
            label = _field_label(f)
            hint = _clean(f.get("hint"))
            discover_from_env.append(f"{label} — {hint}" if hint else label)
    else:
        discover_from_env = [
            f"The current {source} version actually in production",
            f"Dependencies, integrations, and surrounding systems tied to {source}",
            f"Real production workflows, traffic shape, and access patterns that depend on {source}",
            "Current deployment topology, operating constraints, and rollback boundary",
            f"Representative {source} workloads, artifacts, or data you can inspect before planning",
        ]

    # Reviewable checks from required_outputs.
    if required_outputs:
        reviewable_checks = [
            f"{output} — Confirm this is producible for the {source} -> {target} move."
            for output in required_outputs
        ]
    else:
        reviewable_checks = [
            f"List the {source} assets in scope — services, jobs, tables, workflows, repos, or configs that will actually move.",
            "Identify the top workflows that cannot regress — prefer concrete reports, endpoints, jobs, or user journeys over vague success criteria.",
            f"Draft one safe {source} -> {target} pilot path — validate one representative component before assuming the whole migration shape.",
        ]

    # Behavioral differences / whatCouldBreak from tips.
    what_could_break = tips if tips else [
        f"A core {source} capability may behave differently in {target} even when the migration looks structurally straightforward.",
        f"Cutover can fail if validation covers only configuration parity and not runtime behavior on {target}.",
        "Rollback plans are often too vague until ownership, timing, and triggers are explicit.",
        f"{source} workloads with hidden integrations or operational assumptions are the most common surprise source.",
    ]

    lines = [
        f"You are the migration discovery analyst for a {source} -> {target} migration.",
        "",
        "Your job is to gather the highest-signal migration facts before planning begins.",
        "Focus on facts that materially affect risk, effort, cutover complexity, validation, rollout shape, and target architecture.",
        "",
        "Operating rules:",
        "- Prioritize runtime, dependencies, architecture, deployment, and production workflow details.",
        "- Prefer concrete values from configs, metadata, docs, queries, and runtime inspection.",
        "- Do not summarize vaguely when a specific fact can be stated.",
        "- Do not guess at staffing, spend, timeline, or rollout assumptions.",
        "- If a value is missing or not discoverable, write `Unknown`.",
        "",
        "Inspect these first before answering:",
    ]
    lines.extend(f"- {item}" for item in inspect_first)
    lines.extend(["", "Reviewable checks and suggested investigation steps:"])
    lines.extend(f"- {item}" for item in reviewable_checks)
    lines.extend(["", "Behavioral differences and what could break:"])
    lines.extend(f"- {item}" for item in what_could_break)
    lines.extend(
        [
            "",
            "Return the result in exactly this format so Keshro can parse it:",
            "",
            "## Versions",
            "- Source version:",
            "- Target version:",
            "",
            "## Discover from environment",
        ]
    )
    lines.extend(f"- {item}" for item in discover_from_env)
    lines.extend(
        [
            "",
            "## Labor planning (team-supplied if known)",
            "- Team size:",
            f"- Experience level: Options: {' | '.join(_EXPERIENCE_LEVEL_OPTIONS)}",
            f"- Familiarity with source: Options: {' | '.join(_FAMILIARITY_OPTIONS)}",
            f"- Familiarity with target: Options: {' | '.join(_FAMILIARITY_OPTIONS)}",
            f"- Migration roles involved: Relevant examples: {' | '.join(_STANDARD_ROLE_SUGGESTIONS)}",
            "- Timeline:",
            "- Additional team notes:",
            "",
            "## Non-labor cost signals (team-supplied if known)",
        ]
    )
    for label, hint in _COST_SIGNAL_FIELDS:
        lines.append(f"- {label} ({currency}): {hint}")
    lines.extend(
        [
            "",
            "## Execution, cutover, and operating model (team-supplied if known)",
        ]
    )
    for label, hint in _EXECUTION_SIGNAL_FIELDS:
        lines.append(f"- {label}: {hint}")
    lines.extend(
        [
            "",
            f"## {source} to {target} details",
        ]
    )
    for field in all_fields:
        label = _field_label(field)
        hint = _clean(field.get("hint"))
        option_text = ""
        options = [
            str(option).strip()
            for option in (field.get("options") or [])
            if str(option).strip()
        ]
        if options:
            option_text = f" Options: {' | '.join(options)}"
        required_tag = " (required)" if field.get("required") else ""
        suffix = f" Hint: {hint}" if hint else ""
        lines.append(f"- {label}{required_tag}:{option_text}{suffix}")
    lines.extend(
        [
            "",
            "## Additional context",
            "- Anything else that materially affects risk, effort, validation, cutover, rollback, data movement, or delivery:",
            "",
            "Quality bar:",
            "- Use exact field labels.",
            "- Keep answers concise but specific.",
            "- Prefer inspected facts over opinions.",
            "- Do not add sections that are not requested.",
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
    template: dict, work_dir: str | None = None, agent: str = "auto"
) -> str:
    prompt = _build_agent_discovery_prompt(template)
    return _run_prompt_in_agent(
        prompt,
        missing_binary_message=(
            "Could not find a coding agent binary. Make sure you're running this from within your agent's terminal."
        ),
        failure_message_prefix=("Coding agent returned an error: "),
        empty_message="Coding agent returned no discovery response.",
        work_dir=work_dir,
        agent=agent,
    )


def _resolve_prompt_agent(agent: str) -> tuple[str, str]:
    requested = _clean(agent).lower() or _default_agent_preference() or "auto"
    if requested not in {"auto", "claude", "codex"}:
        raise SystemExit(
            "Unsupported agent. Use --agent auto, --agent claude, or --agent codex."
        )
    if requested in {"auto", "claude"}:
        claude_bin = shutil.which("claude")
        if claude_bin:
            return ("claude", claude_bin)
        if requested == "claude":
            raise SystemExit("Could not find the Claude Code binary on PATH.")
    if requested in {"auto", "codex"}:
        codex_bin = shutil.which("codex")
        if codex_bin:
            return ("codex", codex_bin)
        if requested == "codex":
            raise SystemExit("Could not find the Codex binary on PATH.")
    raise SystemExit("Could not find a supported coding agent binary on PATH.")


def _prompt_agent_display_name(agent: str) -> str:
    requested = _clean(agent).lower() or _default_agent_preference() or "auto"
    fallback_names = {
        "claude": "Claude Code",
        "codex": "Codex",
        "auto": "Claude Code",
    }
    try:
        resolved, _ = _resolve_prompt_agent(agent)
    except SystemExit:
        return fallback_names.get(requested, "Claude Code")
    if resolved == "claude":
        return "Claude Code"
    if resolved == "codex":
        return "Codex"
    return resolved


def _wrap_prompt_agent_error(detail: str, agent_name: str) -> str:
    lowered = detail.lower()
    if "hit your limit" in lowered or "quota" in lowered:
        if agent_name == "claude" and shutil.which("codex"):
            return (
                "Claude Code hit a usage limit while Keshro was gathering migration or project context.\n"
                f"Agent message: {detail}\n"
                "Try again with `keshro create --agent codex`, or make Codex the default with "
                "`keshro config set --agent codex`."
            )
        if agent_name == "codex" and shutil.which("claude"):
            return (
                "Codex hit a usage limit while Keshro was gathering migration or project context.\n"
                f"Agent message: {detail}\n"
                "Try again with `keshro create --agent claude`, or make Claude the default with "
                "`keshro config set --agent claude`."
            )
    return detail


def _run_prompt_in_agent(
    prompt: str,
    *,
    missing_binary_message: str,
    failure_message_prefix: str,
    empty_message: str,
    work_dir: str | None = None,
    agent: str = "auto",
) -> str:
    agent_name, agent_bin = _resolve_prompt_agent(agent)
    if not agent_bin:
        raise SystemExit(missing_binary_message)
    resolved_dir = str(Path(work_dir).resolve()) if work_dir else os.getcwd()
    if agent_name == "claude":
        command = [
            agent_bin,
            "-p",
            prompt,
            "--output-format",
            "text",
            "--permission-mode",
            "auto",
            "--add-dir",
            resolved_dir,
            "--no-session-persistence",
        ]
    else:
        command = [
            agent_bin,
            "exec",
            prompt,
            "--cd",
            resolved_dir,
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--ephemeral",
        ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        cwd=resolved_dir,
        check=False,
    )
    if result.returncode != 0:
        detail = (
            _clean(result.stderr) or _clean(result.stdout) or "Coding agent failed."
        )
        detail = _wrap_prompt_agent_error(detail, agent_name)
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


def _missing_required_template_fields(template: dict, answers: dict[str, str]) -> list[dict]:
    missing: list[dict] = []
    for field in template.get("fields") or []:
        field_id = _clean(field.get("id"))
        if not field.get("required") or not field_id:
            continue
        if _clean(answers.get(field_id)):
            continue
        missing.append(field)
    return missing


def _build_required_field_repair_prompt(template: dict, missing_fields: list[dict]) -> str:
    source = _clean(template.get("source")) or "Source"
    target = _clean(template.get("target")) or "Target"
    lines = [
        f"You are repairing missing required migration discovery fields for {source} -> {target}.",
        "The first discovery pass missed required template fields that may still be inferable from the repository.",
        "Inspect the current workspace again and answer only the required fields below.",
        "Return only the completed markdown template below with the same heading and exact field labels.",
        "Do not add commentary before or after the template.",
        "Use `Unknown` only if the repository truly does not provide enough evidence.",
        "",
        f"## {source} to {target} required field repair",
    ]
    for field in missing_fields:
        label = _clean(field.get("label")) or _clean(field.get("id")) or "Field"
        hint = _clean(field.get("hint"))
        suffix = f" Hint: {hint}" if hint else ""
        lines.append(f"- {label} (required):{suffix}")
    return "\n".join(lines)


def _repair_missing_required_discovery_answers(
    template: dict,
    answers: dict[str, str],
    *,
    work_dir: str | None = None,
    agent: str = "auto",
) -> dict[str, str]:
    missing_fields = _missing_required_template_fields(template, answers)
    if not missing_fields:
        return {}
    prompt = _build_required_field_repair_prompt(template, missing_fields)
    raw = _run_prompt_in_agent(
        prompt,
        missing_binary_message=(
            "Could not find a coding agent binary. Make sure you're running this from within your agent's terminal."
        ),
        failure_message_prefix=("Coding agent returned an error: "),
        empty_message="Coding agent returned no required-field repair response.",
        work_dir=work_dir,
        agent=agent,
    )
    repair_template = dict(template)
    repair_template["fields"] = missing_fields
    return _extract_discovery_answers(repair_template, raw)


def _get_migration_clarifiers(client: httpx.Client, payload: dict) -> list[dict]:
    response = client.post("/v1/migrations/clarifiers", json=payload)
    response.raise_for_status()
    body = response.json() or {}
    applied = body.get("applied_context")
    if applied:
        _print_applied_context_banner(applied)
    return list(body.get("questions") or [])


def _extract_repo_scan_answer(question: dict) -> str | None:
    """Return the value of a repo-scan-derived recommended option on a
    clarifier question, if one was injected by the backend clarifier
    (`_attach_discovered_recommendation`).

    Provenance lives in `answer_explanation` (the backend writes a
    "Detected from your repo scan." sentinel line there), not in the
    title. The title now displays the actual discovered value instead of
    a generic 'Based on repo scan' label, so matching on the title would
    produce false negatives."""
    for answer in question.get("answers") or []:
        if not answer.get("recommended"):
            continue
        explanation_lines = answer.get("answer_explanation") or []
        if not any(
            "repo scan" in str(line).lower() for line in explanation_lines
        ):
            continue
        value = _clean(answer.get("value"))
        if value and value.lower() != "unknown":
            return value
    return None


def _preset_answers_from_repo_scan(questions: list[dict]) -> dict[str, str]:
    """Seed suggested_answers with values discovery already pulled in via
    the backend's repo-scan recommendations. The clarifier feedback UI uses
    these to mark each option with a (suggested) tag, and `--answers-file`
    resume picks them up if the user just accepts.

    Questions without a repo-scan recommendation get nothing — the user
    fills them in directly. We deliberately don't call the agent here:
    discovery already had its shot, and re-asking adds 1-2 minutes per
    create with no new information."""
    preset: dict[str, str] = {}
    for question in questions:
        question_id = _clean(question.get("id"))
        if not question_id:
            continue
        discovered = _extract_repo_scan_answer(question)
        if discovered is not None:
            preset[question_id] = discovered
    return preset


def _print_agent_collection_warning(message: str) -> None:
    if _state.json:
        return
    print(f"{YELLOW}{message}{RESET}")


def _prompt_for_migration_template_fields(
    template: dict, answers: dict[str, str]
) -> dict[str, str]:
    if not _interactive_cli_prompts_allowed():
        return answers
    prompted = dict(answers)
    fields = list(template.get("fields") or [])
    if not fields:
        return prompted
    print(
        f"\n{CYAN}Review migration inputs{RESET} {DIM}(press Enter to keep the current value){RESET}"
    )
    for index, field in enumerate(fields, 1):
        field_id = _clean(field.get("id"))
        label = _clean(field.get("label")) or field_id or f"Field {index}"
        if not field_id:
            continue
        current_value = _clean(prompted.get(field_id))
        required = bool(field.get("required"))
        suffix = f" {YELLOW}(required){RESET}" if required else ""
        print(f"\n  {CYAN}{index}.{RESET} {label}{suffix}")
        options = list(field.get("options") or [])
        if options:
            for option_index, option in enumerate(options, 1):
                marker = " [suggested]" if _clean(str(option)) == current_value else ""
                print(f"     {DIM}{option_index}. {option}{marker}{RESET}")
        show_preview = bool(current_value) and not (
            options and any(_clean(str(option)) == current_value for option in options)
        )
        if show_preview:
            preview_lines, was_truncated = _format_preview_lines(current_value)
            print(f"     {DIM}Current value:{RESET}")
            for line in preview_lines:
                print(f"     {DIM}{line}{RESET}")
            if was_truncated:
                print(f"     {DIM}(truncated; type 'v' to view the full value){RESET}")
        if options:
            prompt_label = "  keep current or choose option"
            if current_value:
                prompt_label += f" [{current_value}]"
        elif current_value and show_preview:
            prompt_label = "  Enter=keep, v=view full, r=replace"
        elif current_value:
            prompt_label = "  keep current or enter replacement"
        else:
            prompt_label = "  enter value"
        while True:
            try:
                response = input(f"{prompt_label}: ").strip()
            except EOFError:
                print()
                response = ""
                break
            except KeyboardInterrupt:
                print()
                return prompted
            if (
                current_value
                and show_preview
                and not options
                and response.lower() in {"v", "view"}
            ):
                print(f"     {DIM}Full value:{RESET}")
                for line in _format_full_value_lines(current_value):
                    print(f"     {DIM}{line}{RESET}")
                continue
            if (
                current_value
                and show_preview
                and not options
                and response.lower() in {"r", "replace"}
            ):
                try:
                    response = input("  enter replacement: ").strip()
                except EOFError:
                    print()
                    response = ""
                except KeyboardInterrupt:
                    print()
                    return prompted
                break
            break
        if not response:
            continue
        if options:
            response = _resolve_menu_choice(
                response, [str(option) for option in options]
            )
        prompted[field_id] = (
            _match_select_option(field, response)
            if str(field.get("type") or "").strip() == "select"
            else response
        )
    return prompted


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


def _prompt_for_migration_clarifiers(
    questions: list[dict], suggested_answers: dict[str, str]
) -> dict[str, str]:
    return _prompt_for_clarifying_questions(
        questions,
        suggested_answers,
        heading="Follow-up questions",
    )


def _prompt_for_clarifying_questions(
    questions: list[dict],
    suggested_answers: dict[str, str],
    *,
    heading: str = "Clarifying questions",
    intro: str = "press Enter to keep the suggested answer",
) -> dict[str, str]:
    if not _interactive_cli_prompts_allowed() or not questions:
        return suggested_answers
    answers = dict(suggested_answers)
    print(f"\n{CYAN}{heading}{RESET} {DIM}({intro}){RESET}")
    for index, question in enumerate(questions, 1):
        question_id = _clean(question.get("id")) or f"q{index}"
        prompt_text = _clean(question.get("question")) or question_id
        why = _clean(question.get("why_this_matters"))
        options = list(question.get("answers") or [])
        placeholder = _clean(question.get("placeholder"))
        current_value = _clean(answers.get(question_id))
        print(f"\n  {CYAN}{index}.{RESET} {prompt_text}")
        if why:
            print(f"     {DIM}{why}{RESET}")
        if options:
            for option_index, option in enumerate(options, 1):
                title = _clean(option.get("answer_title")) or _clean(
                    option.get("value")
                )
                value = _clean(option.get("value"))
                marker = " (suggested)" if value and value == current_value else ""
                rec = _format_recommended_suffix(title, bool(option.get("recommended")))
                print(f"     {DIM}{option_index}. {title}{rec}{marker}{RESET}")
        elif placeholder:
            print(f"     {DIM}Hint: {placeholder}{RESET}")
        prompt_label = "  keep or enter answer"
        if current_value:
            prompt_label += f" [{current_value}]"
        try:
            response = input(f"{prompt_label}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not response:
            continue
        if options:
            option_map = {
                _clean(option.get("answer_title"))
                or _clean(option.get("value")): _clean(option.get("value"))
                or _clean(option.get("answer_title"))
                for option in options
                if _clean(option.get("answer_title")) or _clean(option.get("value"))
            }
            selected = _resolve_menu_choice(response, list(option_map))
            response = option_map.get(selected, response)
        answers[question_id] = response
    return answers


def _review_agent_suggested_answers(
    questions: list[dict],
    suggested_answers: dict[str, str],
    *,
    heading: str,
    non_interactive_notice: str,
) -> dict[str, str]:
    if not questions:
        return {}
    if _state.json:
        return suggested_answers
    if _inside_coding_agent():
        if not _state.json:
            print(
                f"{DIM}Using agent-suggested answers for this agent-driven flow. Review and adjust in the dashboard if needed.{RESET}"
            )
        return suggested_answers
    if sys.stdout.isatty():
        return _prompt_for_clarifying_questions(
            questions,
            suggested_answers,
            heading=heading,
        )
    print(f"{YELLOW}{non_interactive_notice}{RESET}")
    return {}


def _prompt_for_optional_cli_context(subject: str, context: str | None) -> str | None:
    if not _interactive_cli_prompts_allowed():
        return context
    existing = _clean(context)
    print(
        f"\n{CYAN}Additional context for {subject}?{RESET} {DIM}(optional; press Enter to skip){RESET}"
    )
    if existing:
        print(f"{DIM}Current context:{RESET} {existing}")
    try:
        response = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return context
    if not response:
        return context
    return "\n\n".join(part for part in [existing, response] if part)


def _create_migration_from_payload(
    payload: dict, template: dict, work_dir: str | None = None
) -> None:
    source = (
        _clean(template.get("source"))
        or _clean(payload.get("source_type"))
        or "Unknown source"
    )
    target = (
        _clean(template.get("target"))
        or _clean(payload.get("target_type"))
        or "Unknown target"
    )
    spinner_message = (
        f"Submitting {source} -> {target} migration and generating execution plan..."
    )
    created: dict = {}
    migration_id = ""
    linked_plan: dict | None = None
    app_url = _app_url_from_api_url(_state.api_url)
    sanitized_payload = _sanitize_json_payload(payload)
    with _Spinner(spinner_message):
        with make_client(_state.api_url, _state.token) as client:
            response = client.post("/v1/migrations", json=sanitized_payload)
            response.raise_for_status()
            created = response.json() or {}
            migration_id = _clean(created.get("id"))
            if migration_id:
                for attempt in range(6):
                    try:
                        plan_res = client.get(f"/v1/migrations/{migration_id}/plan")
                        plan_res.raise_for_status()
                        linked_plan = plan_res.json() or {}
                        break
                    except httpx.RequestError:
                        break
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code != 404:
                            break
                    if attempt < 5:
                        time.sleep(0.75)

                if linked_plan:
                    _set_default_plan_after_create(linked_plan, announce=False)

    if _state.json:
        print_output(dict(created), True)
        return

    status = _clean(created.get("status") or "pending") or "pending"
    print(f"\nMigration created: {source} -> {target}")
    if migration_id:
        print(f"  ID: {migration_id}")
    print(f"  Status: {status}")
    if migration_id:
        print(f"\n  {DIM}Dashboard: {app_url}/migrations/{migration_id}{RESET}")
    normalized_status = status.lower()
    if normalized_status in {"analyzing", "queued", "pending"}:
        print(
            f"  {DIM}Analysis is still running. Open the dashboard or run "
            f"{CYAN}keshro migration view {migration_id}{RESET}{DIM} until it is ready.{RESET}"
        )
    else:
        print(f"  Run {CYAN}keshro continue{RESET} to start executing.")
    if not work_dir:
        print(
            "\nTip: Use --dir to point to your project directory for better auto-discovery."
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

The current task and execution context are provided below. Do not re-fetch them before you start working — start directly from the task details provided here.

Style:
- Be concise. Do not narrate your thought process — just do the work and report what you did.
- Before running any keshro command or git checkpoint, print one short sentence explaining why (e.g. "Marking task as in progress."). Then run the command.
- Do not print internal edit summaries, diff recaps, self-critique, or course-correction commentary. If you revise an approach, just continue with the corrected work.
- Do not print `sed`, `rg`, or similar command output unless it is directly needed to explain a blocker or verification result.

Treat Keshro as the live execution record. When meaningful task progress happens, write it back while the work is happening rather than waiting until the end.

During execution:
- The launcher already marked this task in progress. Do not run `keshro task start` yourself.
- IMPORTANT: write progress notes frequently — at minimum after reading code, after each significant change, and before completion. These notes show up live in Keshro.
  Use: `keshro task note <task-id> -n "..."` for: what you found, what you changed, what you decided, what files you touched
- use `keshro task artifact <task-id> -l "<url>"` for PRs, commits, dashboards, issues, and runbooks
- use `keshro task block <task-id> -r "..."` the moment a real blocker appears that prevents further progress on the task
- if an external system is unavailable but you can still continue from local code, checked-in config, or documented context, record that in a note instead of blocking the task
- use `keshro task unblock <task-id>` when that blocker is cleared
- Do not run `keshro ... --help` or probe command syntax unless a command has already failed with a usage error.

When a task is done:
- record a concise completion note. It must include `Acceptance criteria met:` and `Verification:`. Add `Next task should know:` only when it helps the next task.
- ask for confirmation before running `keshro task done`
- when marking done, report your session cost if available: `keshro task done <task-id> --cost <usd_amount> --tokens <token_count> --model <model_name>` (check your session stats for cost/token info)
- after `keshro task done`, summarize what was accomplished and ask whether to continue to the next task
- if `keshro task done` reports that all plan tasks are completed, a completion audit runs automatically. Review the audit output — it checks whether identified risks were mitigated, open questions were resolved, and all tasks have evidence. Share the audit summary and dashboard link with the user.
- do not automatically start the next task without a clear go-ahead

Ask first before:
- `keshro task done`
- task deletion
- major replans that change scope, sequencing, or {connected_delivery_label or "linked delivery work"}

If a keshro command fails with a connection error, retry once after 5 seconds. For any other error, say what happened and continue working on the code. Do not retry more than once unless asked.

Rules:
- Keep updates concise, factual, and specific.
- Do not silently work around blockers or plan drift.
- Do not assume Keshro is current unless you updated it.
- If asked how to monitor progress, point to `keshro status --watch` or `keshro status --tui`.
- If you need more detail on any task, use `keshro task view <task-id>`.
- Use the active Keshro context already selected in this repo. Do not try to recover by probing alternate plan IDs."""


def _strip_injected_task_lines(text: str | None) -> tuple[str, list[str]]:
    cleaned = _clean(text)
    if not cleaned:
        return "", []
    summary_lines: list[str] = []
    hidden_lines: list[str] = []
    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("RISK:") or line.startswith("INVESTIGATE:"):
            hidden_lines.append(line)
            continue
        summary_lines.append(line)
    return " ".join(summary_lines).strip(), hidden_lines


def _build_continue_brief(
    plan: dict,
    task: dict,
    work_dir: str | None = None,
    session_id: str = "",
) -> str:
    resolved_plan_id = _clean(plan.get("id")) or "<plan-id>"
    task_id = _clean(task.get("id")) or "<task-id>"
    task_title = _clean(task.get("title")) or "Untitled task"
    task_description, hidden_lines = _strip_injected_task_lines(task.get("description"))
    task_status = _clean(task.get("status") or "todo")
    blocked_reason = _clean(task.get("blocked_reason"))
    notes = _clean(task.get("notes"))
    related_files = [
        str(item).strip()
        for item in (task.get("related_files") or [])
        if str(item).strip()
    ]
    acceptance = [
        str(item).strip()
        for item in (task.get("acceptance_criteria") or [])
        if str(item).strip()
    ]
    enrichment_sources = plan.get("enrichment_sources") or []
    analysis = _plan_analysis(plan)
    risks = analysis.get("risks") if isinstance(analysis.get("risks"), list) else []
    unknowns = (
        analysis.get("unknowns") if isinstance(analysis.get("unknowns"), list) else []
    )

    lines = [
        f"Task: {task_title}",
        f"Description: {task_description or 'No description provided.'}",
        f"Status: {task_status}",
        f"Plan: {resolved_plan_id} | Task ID: {task_id} | Session: {session_id}",
    ]
    if work_dir:
        lines.append(f"Project directory: {work_dir}")
    if blocked_reason:
        lines.append(f"Blocker: {blocked_reason}")
    if notes:
        lines.append(f"Current notes: {notes}")
    if related_files:
        lines.append(f"Related files: {', '.join(related_files)}")
    if acceptance:
        lines.append("Acceptance criteria:")
        for item in acceptance:
            lines.append(f"  - {item}")
    if hidden_lines:
        lines.append(
            f"Additional task context is available via: keshro task view {task_id} -p {resolved_plan_id}"
        )
    if enrichment_sources:
        names = [
            _clean(s.get("name")) for s in enrichment_sources if _clean(s.get("name"))
        ]
        if names:
            lines.append(f"Enriched by: {', '.join(names)}")
    if risks:
        lines.append("Top plan risks:")
        for risk in risks[:2]:
            title = _clean((risk or {}).get("title"))
            description = _clean((risk or {}).get("description"))
            lines.append(
                f"  - {_truncate_text(title or description or 'Unspecified risk')}"
            )
    if unknowns:
        lines.append("Open questions:")
        for unknown in unknowns[:2]:
            question = _clean((unknown or {}).get("question"))
            summary = _clean((unknown or {}).get("summary"))
            lines.append(
                f"  - {_truncate_text(question or summary or 'Unspecified question')}"
            )
        lines.append(
            f"Review full risks/questions in UI: {_current_app_url()}/plans/{resolved_plan_id}"
        )
    lines.extend(
        [
            "",
            "Execution reminders:",
            "- The launcher already marked this task in progress. Do not run `keshro task start` yourself.",
            f'- Record concise progress notes with: `keshro task note {task_id} -n "..."`',
            "- Do not run `keshro ... --help` unless a command actually fails with a usage error.",
            "- The current task and execution context are already included below. Do not re-fetch them before you start working.",
            "- Only mark the task blocked if work cannot continue. If local sources let you proceed, note the limitation instead.",
            "- If a keshro command fails with a connection error, retry once after 5 seconds. For any other error, say what happened and keep working unless the failure blocks the task.",
            "- Before `keshro task done`, include `Acceptance criteria met:` and `Verification:` in the completion note.",
            "- When `keshro task done` completes the last task in the plan, a completion audit runs automatically — review the output and share the audit summary and dashboard link with the user.",
            "- You can monitor progress with `keshro status --watch` or `keshro status --tui`.",
        ]
    )
    return "\n".join(lines)


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
        outcome = step.get("outcome")
        if not notes and not outcome:
            continue
        step_tags = {t.lower() for t in (step.get("tags") or [])}
        shared = target_tags & step_tags
        if not shared:
            continue

        # Filter out explicit handoff lines (already in the sequential handoff section)
        lines = []
        if notes:
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

        # Include outcome data when available
        if outcome and isinstance(outcome, dict):
            if outcome.get("files_changed"):
                paths = ", ".join(
                    f.get("path", "") for f in outcome["files_changed"] if f.get("path")
                )
                if paths:
                    lines.append(f"Files changed: {paths}")
            if outcome.get("errors_encountered"):
                for err in outcome["errors_encountered"]:
                    err_parts = [err.get("error_type", ""), err.get("message", "")]
                    if err.get("resolution"):
                        err_parts.append(f"resolved: {err['resolution']}")
                    lines.append("Error: " + " — ".join(p for p in err_parts if p))
            if outcome.get("approach"):
                lines.append(f"Approach: {outcome['approach']}")

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

    # Failure pattern warnings — known pitfalls from past executions
    raw_warnings = task.get("failure_warnings")
    if isinstance(raw_warnings, list):
        failure_warnings = "\n".join(_clean(w) for w in raw_warnings if _clean(w))
    else:
        failure_warnings = _clean(raw_warnings)
    if failure_warnings:
        history_lines.append("⚠ KNOWN FAILURE PATTERNS:")
        history_lines.append(_truncate_text(failure_warnings, limit=600))
        history_lines.append("")

    # Git state since last checkpoint
    git_state = _get_git_state_summary(work_dir) if work_dir else ""
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

    # Acceptance criteria
    acceptance = task.get("acceptance_criteria") or []
    if acceptance:
        task_block.append("Acceptance criteria:")
        for ac in acceptance:
            task_block.append(f"  - {ac}")

    # Risk level + reason
    risk_level = task.get("risk_level") or ""
    risk_reason = task.get("risk_reason") or ""
    if risk_level in ("high", "medium"):
        task_block.append(
            f"Risk: {risk_level}" + (f" — {risk_reason}" if risk_reason else "")
        )

    # Related files
    related_files = task.get("related_files") or []
    if related_files:
        task_block.append(f"Related files: {', '.join(related_files)}")

    # Plan enrichment sources (so agent knows what context was used)
    enrichment_sources = plan.get("enrichment_sources") or []
    if enrichment_sources:
        names = [s.get("name", "") for s in enrichment_sources if s.get("name")]
        if names:
            task_block.append(f"Plan context sources: {', '.join(names)}")

    analysis = _plan_analysis(plan)
    risks = analysis.get("risks") if isinstance(analysis.get("risks"), list) else []
    unknowns = (
        analysis.get("unknowns") if isinstance(analysis.get("unknowns"), list) else []
    )
    if risks:
        task_block.append("Top plan risks:")
        for risk in risks[:2]:
            title = _clean((risk or {}).get("title"))
            description = _clean((risk or {}).get("description"))
            task_block.append(
                f"  - {_truncate_text(title or description or 'Unspecified risk')}"
            )
    if unknowns:
        task_block.append("Open questions:")
        for unknown in unknowns[:2]:
            question = _clean((unknown or {}).get("question"))
            summary = _clean((unknown or {}).get("summary"))
            task_block.append(
                f"  - {_truncate_text(question or summary or 'Unspecified question')}"
            )
        plan_url = f"{_current_app_url()}/plans/{resolved_plan_id}"
        task_block.append(f"Review full risks/questions in UI: {plan_url}")

    continuation = [
        "",
        "Continue from this task now.",
        "- This task is already marked in progress by the launcher. Do not run `keshro task start` again.",
        f'- Before starting work, create a git checkpoint so changes can be rolled back if needed: `git add -A && git commit -m "keshro: checkpoint before {task_title}" --allow-empty`',
        "- Before writing code, briefly say what this task involves and which files you expect to touch.",
        (
            "- Read existing files relevant to this task to understand the current state before making changes."
            if work_dir
            else "- Start from the plan and task context first. Do not inspect the local working directory unless the task clearly requires repo-specific changes."
        ),
        "- If this task is blocked, do not automatically move to the next task unless the plan clearly supports parallel or out-of-order work.",
        "- If you continue execution, keep Keshro updated as you work.",
        "- Before marking a task done, verify your changes: run linters, check syntax, or run relevant tests if they exist. Record the validation result in your completion note under `Verification:`.",
        "- If the task has acceptance criteria, your completion note must explicitly include `Acceptance criteria met:` and `Verification:` before `keshro task done` will succeed.",
    ]
    if auto_continue:
        continuation.append(
            "- AUTO-CONTINUE MODE: After completing each task, automatically pull the next task with "
            "`keshro task next` and continue working. "
            "Still create checkpoints, record notes, and mark tasks done — but do not pause for confirmation between tasks. "
            "If a task fails (tests don't pass, code doesn't compile, validation fails), mark it blocked with "
            '`keshro task block <task-id> -r "..."` and stop. '
            "Say what failed and why. Do not skip to the next task."
        )

    if is_parallelizable:
        continuation.extend(
            [
                "",
                "PARALLEL TASK: This task is marked as parallelizable. Before starting the work itself:",
                "1. Say: 'This task can be parallelized. I will split it into sub-tasks that other agents can pick up.'",
                "2. Analyze the task to identify independent units of work (e.g., separate files, independent components, distinct modules).",
                "3. For each independent unit, create a sub-task using:",
                '   `keshro task plan --title "<sub-task title>" --description "<what to do>" -o "unassigned"`',
                f"4. Record a note on this parent task: `keshro task note {task_id} "
                f'-n "Split into N sub-tasks: <list sub-task titles>. Other agents can pick these up with keshro continue."`',
                f"5. Mark this parent task as completed: `keshro task done {task_id}`",
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
    plan: dict,
    task: dict,
    total_agents: int,
    work_dir: str | None = None,
) -> str:
    """Build a prompt for an unattended parallel agent working on a single task."""
    base_prompt = _build_continue_prompt(
        plan,
        task,
        work_dir=work_dir,
        auto_continue=False,
    )
    resolved_plan_id = _clean(plan.get("id")) or "<plan-id>"
    task_id = _clean(task.get("id")) or "<task-id>"
    task_title = _clean(task.get("title")) or "Untitled task"
    branch_name = f"keshro/{_task_title_slug(task_title)}"

    # Collect file changes from other tasks so this agent knows what's been touched
    other_files_lines = []
    steps = plan.get("plan_steps") or []
    for s in steps:
        if s.get("id") == task.get("id"):
            continue
        s_status = (s.get("status") or "").lower()
        if s_status not in ("completed", "in_progress"):
            continue
        s_files = s.get("related_files") or []
        s_notes = s.get("notes") or ""
        # Extract files from notes (format: "Files created: x, y | Files modified: z")
        import re as _re

        note_files = _re.findall(
            r"Files (?:created|modified|changed):\s*([^\n|]+)", s_notes
        )
        all_files = set(s_files)
        for nf in note_files:
            for f in nf.split(","):
                f = f.strip().split("(")[0].strip()  # remove descriptions in parens
                if f:
                    all_files.add(f)
        if all_files:
            status_label = "done" if s_status == "completed" else "active"
            other_files_lines.append(
                f"  [{status_label}] {_clean(s.get('title'))}: {', '.join(sorted(all_files)[:10])}"
            )

    parallel_parts = [
        "",
        "PARALLEL EXECUTION MODE:",
        f"- You are one of {total_agents} agents running concurrently in isolated git worktrees.",
        "- You are responsible for exactly ONE task. Complete it, then exit. Do NOT pull the next task.",
        f"- Create your changes on a branch named `{branch_name}`.",
        "- Do not ask the user for confirmation — execute autonomously.",
        f"- When done, mark the task complete: `keshro task done {task_id}`",
        f'- If the task fails, mark it blocked: `keshro task block {task_id} -r "reason"`',
        "- Ignore any earlier prompt text that includes a plan id on task commands; use the active Keshro context.",
    ]
    if other_files_lines:
        parallel_parts.append("")
        parallel_parts.append("Files touched by other agents (avoid conflicts):")
        parallel_parts.extend(other_files_lines)

    parallel_context = "\n".join(parallel_parts)
    return base_prompt + parallel_context


def _build_visible_parallel_prompt(
    plan: dict,
    task: dict,
    total_agents: int,
    work_dir: str | None = None,
) -> str:
    """Build a compact prompt for visible terminal sessions."""
    task_id = _clean(task.get("id")) or "<task-id>"
    task_title = _clean(task.get("title")) or "Untitled task"
    task_description = _clean(task.get("description")) or "No description provided."
    task_status = _clean(task.get("status") or "todo")
    task_notes = _clean(task.get("notes"))
    related_files = [_clean(path) for path in (task.get("related_files") or []) if _clean(path)]
    acceptance = [_clean(item) for item in (task.get("acceptance_criteria") or []) if _clean(item)]
    branch_name = f"keshro/{_task_title_slug(task_title)}"

    parts = [
        f"Task: {task_title}",
        f"Task ID: {task_id}",
        f"Status: {task_status}",
        f"Description: {task_description}",
    ]
    if work_dir:
        parts.append(f"Project directory: {work_dir}")
    if related_files:
        parts.append(f"Related files: {', '.join(related_files[:8])}")
    if acceptance:
        parts.append("Acceptance criteria:")
        for item in acceptance[:5]:
            parts.append(f"- {item}")
    if task_notes:
        note_lines = [line.strip() for line in task_notes.splitlines() if line.strip()]
        if note_lines:
            parts.append("Recent notes:")
            for line in note_lines[-2:]:
                parts.append(f"- {_truncate_text(line, limit=180)}")

    parts.extend(
        [
            "",
            "Execution rules:",
            f"- You are one of {total_agents} parallel agents in isolated git worktrees.",
            f"- Use branch `{branch_name}`.",
            "- The launcher already marked this task in progress. Do not run `keshro task start`.",
            "- Create a checkpoint commit before editing.",
            (
                "- Read local files when they are relevant to the task before making changes."
                if work_dir
                else "- Start from the plan and task context first. Do not inspect the local working directory unless the task clearly requires repo-specific changes."
            ),
            "- Keep terminal output minimal. Do not print command output, file listings, diffs, or long explanations unless needed for a blocker or verification.",
            "- Use the active Keshro context already selected in this repo.",
            f'- Record progress with `keshro task note {task_id} -n "..."`.',
            f'- Mark completion with `keshro task done {task_id}`.',
            f'- If the task cannot continue, mark it blocked with `keshro task block {task_id} -r "reason"`.',
            "- Complete exactly this task, then exit.",
        ]
    )
    return "\n".join(parts)


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
    failure_kind: str = ""


async def _post_agent_heartbeat_async(
    client: httpx.AsyncClient,
    plan_id: str,
    task_id: str,
    *,
    session_id: str = "",
    exec_dir: str = "",
    worktree_path: str = "",
    commit_sha: str = "",
    changed_files: list[str] | None = None,
    status: str = "running",
    agent_client: str | None = None,
    current_phase: str | None = None,
    progress_message: str | None = None,
    recent_error: str | None = None,
    runtime_context: dict | None = None,
) -> dict:
    resolved_path = worktree_path or exec_dir
    if changed_files is None and resolved_path:
        changed_files = await _collect_git_changed_files(resolved_path)
    if not commit_sha and resolved_path:
        try:
            commit_sha = await _git_stdout(
                "git", "rev-parse", "HEAD", cwd=resolved_path
            )
        except Exception:
            commit_sha = "unknown"

    payload: dict[str, object] = {
        "task_id": task_id,
        "commit_sha": commit_sha,
        "changed_files": changed_files or [],
        "status": status,
    }
    if agent_client:
        payload["agent_client"] = agent_client
    if session_id:
        payload["agent_session_id"] = session_id
    if current_phase:
        payload["current_phase"] = current_phase
    if progress_message:
        payload["progress_message"] = progress_message[:1000]
    if recent_error:
        payload["recent_error"] = recent_error[:2000]
    if runtime_context:
        payload["runtime_context"] = runtime_context

    try:
        resp = await client.post(f"/v1/agent/plans/{plan_id}/heartbeat", json=payload)
    except Exception:
        return {}
    if resp is None or not hasattr(resp, "json"):
        return {}
    try:
        return resp.json() or {}
    except Exception:
        return {}


async def _terminate_subprocess(proc: asyncio.subprocess.Process) -> None:
    if getattr(proc, "returncode", None) is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    except Exception:
        return

    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
        return
    except Exception:
        pass

    try:
        proc.kill()
    except ProcessLookupError:
        return
    except Exception:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception:
        pass


async def _terminate_pid(pid: int) -> None:
    if pid <= 0:
        return
    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        return
    except Exception:
        return

    deadline = asyncio.get_event_loop().time() + 5
    while asyncio.get_event_loop().time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        except Exception:
            return
        await asyncio.sleep(0.1)

    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        return
    except Exception:
        return


async def _close_terminal_session(title: str) -> None:
    """Close a Terminal.app tab/window created for a visible agent session."""
    if sys.platform != "darwin" or not title:
        return
    try:
        escaped_title = title.replace("\\", "\\\\").replace('"', '\\"')
        osa_script = (
            'tell application "Terminal"\n'
            "  repeat with w in windows\n"
            "    try\n"
            "      repeat with t in tabs of w\n"
            f'        if custom title of t is "{escaped_title}" then\n'
            "          close w saving no\n"
            "          exit repeat\n"
            "        end if\n"
            "      end repeat\n"
            "    end try\n"
            "  end repeat\n"
            "end tell"
        )
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            osa_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
    except Exception:
        pass


def _find_plan_task(plan_payload: dict, task_id: str) -> dict | None:
    steps = (
        plan_payload.get("plan_steps")
        or plan_payload.get("tasks")
        or (plan_payload.get("plan") or {}).get("tasks")
        or []
    )
    for step in steps:
        if _clean(step.get("id")) == task_id:
            return step
    return None


async def _watch_live_conflicts(
    client: httpx.AsyncClient,
    plan_id: str,
    task_id: str,
    *,
    worktree_path: str,
    proc: asyncio.subprocess.Process,
    session_id: str = "",
) -> dict:
    while getattr(proc, "returncode", None) is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=_LIVE_CONFLICT_POLL_SECONDS)
            break
        except asyncio.TimeoutError:
            pass
        heartbeat = await _post_agent_heartbeat_async(
            client,
            plan_id,
            task_id,
            worktree_path=worktree_path,
            session_id=session_id,
            current_phase="running",
        )
        if _clean(str(heartbeat.get("action"))).lower() != "pause":
            continue
        await _terminate_subprocess(proc)
        return heartbeat
    return {}


async def _commit_codex_worktree_snapshot(worktree_path: str, task_id: str) -> bool:
    status = await _git_stdout("git", "status", "--short", cwd=worktree_path)
    if not status.strip():
        return False
    await _git_stdout("git", "add", "-A", cwd=worktree_path)

    proc = await asyncio.create_subprocess_exec(
        "git",
        "-c",
        "user.name=Keshro",
        "-c",
        "user.email=bot@keshro.dev",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-m",
        f"WIP pause snapshot for {task_id}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=worktree_path,
    )
    _stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            (stderr or b"").decode(errors="replace").strip()
            or "failed to snapshot paused worktree state"
        )
    return True


async def _rebase_codex_worktree_onto_latest(
    repo_dir: str, worktree_path: str, task_id: str
) -> str:
    snapshot_committed = await _commit_codex_worktree_snapshot(worktree_path, task_id)
    latest_base = await _git_stdout("git", "rev-parse", "HEAD", cwd=repo_dir)
    current_head = await _git_stdout("git", "rev-parse", "HEAD", cwd=worktree_path)
    if current_head == latest_base and not snapshot_committed:
        return latest_base

    proc = await asyncio.create_subprocess_exec(
        "git",
        "rebase",
        latest_base,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=worktree_path,
    )
    _stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        abort_proc = await asyncio.create_subprocess_exec(
            "git",
            "rebase",
            "--abort",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=worktree_path,
        )
        await abort_proc.communicate()
        raise RuntimeError(
            (stderr or b"").decode(errors="replace").strip()
            or "failed to rebase paused worktree"
        )
    return latest_base


async def _wait_for_conflict_resolution(
    client: httpx.AsyncClient,
    plan_id: str,
    task_id: str,
) -> dict:
    started = time.monotonic()
    first_poll = True
    while time.monotonic() - started < _LIVE_CONFLICT_WAIT_TIMEOUT_SECONDS:
        try:
            resp = await client.get(f"/v1/plans/{plan_id}")
            if resp.status_code == 200:
                task = _find_plan_task(resp.json(), task_id)
                if task:
                    runtime_status = _clean(task.get("runtime_status")).lower()
                    if runtime_status == "needs_rebase":
                        return {"action": "needs_rebase", "task": task}
                    if not first_poll and runtime_status in ("", "active"):
                        return {"action": "resume", "task": task}
                    if _clean(task.get("status")).lower() == "blocked":
                        return {"action": "blocked", "task": task}
        except Exception:
            pass
        first_poll = False
        await asyncio.sleep(_LIVE_CONFLICT_POLL_SECONDS)
    return {"action": "timeout", "task": {}}


def _truncate_title(title: str, max_len: int = 50) -> str:
    """Truncate a title at a word boundary."""
    if len(title) <= max_len:
        return title
    truncated = title[:max_len].rsplit(" ", 1)[0]
    return truncated if truncated else title[:max_len]


def _usage_limit_error_from_text(text: str) -> str | None:
    lowered = _clean(text).lower()
    if not lowered:
        return None
    if "out of extra usage" in lowered or "usage limit" in lowered:
        return "Agent usage limit reached."
    if "hit your limit" in lowered or "out of credits" in lowered:
        return "Agent usage limit reached."
    return None


def _should_ignore_agent_output_line(text: str) -> bool:
    lowered = _clean(text).lower()
    if not lowered:
        return True
    ignored_prefixes = (
        "openai codex v",
        "anthropic claude",
        "workdir:",
        "model:",
        "provider:",
        "approval:",
        "sandbox:",
        "reasoning effort:",
        "reasoning summaries:",
        "session id:",
    )
    if lowered in {"--------", "user", "mcp startup: no servers"}:
        return True
    return lowered.startswith(ignored_prefixes)


def _summarize_agent_command_activity(text: str) -> str | None:
    cleaned = _clean(text)
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if (
        " in " not in cleaned
        or (" succeeded in " not in lowered and " exited " not in lowered)
    ):
        return None

    command_text = cleaned
    if " -lc " in cleaned:
        command_text = cleaned.split(" -lc ", 1)[1].strip()
    elif cleaned.startswith("exec "):
        command_text = cleaned[5:].strip()

    if len(command_text) >= 2 and command_text[0] == command_text[-1] and command_text[0] in {"'", '"'}:
        command_text = command_text[1:-1]

    file_matches = []
    seen_paths: set[str] = set()
    for match in re.findall(
        r"([A-Za-z0-9_./-]+\.(?:py|tf|tfvars|json|ya?ml|md|txt|sh))",
        command_text,
    ):
        if match not in seen_paths:
            file_matches.append(match)
            seen_paths.add(match)

    def _format_paths(prefix: str) -> str:
        if not file_matches:
            return ""
        shown = file_matches[:3]
        suffix = ", ..." if len(file_matches) > 3 else ""
        return f"{prefix} {', '.join(shown)}{suffix}"

    if "sed -n" in command_text or "cat " in command_text:
        return _format_paths("reading") or None
    if "ls -l" in command_text or command_text.startswith("ls "):
        return _format_paths("checking") or None
    if "rg -n" in command_text and file_matches:
        return _format_paths("searching") or None
    if "python -m py_compile" in command_text or "python3 -m py_compile" in command_text:
        return _format_paths("validating") or None
    if "pytest" in command_text:
        return _format_paths("testing") or None
    return None


# Registry of active agent subprocesses and terminal windows for Ctrl+C cleanup
_active_agent_procs: set[asyncio.subprocess.Process] = set()
_active_terminal_titles: set[str] = set()
_active_temp_files: set[str] = set()
_active_terminal_pid_files: set[str] = set()


async def _cleanup_active_agents():
    """Kill all active agent subprocesses and close Terminal.app windows."""
    global _active_agent_procs, _active_terminal_titles, _active_temp_files, _active_terminal_pid_files
    if sys.platform == "darwin" and _active_terminal_titles:
        await asyncio.gather(
            *(_close_terminal_session(title) for title in list(_active_terminal_titles)),
            return_exceptions=True,
        )
    if _active_agent_procs:
        await asyncio.gather(
            *(_terminate_subprocess(proc) for proc in list(_active_agent_procs)),
            return_exceptions=True,
        )
    for pid_file in list(_active_terminal_pid_files):
        try:
            pid = int(Path(pid_file).read_text().strip())
        except Exception:
            continue
        await _terminate_pid(pid)
    if sys.platform == "darwin" and _active_terminal_titles:
        await asyncio.gather(
            *(_close_terminal_session(title) for title in list(_active_terminal_titles)),
            return_exceptions=True,
        )
    # Clean up temp files
    for f in list(_active_temp_files):
        try:
            os.unlink(f)
        except OSError:
            pass
    _active_agent_procs.clear()
    _active_terminal_titles.clear()
    _active_temp_files.clear()
    _active_terminal_pid_files.clear()


def _build_agent_exec_command(
    agent_name: str,
    agent_bin: str,
    prompt: str,
    *,
    task_title: str,
    task_order: int | None = None,
    work_dir: str,
    worktree_name: str,
    use_worktree: bool = True,
) -> list[str]:
    if agent_name == "codex":
        return [
            agent_bin,
            "exec",
            prompt,
            "--cd",
            work_dir,
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--ephemeral",
        ]
    command = [
        agent_bin,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--permission-mode",
        "bypassPermissions",
        "--no-session-persistence",
        "--name",
        f"keshro: {'#' + str(task_order) + ' ' if task_order is not None else ''}{_truncate_title(task_title)}",
        "--add-dir",
        work_dir,
    ]
    if use_worktree:
        command[3:3] = ["--worktree", worktree_name]
    return command


def _build_visible_agent_exec_command(
    agent_name: str,
    agent_bin: str,
    prompt: str,
    *,
    task_title: str,
    task_order: int | None = None,
    work_dir: str,
    worktree_name: str,
    use_worktree: bool = True,
) -> list[str]:
    if agent_name == "codex":
        return [
            agent_bin,
            "exec",
            prompt,
            "--cd",
            work_dir,
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "--ephemeral",
        ]
    command = [
        agent_bin,
        "-p",
        prompt,
        "--permission-mode",
        "bypassPermissions",
        "--name",
        f"keshro: {'#' + str(task_order) + ' ' if task_order is not None else ''}{_truncate_title(task_title)}",
        "--add-dir",
        work_dir,
    ]
    if use_worktree:
        command[3:3] = ["--worktree", worktree_name]
    return command


def _shell_command_with_prompt_placeholder(
    command: list[str], prompt: str, placeholder: str = '"$_KESHRO_PROMPT"'
) -> str:
    """Render a shell command while keeping the prompt at its original argv slot."""
    rendered: list[str] = []
    replaced = False
    for arg in command:
        if not replaced and arg == prompt:
            rendered.append(placeholder)
            replaced = True
        else:
            rendered.append(shlex.quote(arg))
    if not replaced:
        rendered.append(placeholder)
    return " ".join(rendered)


async def _merge_codex_worktree_changes(
    repo_dir: str,
    worktree_path: str,
    base_rev: str,
    task_id: str,
) -> None:
    await _git_stdout("git", "add", "-A", cwd=worktree_path)
    status = await _git_stdout("git", "status", "--short", cwd=worktree_path)
    if status:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "commit",
            "-m",
            f"keshro parallel agent result: {task_id}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=worktree_path,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                (stderr or b"").decode(errors="replace").strip()
                or "failed to commit Codex worktree changes"
            )

    head_rev = await _git_stdout("git", "rev-parse", "HEAD", cwd=worktree_path)
    if head_rev == base_rev:
        return
    diff_proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        "--binary",
        f"{base_rev}..{head_rev}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=worktree_path,
    )
    patch_bytes, stderr = await diff_proc.communicate()
    if diff_proc.returncode != 0:
        raise RuntimeError(
            (stderr or b"").decode(errors="replace").strip()
            or "failed to build Codex worktree patch"
        )
    if not patch_bytes:
        return
    async with _codex_merge_lock:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "apply",
            "--3way",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=repo_dir,
        )
        _stdout, stderr = await proc.communicate(patch_bytes)
        if proc.returncode != 0:
            reset_proc = await asyncio.create_subprocess_exec(
                "git",
                "reset",
                "--hard",
                "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=repo_dir,
            )
            _reset_stdout, reset_stderr = await reset_proc.communicate()
            reset_error = (
                (reset_stderr or b"").decode(errors="replace").strip()
                if reset_proc.returncode != 0
                else ""
            )
            apply_error = (stderr or b"").decode(
                errors="replace"
            ).strip() or "failed to apply Codex worktree patch"
            if reset_error:
                raise RuntimeError(f"{apply_error} (cleanup failed: {reset_error})")
            raise RuntimeError(apply_error)


async def _launch_single_agent(
    task: dict,
    plan: dict,
    plan_id: str,
    work_dir: str,
    total_agents: int,
    semaphore: asyncio.Semaphore,
    api_client: httpx.AsyncClient,
    session_id: str = "",
    agent: str = "auto",
    visible: bool = False,
    launch_index: int = 0,
    use_local_repo_context: bool = True,
    worktree_policy: str = DEFAULT_WORKTREE_POLICY,
    pr_policy: str = DEFAULT_PR_POLICY,
) -> AgentResult:
    global _active_agent_procs, _active_terminal_titles, _active_temp_files, _active_terminal_pid_files
    task_id = _clean(task.get("id")) or "unknown"
    task_title = _clean(task.get("title")) or "Untitled"
    task_order = task.get("order")
    worktree_name = f"keshro-{task_id[:8]}"

    # Resolve agent binary (_resolve_prompt_agent raises SystemExit if not found)
    agent_name, agent_bin = _resolve_prompt_agent(agent)
    use_isolated_worktree = should_use_isolated_worktree(
        task, policy=worktree_policy
    )

    # Stagger agent launches to avoid git worktree lock contention
    if launch_index > 0:
        await asyncio.sleep(launch_index * 1.5)

    async with semaphore:
        print(f"  {YELLOW}▶{RESET} {task_title} {DIM}starting...{RESET}")
        # Register with Collaborator/Collaborator if available
        collab_session_id = f"keshro-{task_id}"
        launched_in_terminal = False
        visible_fallback_reason = ""
        _terminal_temp_files: list[str] = []  # For Terminal.app cleanup
        _terminal_title: str | None = None
        _terminal_pid_file: str | None = None
        _terminal_log_file: str | None = None
        try:
            from .collaborator import (
                is_available,
                launch_terminal,
                notify,
                session_end,
                session_start,
            )

            collab_active = is_available()
            if collab_active and not visible:
                session_start(collab_session_id, work_dir)
            elif visible and not collab_active:
                if sys.platform != "darwin":
                    visible_fallback_reason = (
                        "visible terminal sessions are unavailable on this platform; falling back to headless"
                    )
        except Exception:
            collab_active = False
            if visible and sys.platform != "darwin":
                visible_fallback_reason = (
                    "visible terminal launch unavailable; falling back to headless"
                )


        # For Codex, create a manual git worktree for isolation and merge the
        # resulting changes back into the main repo after the run succeeds.
        codex_worktree_path = ""
        codex_worktree_base_rev = ""
        codex_worktree_branch = ""
        if agent_name == "codex" and use_isolated_worktree:
            import tempfile
            import shutil

            codex_worktree_path = tempfile.mkdtemp(prefix=f"keshro-{worktree_name}-")
            codex_worktree_branch = f"keshro-{task_id[:8]}-{uuid.uuid4().hex[:6]}"
            try:
                codex_worktree_base_rev = await _git_stdout(
                    "git", "rev-parse", "HEAD", cwd=work_dir
                )
                created, err_msg = await _create_codex_worktree(
                    work_dir,
                    codex_worktree_path,
                    codex_worktree_branch,
                    codex_worktree_base_rev,
                )
                if not created and "already exists" in err_msg.lower():
                    await _cleanup_worktree(work_dir, codex_worktree_path)
                    try:
                        shutil.rmtree(codex_worktree_path, ignore_errors=True)
                    except Exception:
                        pass
                    created, err_msg = await _create_codex_worktree(
                        work_dir,
                        codex_worktree_path,
                        codex_worktree_branch,
                        codex_worktree_base_rev,
                    )
                if not created:
                    blocked_reason = f"Failed to create worktree for Codex: {err_msg}"
                    await _mark_task_status_async(
                        api_client,
                        plan_id,
                        task_id,
                        "blocked",
                        blocked_reason=blocked_reason,
                    )
                    return AgentResult(
                        task_id=task_id,
                        task_title=task_title,
                        exit_code=1,
                        stdout="",
                        stderr=blocked_reason,
                        duration_seconds=0,
                        failure_kind="launch",
                    )
            except Exception as exc:
                blocked_reason = f"Failed to create worktree for Codex: {exc}"
                await _mark_task_status_async(
                    api_client,
                    plan_id,
                    task_id,
                    "blocked",
                    blocked_reason=blocked_reason,
                )
                return AgentResult(
                    task_id=task_id,
                    task_title=task_title,
                    exit_code=1,
                    stdout="",
                    stderr=blocked_reason,
                    duration_seconds=0,
                    failure_kind="launch",
                )

        exec_dir = codex_worktree_path if codex_worktree_path else work_dir
        agent_env = os.environ.copy()
        agent_env["KESHRO_ACTIVE_PLAN_ID"] = plan_id
        agent_env["KESHRO_AGENT_CLIENT"] = "Codex" if agent_name == "codex" else "Claude Code"
        agent_env["KESHRO_SUPPRESS_AGENT_SKILL_BANNER"] = "1"
        runtime_context = _collect_task_runtime_context_for(exec_dir)
        latest_stdout_lines: list[str] = []
        latest_stderr_lines: list[str] = []
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        heartbeat_active = False
        heartbeat_task: asyncio.Task | None = None
        live_conflict_task: asyncio.Task | None = None
        live_pause_response: dict = {}
        overall_start = time.monotonic()
        stdout_text = ""
        stderr_text = ""
        result_text = ""
        exit_code = 0
        failure_kind = ""
        cost_usd = 0.0
        tokens_used = 0
        model_name = ""
        latest_error = ""
        live_retry_note = ""
        live_retry_count = 0
        last_headless_progress = ""
        last_headless_command_summary = ""
        last_headless_changed_files: tuple[str, ...] = ()
        last_headless_activity_at = time.monotonic()
        headless_reviewed_files: set[str] = set()
        headless_spinner_enabled = (
            not visible and total_agents == 1 and not _state.json and _stdout_is_tty()
        )
        headless_spinner_active = False
        headless_spinner_task: asyncio.Task | None = None

        def _clear_headless_spinner_line() -> None:
            if not headless_spinner_enabled:
                return
            width = shutil.get_terminal_size(fallback=(100, 24)).columns
            print("\r" + (" " * max(20, width - 1)) + "\r", end="", flush=True)

        async def _headless_spinner_loop() -> None:
            frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
            index = 0
            try:
                while headless_spinner_active:
                    elapsed = int(time.monotonic() - overall_start)
                    width = shutil.get_terminal_size(fallback=(100, 24)).columns
                    label = f"  {DIM}[{task_title}]{RESET} {CYAN}{frames[index % len(frames)]}{RESET} working ({elapsed}s)"
                    clipped = label
                    if len(clipped) >= width:
                        clipped = clipped[: max(1, width - 1)]
                    print("\r" + clipped, end="", flush=True)
                    index += 1
                    await asyncio.sleep(0.12)
            except asyncio.CancelledError:
                pass
            finally:
                _clear_headless_spinner_line()

        def _print_headless_progress(message: str) -> None:
            _clear_headless_spinner_line()
            print(f"  {DIM}[{task_title}]{RESET} {message}")

        # Report start with session ID via agent API and explicitly mark the task
        # in progress so the plan UI reflects the active run immediately.
        try:
            await api_client.post(
                f"/v1/agent/plans/{plan_id}/task-event",
                json={
                    "task_id": task_id,
                    "event": "start",
                    "agent_session_id": session_id,
                    "agent_client": agent_name,
                    "current_phase": "starting",
                    "progress_message": "Launching isolated agent worktree",
                    "runtime_context": runtime_context,
                },
            )
        except Exception:
            pass
        await _mark_task_status_async(api_client, plan_id, task_id, "in_progress")
        if not visible and headless_spinner_enabled:
            headless_spinner_active = True
            headless_spinner_task = asyncio.create_task(_headless_spinner_loop())

        async def _heartbeat_loop() -> None:
            nonlocal last_headless_progress, last_headless_changed_files, last_headless_activity_at
            in_progress_synced = False
            try:
                while heartbeat_active:
                    try:
                        if not in_progress_synced:
                            await _mark_task_status_async(
                                api_client, plan_id, task_id, "in_progress"
                            )
                            in_progress_synced = True
                        changed_files = await _git_changed_files(exec_dir)
                        if changed_files:
                            current_phase = "editing"
                            changed_signature = tuple(changed_files[:3])
                            if changed_signature != last_headless_changed_files:
                                edit_summary = await _summarize_file_edits(
                                    exec_dir, changed_files
                                )
                            else:
                                edit_summary = ""
                            progress_message = edit_summary or (
                                f"Editing {', '.join(changed_files[:3])}"
                            )
                            headless_progress = progress_message
                        elif launched_in_terminal:
                            current_phase = "visible_terminal"
                            progress_message = "Running in a visible Collaborator terminal"
                            headless_progress = ""
                            changed_signature = ()
                        else:
                            current_phase = "running"
                            progress_message = "Agent running"
                            headless_progress = ""
                            changed_signature = ()
                        if (
                            not visible
                            and headless_progress
                            and (
                                headless_progress != last_headless_progress
                                or changed_signature != last_headless_changed_files
                            )
                        ):
                            _print_headless_progress(headless_progress)
                            last_headless_progress = headless_progress
                            last_headless_changed_files = changed_signature
                            last_headless_activity_at = time.monotonic()
                        await _post_agent_heartbeat_async(
                            api_client,
                            plan_id,
                            task_id,
                            session_id=session_id,
                            exec_dir=exec_dir,
                            changed_files=changed_files,
                            status="running",
                            agent_client=agent_name,
                            current_phase=current_phase,
                            progress_message=progress_message,
                            recent_error=latest_error or None,
                            runtime_context=runtime_context,
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(3)
            except asyncio.CancelledError:
                return

        async def _consume_stream(
            stream: asyncio.StreamReader | None,
            latest_lines: list[str],
            chunks: list[str],
        ) -> None:
            nonlocal last_headless_command_summary, last_headless_progress, last_headless_activity_at
            if stream is None:
                return
            while True:
                line = await stream.readline()
                if not line:
                    break
                decoded = line.decode(errors="replace")
                chunks.append(decoded)
                cleaned = decoded.strip()
                if cleaned:
                    latest_lines.append(cleaned)
                    del latest_lines[:-20]
                    if visible or _should_ignore_agent_output_line(cleaned):
                        continue
                    for match in re.findall(
                        r"([A-Za-z0-9_./-]+\.(?:py|tf|tfvars|json|ya?ml|md|txt|sh))",
                        cleaned,
                    ):
                        headless_reviewed_files.add(match)
                    summary = _summarize_agent_command_activity(cleaned)
                    if summary in {
                        "switching git branch",
                        "creating checkpoint commit",
                    }:
                        continue
                    if summary and summary != last_headless_command_summary:
                        _print_headless_progress(summary)
                        last_headless_command_summary = summary
                        last_headless_progress = summary
                        last_headless_activity_at = time.monotonic()

        while True:
            prompt = (
                _build_visible_parallel_prompt(
                    plan,
                    task,
                    total_agents,
                    work_dir=work_dir if use_local_repo_context else None,
                )
                if visible
                else _build_parallel_prompt(
                    plan,
                    task,
                    total_agents,
                    work_dir=work_dir if use_local_repo_context else None,
                )
            )
            if live_retry_note:
                prompt = (
                    prompt
                    + "\n\nExecution update:\n"
                    + live_retry_note
                    + "\nReview the current codebase state before continuing."
                )
            command = _build_agent_exec_command(
                agent_name,
                agent_bin,
                prompt,
                task_title=task_title,
                task_order=task_order,
                work_dir=exec_dir,
                worktree_name=worktree_name,
                use_worktree=use_isolated_worktree,
            )
            visible_command = _build_visible_agent_exec_command(
                agent_name,
                agent_bin,
                prompt,
                task_title=task_title,
                task_order=task_order,
                work_dir=exec_dir,
                worktree_name=worktree_name,
                use_worktree=use_isolated_worktree,
            )

            heartbeat_active = True
            heartbeat_task = asyncio.create_task(_heartbeat_loop())

            if collab_active and visible and not launched_in_terminal:
                order_prefix = f"#{task_order} " if task_order is not None else ""
                tile_title = f"keshro: {order_prefix}{_truncate_title(task_title)}"
                try:
                    from .collaborator import launch_terminal

                    tile_id = launch_terminal(
                        command=shlex.join(
                            [
                                "env",
                                f"KESHRO_ACTIVE_PLAN_ID={plan_id}",
                                "KESHRO_SUPPRESS_AGENT_SKILL_BANNER=1",
                                *visible_command,
                            ]
                        ),
                        cwd=exec_dir,
                        title=tile_title,
                        session_id=collab_session_id,
                    )
                    launched_in_terminal = tile_id is not None
                    if launched_in_terminal:
                        print(
                            f"    {GREEN}Visible session launched:{RESET} {task_title} -> Collaborator ({tile_title})"
                        )
                    else:
                        visible_fallback_reason = (
                            "visible terminal launch RPC unavailable"
                        )
                        session_start(collab_session_id, work_dir)
                except Exception:
                    launched_in_terminal = False
                    visible_fallback_reason = "Collaborator launch failed"
                    session_start(collab_session_id, work_dir)

            # Native terminal fallback — open a new Terminal.app tab on macOS
            if visible and not launched_in_terminal and sys.platform == "darwin":
                try:
                    import tempfile

                    order_prefix = f"#{task_order} " if task_order is not None else ""
                    tab_title = f"keshro: {order_prefix}{_truncate_title(task_title)}"

                    # Write prompt to a separate file to avoid shell quoting
                    # issues (prompt often contains single quotes).
                    prompt_file = tempfile.NamedTemporaryFile(
                        mode="w", suffix=".txt", prefix="keshro-prompt-", delete=False
                    )
                    prompt_text = prompt
                    command_line = _shell_command_with_prompt_placeholder(
                        visible_command, prompt
                    )
                    prompt_file.write(prompt_text)
                    prompt_file.close()

                    pid_file = tempfile.NamedTemporaryFile(
                        mode="w", suffix=".pid", prefix="keshro-agent-", delete=False
                    )
                    pid_file.close()

                    log_file = tempfile.NamedTemporaryFile(
                        mode="w", suffix=".log", prefix="keshro-agent-", delete=False
                    )
                    log_file.close()

                    script_file = tempfile.NamedTemporaryFile(
                        mode="w", suffix=".sh", prefix="keshro-agent-", delete=False
                    )
                    script_file.write("#!/usr/bin/env zsh\n")
                    # Set the terminal tab/window title via escape sequence
                    esc_title = tab_title.replace("\\", "\\\\").replace('"', '\\"')
                    script_file.write(f'printf "\\033]0;{esc_title}\\007"\n')
                    # Clear the screen and scrollback so Terminal.app startup noise
                    # and the temp script path do not remain visible above the agent.
                    script_file.write('printf "\\033c\\033[3J\\033[H\\033[2J"\n')
                    # Add claude to PATH (common install locations)
                    script_file.write('export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:$PATH"\n')
                    script_file.write(
                        f'export KESHRO_ACTIVE_PLAN_ID={shlex.quote(plan_id)}\n'
                    )
                    script_file.write('export KESHRO_SUPPRESS_AGENT_SKILL_BANNER=1\n')
                    script_file.write(f"cd {shlex.quote(exec_dir)} || exit 1\n")
                    # Read prompt from file to avoid shell quoting issues.
                    script_file.write(
                        f'_KESHRO_PROMPT="$(cat {shlex.quote(prompt_file.name)})"\n'
                    )
                    script_file.write(
                        f'exec > >(tee -a {shlex.quote(log_file.name)}) 2>&1\n'
                    )
                    script_file.write(f'echo $$ > {shlex.quote(pid_file.name)}\n')
                    script_file.write(f"exec {command_line}\n")
                    script_file.close()
                    os.chmod(script_file.name, 0o755)

                    # Escape for AppleScript string (double backslashes and double quotes)
                    osa_path = script_file.name.replace("\\", "\\\\").replace('"', '\\"')
                    osa_title = tab_title.replace("\\", "\\\\").replace('"', '\\"')
                    osa_script = (
                        'tell application "Terminal"\n'
                        "  activate\n"
                        f'  do script "{osa_path}"\n'
                        f'  set custom title of selected tab of front window to "{osa_title}"\n'
                        "end tell"
                    )
                    osa_proc = await asyncio.create_subprocess_exec(
                        "osascript", "-e", osa_script,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _osa_out, osa_err = await osa_proc.communicate()
                    if osa_proc.returncode == 0:
                        launched_in_terminal = True
                        _terminal_temp_files = [
                            script_file.name,
                            prompt_file.name,
                            pid_file.name,
                            log_file.name,
                        ]
                        _terminal_title = tab_title
                        _terminal_pid_file = pid_file.name
                        _terminal_log_file = log_file.name
                        _active_terminal_titles.add(tab_title)
                        _active_temp_files.update(_terminal_temp_files)
                        _active_terminal_pid_files.add(pid_file.name)
                        print(
                            f"    {GREEN}Visible session launched:{RESET} {task_title} -> Terminal.app ({tab_title})"
                        )
                    else:
                        err_detail = (osa_err or b"").decode(errors="replace").strip()
                        visible_fallback_reason = (
                            "Failed to launch Terminal.app"
                            + (f": {err_detail}" if err_detail else "")
                            + "; falling back to headless"
                        )
                        try:
                            os.unlink(script_file.name)
                        except OSError:
                            pass
                        try:
                            os.unlink(prompt_file.name)
                        except OSError:
                            pass
                        try:
                            os.unlink(pid_file.name)
                        except OSError:
                            pass
                        try:
                            os.unlink(log_file.name)
                        except OSError:
                            pass
                except Exception as exc:
                    visible_fallback_reason = (
                        f"Failed to launch Terminal.app: {exc}; falling back to headless"
                    )

            if visible and not launched_in_terminal and visible_fallback_reason:
                print(
                    f"    {RED}Visible launch failed:{RESET} {visible_fallback_reason}"
                )

            if launched_in_terminal:
                exit_code = 0
                stdout_text = ""
                stderr_text = ""
                poll_interval = 2
                _terminal_poll_start = asyncio.get_event_loop().time()
                _TERMINAL_STALE_TIMEOUT = 300  # 5 min with no task status change = stale
                while True:
                    await asyncio.sleep(poll_interval)
                    log_excerpt = ""
                    usage_limit_reason = None
                    if _terminal_log_file:
                        try:
                            log_text = Path(_terminal_log_file).read_text(
                                errors="replace"
                            )
                            log_lines = log_text.strip().splitlines()
                            if log_lines:
                                log_excerpt = "\n".join(log_lines[-20:])
                            usage_limit_reason = _usage_limit_error_from_text(log_text)
                        except OSError:
                            pass
                    if usage_limit_reason:
                        exit_code = 1
                        failure_kind = "usage_limit"
                        stderr_text = usage_limit_reason
                        if log_excerpt:
                            stderr_text = f"{stderr_text}\nRecent output:\n{log_excerpt}"
                        print(
                            f"    {RED}Visible session failed immediately:{RESET} {task_title} ({usage_limit_reason})"
                        )
                        break
                    try:
                        resp = await api_client.get(f"/v1/plans/{plan_id}")
                        if resp.status_code == 200:
                            plan_data = resp.json()
                            tasks_list = (
                                plan_data.get("plan_steps")
                                or plan_data.get("tasks")
                                or plan_data.get("plan", {}).get("tasks")
                                or []
                            )
                            for t in tasks_list:
                                if t.get("id") == task_id:
                                    status = t.get("status", "")
                                    if status in ("completed", "done"):
                                        break
                                    elif status in ("canceled", "cancelled"):
                                        exit_code = 1
                                        stderr_text = "Agent task was cancelled"
                                        break
                                    elif status == "blocked":
                                        exit_code = 1
                                        stderr_text = t.get(
                                            "blocked_reason", "Agent blocked"
                                        )
                                        break
                            else:
                                poll_interval = min(poll_interval + 2, 15)
                                continue
                            break
                    except Exception:
                        pass
                    if _terminal_pid_file:
                        try:
                            pid = int(Path(_terminal_pid_file).read_text().strip())
                        except Exception:
                            pid = None
                        if pid is not None:
                            try:
                                os.kill(pid, 0)
                            except OSError:
                                exit_code = 1
                                failure_kind = "launch"
                                stderr_text = (
                                    "Visible agent exited before updating task status."
                                )
                                if log_excerpt:
                                    stderr_text = (
                                        f"{stderr_text}\nRecent output:\n{log_excerpt}"
                                    )
                                print(
                                    f"    {RED}Visible session exited early:{RESET} {task_title}"
                                )
                                break
                            except Exception:
                                pass
                    # Check if task was never picked up (agent failed to start)
                    elapsed = asyncio.get_event_loop().time() - _terminal_poll_start
                    if elapsed > _TERMINAL_STALE_TIMEOUT:
                        exit_code = 1
                        failure_kind = "launch"
                        stderr_text = "Terminal agent did not update task status within 5 minutes — agent may have failed to start"
                        print(f"    {YELLOW}!{RESET} {task_title}: terminal agent timed out")
                        break
                    poll_interval = min(poll_interval + 2, 15)

                # Clean up Terminal.app temp files
                for _f in _terminal_temp_files:
                    try:
                        os.unlink(_f)
                    except OSError:
                        pass
                _active_temp_files.difference_update(_terminal_temp_files)
                _terminal_temp_files = []
                if _terminal_title:
                    await _close_terminal_session(_terminal_title)
                    _active_terminal_titles.discard(_terminal_title)
                if _terminal_pid_file:
                    _active_terminal_pid_files.discard(_terminal_pid_file)
                break

            # Standard subprocess mode — pipe stdout for agent output parsing
            try:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=exec_dir,
                    env=agent_env,
                )
                _active_agent_procs.add(proc)
                if agent_name == "codex" and codex_worktree_path:
                    live_conflict_task = asyncio.create_task(
                        _watch_live_conflicts(
                            api_client,
                            plan_id,
                            task_id,
                            worktree_path=codex_worktree_path,
                            proc=proc,
                            session_id=session_id,
                        )
                    )
                else:
                    live_conflict_task = None
                if (
                    getattr(proc, "stdout", None) is None
                    or getattr(proc, "stderr", None) is None
                ):
                    stdout_bytes, stderr_bytes = await proc.communicate()
                    stdout_text = (stdout_bytes or b"").decode(errors="replace").strip()
                    stderr_text = (stderr_bytes or b"").decode(errors="replace").strip()
                    exit_code = proc.returncode or 0
                    _active_agent_procs.discard(proc)
                else:
                    readers = [
                        asyncio.create_task(
                            _consume_stream(
                                proc.stdout, latest_stdout_lines, stdout_chunks
                            )
                        ),
                        asyncio.create_task(
                            _consume_stream(
                                proc.stderr, latest_stderr_lines, stderr_chunks
                            )
                        ),
                    ]
                    await proc.wait()
                    await asyncio.gather(*readers, return_exceptions=True)
                    exit_code = proc.returncode or 0
                    _active_agent_procs.discard(proc)
                    stdout_text = "".join(stdout_chunks).strip()
                    stderr_text = "".join(stderr_chunks).strip()
                if live_conflict_task is not None:
                    try:
                        live_pause_response = await live_conflict_task
                    except (Exception, asyncio.CancelledError):
                        live_pause_response = {}
            except Exception as exc:
                heartbeat_active = False
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)
                if live_conflict_task is not None:
                    live_conflict_task.cancel()
                    await asyncio.gather(live_conflict_task, return_exceptions=True)
                if codex_worktree_path:
                    await _cleanup_worktree(work_dir, codex_worktree_path)
                if codex_worktree_branch:
                    try:
                        branch_proc = await asyncio.create_subprocess_exec(
                            "git",
                            "branch",
                            "-D",
                            codex_worktree_branch,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            cwd=work_dir,
                        )
                        await branch_proc.communicate()
                    except Exception:
                        pass
                if collab_active:
                    try:
                        session_end(collab_session_id)
                    except Exception:
                        pass
                for _f in _terminal_temp_files:
                    try:
                        os.unlink(_f)
                    except OSError:
                        pass
                _active_temp_files.difference_update(_terminal_temp_files)
                _terminal_temp_files = []
                if _terminal_title:
                    await _close_terminal_session(_terminal_title)
                    _active_terminal_titles.discard(_terminal_title)
                if _terminal_pid_file:
                    _active_terminal_pid_files.discard(_terminal_pid_file)
                return AgentResult(
                    task_id=task_id,
                    task_title=task_title,
                    exit_code=1,
                    stdout="",
                    stderr=str(exc),
                    duration_seconds=time.monotonic() - overall_start,
                    failure_kind="launch",
                )
            heartbeat_active = False
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)

            if live_pause_response:
                live_retry_count += 1
                blocker_id = (
                    _clean(str(live_pause_response.get("conflict_task_id")))
                    or "another task"
                )
                pause_reason = (
                    _clean(str(live_pause_response.get("reason")))
                    or f"Live overlap detected with {blocker_id}."
                )
                print(f"    {YELLOW}!{RESET} {pause_reason}")
                await _post_agent_note_async(
                    api_client,
                    plan_id,
                    task_id,
                    f"CLI paused this worktree after live conflict detection: {pause_reason}",
                    session_id=session_id,
                )
                if live_retry_count > _LIVE_CONFLICT_MAX_RETRIES:
                    exit_code = 1
                    stderr_text = (
                        "Exceeded live conflict retry limit while waiting to resume."
                    )
                    result_text = ""
                    break

                resolution = await _wait_for_conflict_resolution(
                    api_client, plan_id, task_id
                )
                action = _clean(resolution.get("action")).lower()
                task_state = resolution.get("task") or {}
                if action == "needs_rebase":
                    try:
                        codex_worktree_base_rev = (
                            await _rebase_codex_worktree_onto_latest(
                                work_dir,
                                codex_worktree_path,
                                task_id,
                            )
                        )
                    except Exception as exc:
                        exit_code = 1
                        stderr_text = str(exc)
                        result_text = ""
                        break
                    live_retry_note = (
                        f"An earlier attempt was paused because task '{blocker_id}' touched the same files. "
                        "The worktree has been rebased onto the latest base."
                    )
                    await _post_agent_note_async(
                        api_client,
                        plan_id,
                        task_id,
                        f"Resuming after rebasing this worktree onto the latest base following overlap with '{blocker_id}'.",
                        session_id=session_id,
                    )
                    print(
                        f"    {CYAN}↺{RESET} {task_title} {DIM}rebased after {blocker_id}; resuming...{RESET}"
                    )
                    continue
                if action == "resume":
                    live_retry_note = (
                        f"An earlier attempt was paused because task '{blocker_id}' touched the same files. "
                        "That conflicting task stopped before landing changes."
                    )
                    await _post_agent_note_async(
                        api_client,
                        plan_id,
                        task_id,
                        f"Conflicting task '{blocker_id}' stopped before landing. Resuming this worktree.",
                        session_id=session_id,
                    )
                    print(
                        f"    {CYAN}↺{RESET} {task_title} {DIM}conflict cleared; resuming...{RESET}"
                    )
                    continue
                if action == "blocked":
                    exit_code = 1
                    stderr_text = _clean(task_state.get("blocked_reason")) or (
                        f"Task became blocked while waiting for '{blocker_id}' to finish."
                    )
                    result_text = ""
                    break
                exit_code = 1
                stderr_text = (
                    _clean(task_state.get("runtime_status_reason"))
                    or "Timed out waiting for the conflicting task to finish."
                )
                result_text = ""
                break

            break

        duration = time.monotonic() - overall_start

        # Parse cost and token data from agent's JSON output (Claude only)
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
                    f"/v1/agent/plans/{plan_id}/task-event",
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

        if exit_code == 0 and codex_worktree_path and codex_worktree_base_rev:
            try:
                await _merge_codex_worktree_changes(
                    work_dir,
                    codex_worktree_path,
                    codex_worktree_base_rev,
                    task_id,
                )
            except Exception as exc:
                exit_code = 1
                failure_kind = "execution"
                stderr_text = str(exc)

        if exit_code == 0:
            # Build a detailed completion note
            touched_files = await _git_changed_files(exec_dir)
            cost_parts = [f"{duration:.0f}s"]
            if tokens_used > 0:
                cost_parts.append(f"{tokens_used:,} tokens")
            if model_name:
                cost_parts.append(model_name)
            if cost_usd > 0:
                cost_parts.append(f"${cost_usd:.4f}")
            note = f"Completed by parallel agent in {' | '.join(cost_parts)}"
            await _mark_task_status_async(
                api_client, plan_id, task_id, "completed"
            )
            # Append completion summary as a note (don't overwrite agent notes)
            await _post_agent_note_async(
                api_client, plan_id, task_id, note, session_id=session_id
            )
            # Collect structured outcome data
            outcome = await _collect_task_outcome_async(exec_dir)
            # Report structured metrics via agent API
            try:
                done_payload: dict[str, object] = {
                    "task_id": task_id,
                    "event": "done",
                    "agent_session_id": session_id,
                    "duration_seconds": duration,
                    "tokens_used": tokens_used,
                    "cost_usd": cost_usd,
                    "model": model_name,
                }
                if outcome:
                    done_payload["outcome"] = outcome
                done_payload["current_phase"] = "completed"
                done_payload["progress_message"] = note
                done_payload["touched_files"] = touched_files
                # Update runtime context with worktree info
                wt_path = str(Path(work_dir) / ".claude" / "worktrees" / worktree_name)
                if Path(wt_path).is_dir():
                    updated_rc = dict(runtime_context or {})
                    updated_rc["cwd"] = wt_path
                    try:
                        wt_br = await _git_stdout("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=wt_path)
                        if wt_br and wt_br != "HEAD":
                            updated_rc["git_branch"] = wt_br
                    except Exception:
                        pass
                    done_payload["runtime_context"] = updated_rc
                else:
                    done_payload["runtime_context"] = runtime_context
                await api_client.post(
                    f"/v1/agent/plans/{plan_id}/task-event",
                    json=done_payload,
                )
            except Exception:
                pass

            worktree_dir = str(Path(work_dir) / ".claude" / "worktrees" / worktree_name)
            task_branch_dir = (
                worktree_dir
                if Path(worktree_dir).is_dir()
                else (exec_dir if use_isolated_worktree else "")
            )
            branch_pushed = False
            if task_branch_dir:
                try:
                    wt_branch = await _git_stdout(
                        "git", "rev-parse", "--abbrev-ref", "HEAD", cwd=task_branch_dir
                    )
                    if wt_branch and wt_branch != "HEAD":
                        await api_client.patch(
                            f"/v1/plans/{plan_id}/tasks/{task_id}",
                            json={"github_branch": wt_branch},
                        )
                except Exception:
                    pass

                pr_url, branch_pushed = await _create_task_pr(
                    exec_dir=task_branch_dir,
                    task_id=task_id,
                    task_title=task_title,
                    plan_title=_clean(plan.get("title")) or "Untitled plan",
                    task=task,
                    api_client=api_client,
                    plan_id=plan_id,
                    pr_policy=pr_policy,
                )
                if pr_url:
                    print(f"    {GREEN}PR:{RESET} {pr_url}")

            if Path(worktree_dir).is_dir() and branch_pushed:
                await _cleanup_worktree(work_dir, worktree_dir)
            elif Path(worktree_dir).is_dir():
                # No PR created — check if worktree is clean (no new commits)
                has_changes = False
                try:
                    diff = await _git_stdout(
                        "git", "diff", "--stat", "HEAD", cwd=worktree_dir
                    )
                    status = await _git_stdout(
                        "git", "status", "--short", cwd=worktree_dir
                    )
                    has_changes = bool(diff.strip() or status.strip())
                except Exception:
                    pass
                if not has_changes:
                    await _cleanup_worktree(work_dir, worktree_dir)
                else:
                    print(
                        f"    {YELLOW}Worktree preserved with uncommitted changes:{RESET} "
                        f"{DIM}{worktree_dir}{RESET}"
                    )
        else:
            reason = stderr_text[:200] or result_text[:200] or "Agent exited with error"
            if failure_kind in {"launch", "usage_limit"}:
                note = f"Agent launch failed transiently: {reason}"
                await _mark_task_status_async(
                    api_client, plan_id, task_id, "todo", notes=note
                )
                try:
                    await api_client.post(
                        f"/v1/agent/plans/{plan_id}/task-event",
                        json={
                            "task_id": task_id,
                            "event": "note",
                            "note": note,
                            "agent_session_id": session_id,
                            "current_phase": "failed_to_launch",
                            "recent_error": reason,
                            "touched_files": await _git_changed_files(exec_dir),
                            "runtime_context": runtime_context,
                        },
                    )
                except Exception:
                    pass
            else:
                await _mark_task_status_async(
                    api_client, plan_id, task_id, "blocked", blocked_reason=reason
                )
                try:
                    await api_client.post(
                        f"/v1/agent/plans/{plan_id}/task-event",
                        json={
                            "task_id": task_id,
                            "event": "block",
                            "reason": reason,
                            "agent_session_id": session_id,
                            "current_phase": "blocked",
                            "recent_error": reason,
                            "touched_files": await _git_changed_files(exec_dir),
                            "runtime_context": runtime_context,
                        },
                    )
                except Exception:
                    pass

        # End Collaborator session + notify
        if collab_active:
            try:
                session_end(collab_session_id)
                if exit_code == 0:
                    notify(f"✓ {task_title[:50]} completed ({duration:.0f}s)")
                else:
                    if failure_kind in {"launch", "usage_limit"}:
                        notify(f"✗ {task_title[:50]} failed to launch")
                    else:
                        notify(f"✗ {task_title[:50]} blocked")
            except Exception:
                pass

        for _f in _terminal_temp_files:
            try:
                os.unlink(_f)
            except OSError:
                pass
        _active_temp_files.difference_update(_terminal_temp_files)
        if _terminal_title:
            await _close_terminal_session(_terminal_title)
            _active_terminal_titles.discard(_terminal_title)
        if _terminal_pid_file:
            _active_terminal_pid_files.discard(_terminal_pid_file)

        if codex_worktree_path:
            await _cleanup_worktree(work_dir, codex_worktree_path)
            if codex_worktree_branch:
                try:
                    await _git_stdout(
                        "git", "branch", "-D", codex_worktree_branch, cwd=work_dir
                    )
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
            failure_kind=failure_kind,
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
    agent: str = "auto",
    visible: bool = False,
    executor_override: str | None = None,
) -> None:
    import uuid as _uuid

    from .executor import (
        DEFAULT_EXECUTOR,
        MANAGED_AGENT,
        build_executor,
        resolve_task_executor,
    )

    resolved_plan_id = _require_plan_context(plan_id)
    inside_agent_session = _inside_coding_agent()
    resolved_dir, use_local_repo_context = _resolve_continue_work_dir(
        resolved_plan_id, work_dir, allow_prompt=True
    )
    exec_dir = resolved_dir or os.getcwd()

    # Verify that the resolved agent binary exists (raises SystemExit if not found)
    _resolve_prompt_agent(agent)

    # Warn if not on the default branch — worktrees branch from current HEAD
    try:
        current_branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=exec_dir,
        ).stdout.strip()
        default_branch = _resolve_default_branch(exec_dir)
        if default_branch and current_branch and current_branch != default_branch:
            print(
                f"{YELLOW}Warning:{RESET} You are on branch {CYAN}{current_branch}{RESET}, "
                f"not {CYAN}{default_branch}{RESET}. "
                f"Agent worktrees will branch from {CYAN}{current_branch}{RESET}.\n"
            )
    except Exception:
        pass

    with make_client(_state.api_url, _state.token) as client:
        res = client.get(f"/v1/plans/{resolved_plan_id}")
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
                _fetch_and_display_completion_audit(resolved_plan_id, plan)
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
        blocked_steps = [
            s for s in steps if _clean(s.get("status") or "").lower() == "blocked"
        ]

        print(
            f"\n{done_count}/{total_count} done — {len(actionable)} task(s) ready to execute:"
        )

        for task in actionable:
            title = _clean(task.get("title")) or "Untitled"
            order = task.get("order", 0)
            deps = task.get("depends_on") or []
            dep_str = f" {DIM}← {', '.join(deps[:3])}{RESET}" if deps else ""
            print(f"  Task #{order}: {title}{dep_str}")

        if blocked_steps:
            print(f"\n  {YELLOW}⚠ {len(blocked_steps)} blocked (skipped):{RESET}")
            for bs in blocked_steps:
                tid = _clean(bs.get("id")) or "?"
                order = bs.get("order", 0)
                reason = _clean(bs.get("blocked_reason")) or "no reason"
                print(f"    {RED}✗{RESET} (#{order}) {_clean(bs.get('title'))}")
                print(f"      {DIM}{reason}{RESET}")
                print(
                    f"      {DIM}→ keshro task unblock {tid} -p {resolved_plan_id}{RESET}"
                )
            print()

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

        agent_count = len(actionable)
        agent_label = "agent" if agent_count == 1 else "agents"
        print(
            f"\nLaunching {agent_count} {agent_label} (max concurrency: {max_concurrency})...\n"
        )
        if wave == 1:
            print(
                f"{DIM}💡 Monitor all agents: keshro status --tui -p {resolved_plan_id}{RESET}\n"
            )
            if inside_agent_session:
                print(
                    f"{DIM}Agent-session mode: execution stays headless, and Keshro will summarize live progress here as tasks start, edit files, validate, complete, or block.{RESET}\n"
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
            single_task_label = (
                _clean(actionable[0].get("title")) if len(actionable) == 1 else ""
            )
            launch_specs = [
                {
                    "task": task,
                    "session_id": f"agent-{_clean(task.get('id'))[:8]}-{_uuid.uuid4().hex[:6]}",
                    "runtime_context": _collect_task_runtime_context_for(exec_dir),
                }
                for task in actionable
            ]
            # Resolve each task's executor: --executor flag > task.executor >
            # plan.effective_default_executor (server-resolved user→org default,
            # safety net for legacy plans and post-creation user-default
            # changes) > DEFAULT_EXECUTOR. ``build_executor`` returns the
            # right concrete implementation for the resolved name;
            # ``LocalClaudeCodeExecutor`` delegates back to ``_launch_single_agent``
            # (today's behavior), while ``ManagedAgentExecutor`` hits the
            # backend session proxy. We log per-task selection if any task is
            # set to something non-default so users can see what's being
            # routed where.
            plan_default = _clean(plan.get("effective_default_executor")) or None
            plan_worktree_policy = resolve_worktree_policy(plan)
            plan_pr_policy = resolve_pr_policy(plan)
            resolved_executors = [
                resolve_task_executor(
                    spec["task"],
                    cli_override=executor_override,
                    plan_default=plan_default,
                )
                for spec in launch_specs
            ]
            non_default = [
                (spec["task"], name)
                for spec, name in zip(launch_specs, resolved_executors)
                if name != DEFAULT_EXECUTOR
            ]
            if non_default:
                print(
                    f"\n{DIM}Executor routing: "
                    f"{len(non_default)}/{len(launch_specs)} task(s) on non-default runtime{RESET}"
                )
                for task_obj, name in non_default:
                    title = _clean(task_obj.get("title")) or "Untitled"
                    print(f"  {DIM}→ {title}: {name}{RESET}")
            # Warn when a task will run on Managed Agents (Claude) while the
            # user asked for Codex — the --agent choice is ignored for those
            # tasks since Managed Agents only runs Claude.
            if agent not in ("auto", "claude"):
                managed_mismatch = [
                    spec["task"]
                    for spec, name in zip(launch_specs, resolved_executors)
                    if name == MANAGED_AGENT
                ]
                if managed_mismatch:
                    titles = ", ".join(
                        _clean(t.get("title")) or "Untitled" for t in managed_mismatch
                    )
                    print(
                        f"{DIM}Warning: --agent {agent} is ignored for "
                        f"{len(managed_mismatch)} task(s) routed to managed_agent "
                        f"(Managed Agents only runs Claude): {titles}{RESET}"
                    )
            # Reuse a single executor instance per resolved name so that
            # executors which may hold connections or shared state (e.g. the
            # managed-agent path) aren't re-allocated per task in the wave.
            _executor_cache: dict[str, Any] = {}

            def _get_executor(name: str):
                cached = _executor_cache.get(name)
                if cached is None:
                    cached = build_executor(name, launch=_launch_single_agent)
                    _executor_cache[name] = cached
                return cached

            agent_coros = [
                _get_executor(name).run_task(
                    spec["task"],
                    plan,
                    resolved_plan_id,
                    exec_dir,
                    len(actionable),
                    semaphore,
                    api_client,
                    session_id=str(spec["session_id"]),
                    agent=agent,
                    visible=visible,
                    launch_index=i,
                    use_local_repo_context=use_local_repo_context,
                    worktree_policy=plan_worktree_policy,
                    pr_policy=plan_pr_policy,
                )
                for i, (spec, name) in enumerate(
                    zip(launch_specs, resolved_executors)
                )
            ]

            # Background poller — prints new notes/status as agents work
            # Seed with existing note counts so we only show genuinely new notes
            _seen_notes: dict[str, int] = {}
            for _t in actionable:
                _tid = _t.get("id", "")
                _existing_notes = (_t.get("notes") or "").strip()
                if _existing_notes:
                    _seen_notes[_tid] = len(
                        [
                            note_line
                            for note_line in _existing_notes.split("\n")
                            if note_line.strip()
                        ]
                    )
            _poller_done = False

            async def _poll_progress():
                import time as _poll_time

                def _print_poll_progress(message: str) -> None:
                    if single_task_label and not visible and _stdout_is_tty():
                        width = shutil.get_terminal_size(fallback=(100, 24)).columns
                        print("\r" + (" " * max(20, width - 1)) + "\r", end="", flush=True)
                    print(message)

                _start_time = _poll_time.monotonic()
                _last_heartbeat = 0  # seconds since last heartbeat message
                _seen_sessions: dict[str, tuple] = {}
                _saw_progress_update = False
                _last_observed_activity = _start_time
                _last_snapshot_signature = ""
                _last_snapshot_at = 0.0
                _POLL_INTERVAL_SECONDS = 3
                _SINGLE_TASK_HEARTBEAT_INTERVAL_SECONDS = (
                    12 if inside_agent_session else 45
                )
                _MULTI_TASK_HEARTBEAT_INTERVAL_SECONDS = (
                    8 if inside_agent_session else 10
                )
                try:
                    while not _poller_done:
                        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                        if _poller_done:
                            break
                        elapsed = _poll_time.monotonic() - _start_time
                        idle_elapsed = _poll_time.monotonic() - _last_observed_activity
                        heartbeat_interval = (
                            _SINGLE_TASK_HEARTBEAT_INTERVAL_SECONDS
                            if single_task_label
                            else _MULTI_TASK_HEARTBEAT_INTERVAL_SECONDS
                        )
                        try:
                            async with make_async_client(
                                _state.api_url, _state.token
                            ) as poll_client:
                                resp = await poll_client.get(
                                    f"/v1/plans/{resolved_plan_id}"
                                )
                            if not resp.is_success:
                                # Heartbeat even if poll fails
                                if idle_elapsed >= heartbeat_interval and elapsed - _last_heartbeat >= heartbeat_interval:
                                    mins = int(elapsed // 60)
                                    secs = int(elapsed % 60)
                                    time_str = (
                                        f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
                                    )
                                    if single_task_label:
                                        activity_text = (
                                            f"reviewed {len(headless_reviewed_files)} files; no edits yet"
                                            if headless_reviewed_files and not _saw_progress_update
                                            else (
                                                f"still working"
                                                if _saw_progress_update
                                                else "active; waiting for the first task note or file change"
                                            )
                                        )
                                        _print_poll_progress(
                                            f"  {DIM}[{single_task_label}]{RESET} {activity_text} ({time_str} elapsed)"
                                        )
                                    else:
                                        _print_poll_progress(
                                            f"  {DIM}⋯ {agent_label} still working ({time_str} elapsed){RESET}"
                                        )
                                    _last_heartbeat = elapsed
                                continue
                            fresh = resp.json()
                            snapshot = _plan_execution_snapshot(fresh)
                            if inside_agent_session:
                                snapshot_signature = "|".join(
                                    [
                                        snapshot,
                                        ",".join(
                                            sorted(
                                                _clean(session.get("session_id"))
                                                for session in fresh.get(
                                                    "agent_sessions", []
                                                )
                                                if _clean(session.get("status")).lower()
                                                in {"starting", "running", "in_progress"}
                                            )
                                        ),
                                    ]
                                )
                                if (
                                    snapshot_signature != _last_snapshot_signature
                                    or elapsed - _last_snapshot_at >= heartbeat_interval
                                ):
                                    _print_poll_progress(
                                        f"  {DIM}status:{RESET} {snapshot}"
                                    )
                                    _last_snapshot_signature = snapshot_signature
                                    _last_snapshot_at = elapsed
                                    _last_heartbeat = elapsed
                            for session in fresh.get("agent_sessions", []):
                                session_id = _clean(session.get("session_id"))
                                if not session_id:
                                    continue
                                status = _clean(session.get("status")).lower()
                                phase = _clean(session.get("current_phase"))
                                progress_message = _clean(session.get("progress_message"))
                                touched_files = tuple((session.get("touched_files") or [])[:3])
                                conflicting_files = tuple((session.get("conflicting_files") or [])[:3])
                                recent_errors = session.get("recent_errors") or []
                                latest_error = _clean(recent_errors[-1] if recent_errors else "")
                                if _should_ignore_agent_output_line(latest_error):
                                    latest_error = ""
                                signature = (
                                    status,
                                    phase,
                                    progress_message,
                                    touched_files,
                                    conflicting_files,
                                    latest_error,
                                )
                                if _seen_sessions.get(session_id) == signature:
                                    continue
                                _seen_sessions[session_id] = signature
                                label = (
                                    _clean(session.get("task_title"))
                                    or _clean(session.get("task_id"))
                                    or session_id
                                )
                                _print_poll_progress(
                                    f"  {DIM}[{label} · {session_id}]{RESET} {_summarize_agent_session_line(session)}"
                                )
                                _saw_progress_update = True
                                _last_observed_activity = _poll_time.monotonic()
                                _last_heartbeat = _poll_time.monotonic() - _start_time
                            for s in fresh.get("plan_steps", []):
                                sid = s.get("id", "")
                                status = (s.get("status") or "").lower()
                                if status != "in_progress":
                                    continue
                                notes = (s.get("notes") or "").strip()
                                if not notes:
                                    continue
                                note_lines = [
                                    note_line.strip()
                                    for note_line in notes.split("\n")
                                    if note_line.strip()
                                ]
                                prev_count = _seen_notes.get(sid, 0)
                                if len(note_lines) > prev_count:
                                    new_lines = note_lines[prev_count:]
                                    title = _clean(s.get("title")) or "?"
                                    for nl in new_lines:
                                        note_text = _format_task_note_for_terminal(nl)
                                        if not note_text:
                                            continue
                                        _print_poll_progress(
                                            f"  {DIM}[{title}]{RESET} {note_text}"
                                        )
                                    _saw_progress_update = True
                                    _last_observed_activity = _poll_time.monotonic()
                                    _last_heartbeat = (
                                        _poll_time.monotonic() - _start_time
                                    )
                                    _seen_notes[sid] = len(note_lines)
                            # Heartbeat after notes — only if no new notes appeared this cycle
                            _last_observed_activity = max(
                                _last_observed_activity,
                                last_headless_activity_at,
                            )
                            idle_elapsed = _poll_time.monotonic() - _last_observed_activity
                            if idle_elapsed >= heartbeat_interval and elapsed - _last_heartbeat >= heartbeat_interval:
                                mins = int(elapsed // 60)
                                secs = int(elapsed % 60)
                                time_str = (
                                    f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
                                )
                                if single_task_label:
                                    activity_text = (
                                        f"reviewed {len(headless_reviewed_files)} files; no edits yet"
                                        if headless_reviewed_files and not _saw_progress_update
                                        else (
                                            "still working"
                                            if _saw_progress_update
                                            else "active; waiting for the first task note or file change"
                                        )
                                    )
                                    print(
                                        f"  {DIM}[{single_task_label}]{RESET} {activity_text} ({time_str} elapsed)"
                                    )
                                else:
                                    print(
                                        f"  {DIM}⋯ {snapshot} ({time_str} elapsed){RESET}"
                                    )
                                _last_heartbeat = elapsed
                        except Exception:
                            pass
                except asyncio.CancelledError:
                    pass

            poller_task = asyncio.create_task(_poll_progress())

            # Stream results as each agent completes
            results: list[AgentResult | Exception] = []
            stopped_task_ids: set[str] = set()
            completed_count = 0
            agent_tasks = [asyncio.create_task(c) for c in agent_coros]
            total_agents = len(agent_tasks)
            _cancelled = False
            _cancel_requested = False
            _sigint_count = 0

            def _handle_sigint():
                nonlocal _sigint_count, _cancelled, _cancel_requested
                _sigint_count += 1
                remaining = total_agents - completed_count
                if _sigint_count == 1 and remaining == 1:
                    _cancel_requested = True
                    _cancelled = True
                    print(f"\n  {RED}Stopping 1 agent...{RESET}")
                    for t in agent_tasks:
                        if not t.done():
                            t.cancel()
                elif _sigint_count == 1:
                    _cancel_requested = True
                    running_label = "agent" if remaining == 1 else "agents"
                    print(f"\n  {YELLOW}⚠ {remaining} {running_label} still running.{RESET}")
                    print(f"  {YELLOW}Press Ctrl+C again to force stop, or wait for agents to finish.{RESET}")
                else:
                    _cancelled = True
                    stopping_label = "agent" if remaining == 1 else "agents"
                    print(f"\n  {RED}Force stopping {remaining} {stopping_label}...{RESET}")
                    for t in agent_tasks:
                        if not t.done():
                            t.cancel()

            loop = asyncio.get_event_loop()
            import signal
            loop.add_signal_handler(signal.SIGINT, _handle_sigint)
            try:
                for coro in asyncio.as_completed(agent_tasks):
                    try:
                        r = await coro
                        results.append(r)
                        completed_count += 1
                        stopped_by_user = _cancel_requested and r.exit_code == 0
                        if stopped_by_user:
                            stopped_task_ids.add(r.task_id)
                        if _cancelled and not stopped_by_user:
                            break
                        if stopped_by_user:
                            status = f"{YELLOW}stopped{RESET}"
                        elif r.exit_code == 0:
                            status = f"{GREEN}done{RESET}"
                        elif r.failure_kind in {"launch", "usage_limit"}:
                            status = f"{RED}failed to launch{RESET}"
                        else:
                            status = f"{RED}blocked{RESET}"
                        dur = f"{r.duration_seconds:.0f}s" if r.duration_seconds else ""
                        cost = f" ${r.cost_usd:.2f}" if r.cost_usd > 0 else ""
                        summary_line = (
                            f"  [{completed_count}/{total_agents}] {status}  {r.task_title}{DIM} {dur}{cost}{RESET}"
                        )
                        if r.exit_code != 0:
                            reason = _clean((r.stderr or r.stdout).splitlines()[0])
                            if reason:
                                summary_line += f"\n    {DIM}{reason}{RESET}"
                        print(summary_line)
                        if r.exit_code != 0 and r.failure_kind == "usage_limit":
                            _cancelled = True
                            remaining = total_agents - completed_count
                            print(
                                f"  {RED}Stopping remaining agents:{RESET} {r.task_title} failed because the selected agent hit its usage limit."
                            )
                            if remaining > 0:
                                for t in agent_tasks:
                                    if not t.done():
                                        t.cancel()
                                await _cleanup_active_agents()
                            break
                    except asyncio.CancelledError:
                        completed_count += 1
                        if _cancelled:
                            break
                    except Exception as exc:
                        results.append(exc)
                        completed_count += 1
                        print(
                            f"  [{completed_count}/{total_agents}] {RED}error{RESET}  {exc}"
                        )
            finally:
                loop.remove_signal_handler(signal.SIGINT)
                if _cancelled:
                    reason = "Execution stopped by user before task completion."
                    await asyncio.gather(
                        *(
                            _mark_agent_session_stopped_async(
                                api_client,
                                resolved_plan_id,
                                _clean(spec["task"].get("id")),
                                session_id=str(spec["session_id"]),
                                exec_dir=exec_dir,
                                reason=reason,
                                runtime_context=spec["runtime_context"]
                                if isinstance(spec["runtime_context"], dict)
                                else None,
                            )
                            for task_future, spec in zip(agent_tasks, launch_specs)
                            if not task_future.done()
                            and _clean(spec["task"].get("id"))
                        ),
                        return_exceptions=True,
                    )
                    await _cleanup_active_agents()

            _poller_done = True
            poller_task.cancel()
            try:
                await poller_task
            except asyncio.CancelledError:
                pass

        if _cancelled:
            print(f"\n{YELLOW}Cancelled.{RESET}")
            return

        succeeded = sum(
            1
            for r in results
            if not isinstance(r, Exception)
            and r.exit_code == 0
            and r.task_id not in stopped_task_ids
        )
        stopped = sum(
            1
            for r in results
            if not isinstance(r, Exception)
            and r.exit_code == 0
            and r.task_id in stopped_task_ids
        )
        failed = len(results) - succeeded - stopped
        wave_cost = sum(r.cost_usd for r in results if not isinstance(r, Exception))
        wave_tokens = sum(
            r.tokens_used for r in results if not isinstance(r, Exception)
        )

        cost_summary = (
            f"  cost: ${wave_cost:.2f} ({wave_tokens:,} tokens)"
            if wave_cost > 0
            else ""
        )
        summary_bits = [f"{GREEN}{succeeded} succeeded{RESET}"]
        if stopped > 0:
            summary_bits.append(f"{YELLOW}{stopped} stopped{RESET}")
        summary_bits.append(f"{RED}{failed} failed{RESET}")
        print(f"\n{', '.join(summary_bits)}{cost_summary}")

        # Check for blocked tasks and tell the user exactly what to do
        with make_client(_state.api_url, _state.token) as refresh_client:
            refreshed = refresh_client.get(f"/v1/plans/{resolved_plan_id}").json()
        refreshed_steps = refreshed.get("plan_steps") or []
        blocked_steps = [
            s for s in refreshed_steps if _clean(s.get("status")).lower() == "blocked"
        ]

        if blocked_steps:
            continue_arg = _execution_context_arg(plan, resolved_plan_id)
            print(f"\n{RED}{'─' * 50}{RESET}")
            print(
                f"{RED}{len(blocked_steps)} task(s) blocked — needs your attention:{RESET}\n"
            )
            for s in blocked_steps:
                tid = _clean(s.get("id")) or "?"
                title = _clean(s.get("title")) or "Untitled"
                reason = _clean(s.get("blocked_reason")) or "no reason given"
                print(f"  {RED}✗{RESET} {title}")
                print(f"    {DIM}Reason: {reason}{RESET}")
                print(
                    f"    {DIM}Unblock: keshro task unblock {tid} -p {resolved_plan_id}{RESET}"
                )
            print(
                f"\n{DIM}After unblocking, run: keshro continue -p {continue_arg}{RESET}"
            )
            print(f"{RED}{'─' * 50}{RESET}")

        refreshed_done = len(
            [
                s
                for s in refreshed_steps
                if _clean(s.get("status") or "").lower() == "completed"
            ]
        )
        refreshed_total = len(refreshed_steps)

        if not run_all:
            if failed == 0 and succeeded > 0:
                remaining = max(0, refreshed_total - refreshed_done)
                if remaining > 0:
                    print(
                        f"\n{DIM}{remaining} task(s) remaining.{RESET}\n"
                        f"{DIM}  keshro continue        — run the next task{RESET}\n"
                        f"{DIM}  keshro continue --all  — auto-continue through all remaining waves{RESET}"
                    )
                elif refreshed_total > 0:
                    print(f"\n{GREEN}All {refreshed_total} tasks completed.{RESET}")
            break

        # Re-fetch plan for next wave
        with make_client(_state.api_url, _state.token) as client:
            res = client.get(f"/v1/plans/{resolved_plan_id}")
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
                "/v1/auth/me",
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
    agent: str = "auto",
) -> None:
    import uuid as _uuid

    _ensure_authenticated()
    session_id = f"agent-{_uuid.uuid4().hex[:8]}"
    resolved_plan_id = _current_plan_id(plan_id)
    if not resolved_plan_id:
        raise SystemExit(
            "Execution context or migration ID required. Pass --plan-id <id> or save one with `keshro config set --plan-id <id>`."
        )
    work_dir, use_local_repo_context = _resolve_continue_work_dir(
        resolved_plan_id, work_dir, allow_prompt=False
    )
    plan = _get_plan_or_exit(resolved_plan_id)

    # Draft plan warning
    plan_status = _clean(plan.get("status") or "draft").lower()
    if plan_status == "draft" and not confirm:
        steps = sorted(plan.get("plan_steps") or [], key=lambda s: s.get("order", 0))
        migration_id = _clean(plan.get("migration_id"))
        continue_arg = _execution_context_arg(plan, resolved_plan_id)
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
        print(f"\n  To execute: keshro continue -p {continue_arg} --confirm\n")
        raise SystemExit(0)

    # Mark draft plan as active on first confirmed execution
    if plan_status == "draft" and confirm:
        try:
            with make_client(_state.api_url, _state.token) as client:
                client.patch(
                    f"/v1/plans/{resolved_plan_id}",
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
    if _stdout_is_tty():
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
        print(
            _build_continue_brief(
                plan,
                task,
                work_dir=work_dir,
                session_id=session_id,
            )
        )


def _confirm_implicit_continue_plan(
    resolved_plan_id: str, work_dir: str | None = None
) -> str:
    if _state.json or not _stdout_is_tty():
        return resolved_plan_id
    plan_label = _current_plan_label(work_dir=work_dir) or resolved_plan_id
    context_details = _load_plan_context_details(resolved_plan_id)
    is_migration = context_details.get("kind") == "migration"
    context_label = "migration" if is_migration else "plan"
    dashboard_url = (
        f"{_current_app_url()}/migrations/{context_details.get('migration_id')}"
        if is_migration and _clean(context_details.get("migration_id"))
        else f"{_current_app_url()}/plans/{resolved_plan_id}"
    )
    try:
        confirmed = typer.confirm(
            f"Continue with {context_label} '{plan_label}' ({resolved_plan_id})?\n"
            f"{DIM}Dashboard:{RESET} {CYAN}{dashboard_url}{RESET}",
            default=True,
        )
    except click.Abort:
        print()
        raise SystemExit(0)
    if confirmed:
        return resolved_plan_id
    print(
        f"{DIM}Enter a plan ID or migration ID to continue with a different execution context, or press Enter to cancel.{RESET}"
    )
    try:
        override = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(0)
    if not override:
        raise SystemExit(0)
    override_plan_id, override_title = _resolve_continue_override_context(override)
    if not override_plan_id:
        raise SystemExit(0)
    if not _state.json:
        label = override_title or override_plan_id
        print(f"{DIM}Using:{RESET} {label} ({override_plan_id})")
    return override_plan_id


def _resolve_continue_override_context(
    value: str | None,
) -> tuple[str | None, str | None]:
    explicit_id = _clean(value)
    if not explicit_id:
        return None, None
    with make_client(_state.api_url, _state.token) as client:
        plan_res = client.get(f"/v1/plans/{explicit_id}")
        if plan_res.status_code < 400:
            plan = plan_res.json()
            return explicit_id, _clean(plan.get("title")) or explicit_id

        migration_plan_res = client.get(f"/v1/migrations/{explicit_id}/plan")
        if migration_plan_res.status_code < 400:
            plan = migration_plan_res.json()
            plan_id = _clean(plan.get("id")) or explicit_id
            return plan_id, _clean(plan.get("title")) or plan_id

    raise SystemExit(
        f"Could not resolve '{explicit_id}' to an execution context or migration-linked context."
    )


def _view_task(plan_id: str | None, task_id: str) -> None:
    resolved_plan_id = _require_plan_context(plan_id)
    with make_client(_state.api_url, _state.token) as client:
        res = client.get(f"/v1/plans/{resolved_plan_id}")
        res.raise_for_status()
        plan = res.json()
        if _state.json:
            print_output(plan, True)
            return
        _print_task_detail(plan, task_id=task_id)


def _dependencies_met(step: dict, steps: list[dict]) -> bool:
    """Check if all dependencies for a task are terminally complete."""
    depends_on = step.get("depends_on") or []
    if not depends_on:
        return True
    step_statuses = {
        _clean(s.get("id")): _clean(s.get("status") or "todo").lower() for s in steps
    }
    return all(
        step_statuses.get(_clean(dep_id)) in {"completed", "done"}
        for dep_id in depends_on
    )


def _next_actionable_task(plan: dict, parallel: bool = False) -> dict | None:
    steps = sorted(plan.get("plan_steps") or [], key=lambda step: step.get("order", 0))
    if parallel:
        # In parallel mode: skip in_progress tasks (another agent owns them),
        # only pick up todo tasks whose dependencies are met.
        # _dependencies_met already rejects tasks with unmet or blocked dependencies.
        for step in steps:
            if _clean(step.get("status") or "todo").lower() != "todo":
                continue
            if not _dependencies_met(step, steps):
                continue
            return step
        return None
    # Default: resume in_progress work if its dependencies are still satisfied,
    # otherwise pick the first todo task whose dependencies are satisfied.
    for desired_status in ("in_progress", "todo"):
        match = next(
            (
                step
                for step in steps
                if _clean(step.get("status") or "todo").lower() == desired_status
                and _dependencies_met(step, steps)
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
        if _clean(s.get("status") or "todo").lower() in {"completed", "done"}
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


def _should_use_local_repo_context(
    plan_id: str | None, work_dir: str | None = None
) -> bool:
    if _clean(work_dir):
        return True
    saved_work_dir = _clean(load_auth().get("default_work_dir"))
    if not saved_work_dir:
        return False
    resolved = str(Path(saved_work_dir).resolve())
    saved_repo_root = _discover_repo_root(resolved)
    if saved_repo_root is None:
        return False
    linked_plan_id, _ = _resolve_repo_linked_plan(str(saved_repo_root.resolve()))
    return bool(_clean(linked_plan_id) and _clean(linked_plan_id) == _clean(plan_id))


def _resolve_continue_work_dir(
    plan_id: str | None, work_dir: str | None = None, *, allow_prompt: bool = False
) -> tuple[str | None, bool]:
    explicit_work_dir = _clean(work_dir)
    if explicit_work_dir:
        resolved = str(Path(explicit_work_dir).resolve())
        return resolved, True

    saved_work_dir = _clean(load_auth().get("default_work_dir"))
    if saved_work_dir:
        resolved = str(Path(saved_work_dir).resolve())
        saved_repo_root = _discover_repo_root(resolved)
        if saved_repo_root is not None:
            linked_plan_id, _ = _resolve_repo_linked_plan(str(saved_repo_root.resolve()))
            if _clean(linked_plan_id) and _clean(linked_plan_id) == _clean(plan_id):
                return str(saved_repo_root.resolve()), True

    cwd = str(Path.cwd().resolve())
    repo_root = _discover_repo_root(cwd)
    if repo_root is None:
        return None, False

    resolved_repo_root = str(repo_root.resolve())
    linked_plan_id, _ = _resolve_repo_linked_plan(resolved_repo_root)
    repo_link_matches = bool(
        _clean(linked_plan_id) and _clean(linked_plan_id) == _clean(plan_id)
    )
    if not allow_prompt or _state.json or not _stdout_is_tty():
        return None, False

    try:
        prompt_lines = [
            "Use the current working directory as execution context for this plan?",
            f"{DIM}Current directory:{RESET} {resolved_repo_root}",
        ]
        if repo_link_matches:
            prompt_lines.append(
                f"{DIM}Keshro found a repo link to this plan, but it may be stale.{RESET}"
            )
        use_current_repo = typer.confirm(
            "\n".join(prompt_lines),
            default=False,
        )
    except click.Abort:
        print()
        raise SystemExit(0)

    if use_current_repo:
        return resolved_repo_root, True
    return None, False


def _delete_task(
    plan_id: str | None,
    task_id: str,
    feedback_reason: str | None = None,
    assume_yes: bool = False,
) -> None:
    resolved_plan_id = _require_plan_context(plan_id)
    with make_client(_state.api_url, _state.token) as client:
        plan_res = client.get(f"/v1/plans/{resolved_plan_id}")
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
            f"/v1/plans/{resolved_plan_id}/tasks/{task_id}",
            json=payload,
        )
        res.raise_for_status()
        plan = res.json()
        if _state.json:
            print_output(plan, True)
            return
        print(f"Deleted task {task_id} from plan {resolved_plan_id}.")


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
    _state.token = token or load_auth().get("token") or None
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


def _detect_migration_intent(description: str) -> tuple[str, str, str | None] | None:
    """Ask the LLM whether a description is about a technology/platform migration.

    Returns (source_tech, target_tech, driver) or None.
    """
    try:
        client = make_client()
        resp = client.post(
            "/v1/plans/detect-migration",
            json={"description": description},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get("is_migration") and data.get("source") and data.get("target"):
            return (data["source"], data["target"], data.get("driver"))
        return None
    except Exception:
        return None


def _find_migration_template(source: str, target: str) -> str | None:
    """Try to find a matching migration template key for a source/target pair."""
    try:
        import re as _re

        def _normalize_name(value: str) -> str:
            cleaned = _clean(value).lower()
            if not cleaned:
                return ""
            for prefix in ("apache ", "amazon ", "aws ", "google ", "gcp "):
                if cleaned.startswith(prefix):
                    cleaned = cleaned[len(prefix) :].strip()
                    break
            cleaned = cleaned.replace("&", " and ")
            cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned).strip()
            return " ".join(token for token in cleaned.split() if token)

        def _name_variants(value: str) -> list[str]:
            raw = _clean(value)
            if not raw:
                return []
            variants: list[str] = []

            def _add_variant(candidate: str) -> None:
                normalized = _normalize_name(candidate)
                if normalized and normalized not in variants:
                    variants.append(normalized)

            _add_variant(raw)
            without_parens = _re.sub(r"\([^)]*\)", " ", raw)
            _add_variant(without_parens)
            before_dash = _re.split(r"\s[-:/]\s", raw, maxsplit=1)[0]
            _add_variant(before_dash)
            return variants

        def _matches_name(left: str, right: str) -> bool:
            left_variants = _name_variants(left)
            right_variants = _name_variants(right)
            return any(lv == rv for lv in left_variants for rv in right_variants)

        def _slug(v: str) -> str:
            return _re.sub(r"[^a-z0-9]+", "-", v.strip().lower()).strip("-")

        desired_source = _normalize_name(source)
        desired_target = _normalize_name(target)

        with make_client(_state.api_url, _state.token) as client:
            res = client.get("/v1/plans/templates")
            if res.status_code == 200:
                templates = res.json() or []
                for template in templates:
                    template_source = str(template.get("source_type") or "")
                    template_target = str(template.get("target_type") or "")
                    if _matches_name(template_source, source) and _matches_name(
                        template_target, target
                    ):
                        key = _clean(template.get("key"))
                        if key:
                            return key

            template_key = f"{_slug(desired_source)}-to-{_slug(desired_target)}"
            resp = client.get(
                "/v1/migrations/path-template/lookup",
                params={"template_key": template_key},
            )
            if resp.status_code == 200:
                data = resp.json()
                if data and data.get("template_key"):
                    return data["template_key"]

            resp = client.get(
                "/v1/migrations/path-template",
                params={"source_type": source, "target_type": target},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not data:
                return None
            key = data.get("template_key") or ""
            if data.get("is_auto_generated"):
                return None
            if key:
                return key
            return None
    except Exception:
        return None


def _resolve_explicit_migration_context(
    description: str,
    *,
    as_migration: bool,
    source_type: str | None,
    target_type: str | None,
) -> tuple[str, str, str | None] | None:
    explicit_source = _clean(source_type)
    explicit_target = _clean(target_type)
    explicit_requested = as_migration or bool(explicit_source or explicit_target)

    if not explicit_requested:
        return None
    if bool(explicit_source) != bool(explicit_target):
        raise SystemExit(
            "Pass both --source-type and --target-type together when forcing migration mode."
        )
    if explicit_source and explicit_target:
        return explicit_source, explicit_target, None

    detected = _detect_migration_intent(description)
    if detected:
        if len(detected) == 2:
            source_tech, target_tech = detected
            return source_tech, target_tech, None
        source_tech, target_tech, driver = detected
        return source_tech, target_tech, driver

    raise SystemExit(
        "Could not determine the migration source and target from the provided context. "
        "Pass --source-type and --target-type, or use --template <template-key>."
    )


def _classify_source(source: str | None) -> tuple[str, str | None]:
    """Classify a positional source argument into (source_type, value).

    Returns one of:
      ("directory", None)          — use cwd
      ("directory", resolved_path) — local path
      ("github_repo", url)         — GitHub repo URL
      ("github_issue", url)        — GitHub issue URL
      ("linear", url)              — Linear issue URL
      ("jira", url)                — Jira URL
      ("url", url)                 — any other URL
    """
    if not source:
        return ("directory", None)

    # URLs
    if source.startswith("http://") or source.startswith("https://"):
        # GitHub issue: https://github.com/{owner}/{repo}/issues/{num}
        if re.match(r"https://github\.com/[^/]+/[^/]+/issues/\d+", source):
            return ("github_issue", source)
        # GitHub repo: https://github.com/{owner}/{repo} with optional /tree/... or /blob/...
        if re.match(r"https://github\.com/[^/]+/[^/]+(/tree/.*|/blob/.*)?$", source):
            return ("github_repo", source)
        # Linear
        if source.startswith("https://linear.app/"):
            return ("linear", source)
        # Jira
        if "/browse/" in source or "/jira/" in source:
            return ("jira", source)
        # Any other URL
        return ("url", source)

    # Local paths
    if source.startswith("/") or source.startswith("./") or source.startswith(".."):
        return ("directory", str(Path(source).resolve()))
    # Check if it looks like a relative path that exists
    if os.path.exists(source):
        return ("directory", str(Path(source).resolve()))

    # Default: assume it's a path
    return ("directory", source)


@app.command("create")
def _create_migration(
    source: Annotated[
        Optional[str],
        typer.Argument(
            help="Directory, GitHub URL, Linear URL, or any URL. Defaults to current directory.",
        ),
    ] = None,
    template: Annotated[
        Optional[str],
        typer.Option(
            "--template",
            "-t",
            help="Migration template key, e.g. aws-batch-to-airflow. Run 'keshro migration templates' to see all templates.",
        ),
    ] = None,
    field: Annotated[
        Optional[list[str]],
        typer.Option(
            "--field",
            help="Advanced template field override.",
            hidden=True,
        ),
    ] = None,
    answer: Annotated[
        Optional[list[str]],
        typer.Option(
            "--answer",
            help="Internal agent resume option for clarifier answers.",
            hidden=True,
        ),
    ] = None,
    answers_file: Annotated[
        Optional[str],
        typer.Option(
            "--answers-file",
            help="Internal agent resume option for clarifier answers.",
            hidden=True,
        ),
    ] = None,
    context: Annotated[
        Optional[str], typer.Option("--context", "-c", help="Additional context.")
    ] = None,
    context_file: Annotated[
        Optional[str],
        typer.Option(
            "--context-file",
            "-f",
            help="Read additional context from a file.",
        ),
    ] = None,
    github_url: Annotated[
        Optional[str], typer.Option("--github-url", "-g", help="GitHub URL to attach.")
    ] = None,
    resource_url: Annotated[
        Optional[str],
        typer.Option("--resource-url", "-u", help="Reference URL to attach."),
    ] = None,
    org_id: Annotated[
        Optional[str],
        typer.Option(
            "--org-id",
            "-o",
            help="Create under an org by ID. Defaults to your saved org context (see `keshro config`). Use --personal to force personal scope.",
        ),
    ] = None,
    org: Annotated[
        Optional[str],
        typer.Option(
            "--org",
            help="Create under an org by name (resolved via the orgs API).",
        ),
    ] = None,
    personal: Annotated[
        bool,
        typer.Option(
            "--personal",
            help="Force personal (user-level) scope, ignoring any saved default org.",
        ),
    ] = False,
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
    skip_questions: Annotated[
        bool,
        typer.Option(
            "--skip-questions",
            help="Advanced option to skip clarifying questions.",
            hidden=True,
        ),
    ] = False,
    as_migration: Annotated[
        bool,
        typer.Option(
            "--as-migration",
            "-m",
            help="Force migration mode. Detects source/target from context unless --source-type and --target-type are provided explicitly.",
        ),
    ] = False,
    source_type_override: Annotated[
        Optional[str],
        typer.Option(
            "--source-type",
            help="Advanced migration source override.",
            hidden=True,
        ),
    ] = None,
    target_type_override: Annotated[
        Optional[str],
        typer.Option(
            "--target-type",
            help="Advanced migration target override.",
            hidden=True,
        ),
    ] = None,
    agent: Annotated[
        str,
        typer.Option(
            "--agent",
            "-a",
            help="Coding agent to use for discovery and clarifying questions: auto, claude, or codex.",
        ),
    ] = "auto",
):
    """Create a migration or project from a repo, issue, URL, or freeform request."""
    if template and (
        as_migration or _clean(source_type_override) or _clean(target_type_override)
    ):
        raise SystemExit(
            "--template already selects migration mode. Do not combine it with --as-migration, --source-type, or --target-type."
        )

    file_context = _read_context_file(context_file)
    if file_context:
        context = "\n\n".join(
            part for part in [context, file_context] if part and part.strip()
        )

    # Classify the positional source argument and set defaults
    source_type, source_value = _classify_source(source)
    explicit_work_dir = False
    if work_dir is not None:
        explicit_work_dir = True
    if source_type == "directory" and work_dir is None:
        explicit_work_dir = bool(source_value)
        work_dir = source_value or "."
    elif source_type == "github_repo" and repo_url is None:
        repo_url = source_value
    elif source_type == "github_issue":
        if github_url is None:
            github_url = source_value
        if resource_url is None:
            resource_url = source_value
    elif source_type in ("linear", "jira", "url"):
        if resource_url is None:
            resource_url = source_value
    import tempfile

    # Clone remote repo if needed
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
        provided_clarifier_answers = _parse_field_assignments(answer)
        (
            file_answers,
            resume_questions,
            resume_enrichment_context,
            resume_seed_answers,
        ) = _load_answer_file_bundle(answers_file)
        provided_clarifier_answers.update(file_answers)
        context_entered_interactively = False
        # Resolve the scope (org or personal) once, up front, so it shows in
        # the banner AND flows through to backend creation calls. Without this
        # the saved default_org_id never reaches `--org-id`-style code paths
        # and every keshro create lands at the user level even when the user
        # set an org default via `keshro config set --org`.
        if personal and (org_id or org):
            raise SystemExit(
                "Cannot combine --personal with --org/--org-id. Pick one."
            )
        resolved_org_id, scope_label = _resolve_creation_scope(
            org_id, org, force_personal=personal
        )
        org_id = resolved_org_id
        if template:
            if not _state.json:
                print(f"{DIM}Using {_prompt_agent_display_name(agent)}{RESET}\n")
                _print_creation_scope_banner(scope_label)
            # Migration mode — use the existing migration flow
            answers = _parse_field_assignments(field)
            return _create_migration_inner(
                template,
                answers,
                context,
                github_url,
                resource_url,
                org_id,
                work_dir,
                clarifier_answers=provided_clarifier_answers,
                resume_questions=resume_questions,
                seed_answers=resume_seed_answers,
                answers_file_path=answers_file,
                skip_questions=skip_questions,
                prompt_for_context=not (
                    answers_file or bool(context and context.strip())
                ),
                agent=agent,
            )
        else:
            # Generic project mode — scan, get questions, agent answers, generate plan
            _ensure_authenticated()
            resolved_work_dir = str(Path(work_dir or ".").resolve())
            if not _state.json:
                print(f"{DIM}Using {_prompt_agent_display_name(agent)}{RESET}\n")
                _print_creation_scope_banner(scope_label)

            # If no description provided, prompt for one
            has_description = bool(
                (context and context.strip())
                or resource_url
                or (
                    source_type in ("github_issue", "linear", "jira", "url")
                    and source_value
                )
            )
            file_context = _read_context_file(context_file) if context_file else None
            if file_context:
                has_description = True

            if not has_description:
                if sys.stdout.isatty():
                    print(f"{CYAN}What do you want to do in this project?{RESET}")
                    print(
                        f'{DIM}Describe the work — e.g. "migrate Express to Fastify", '
                        f'"add auth with NextAuth", "extract billing into a service"{RESET}'
                    )
                    try:
                        user_input = input(f"\n{CYAN}>{RESET} ").strip()
                    except (EOFError, KeyboardInterrupt):
                        raise SystemExit(0)
                    if not user_input:
                        raise SystemExit(
                            "No description provided. Usage:\n"
                            '  keshro create --context "migrate Express to Fastify"\n'
                            "  keshro create https://github.com/org/repo/issues/42"
                        )
                    context = user_input
                    context_entered_interactively = True
                else:
                    raise SystemExit(
                        "No project description provided. Pass one of:\n"
                        '  keshro create --context "what you want to do"\n'
                        "  keshro create --context-file prd.md\n"
                        "  keshro create https://github.com/org/repo/issues/42\n"
                        "  keshro create https://linear.app/team/issue/PROJ-123"
                    )

            if not context_entered_interactively:
                context = _prompt_for_optional_cli_context("this project", context)

            # Step 1: Collect codebase context if we have a directory
            discovered_context = None
            explicit_scan_target = explicit_work_dir or bool(repo_url)
            if os.path.isdir(resolved_work_dir) and _should_scan_default_work_dir(
                resolved_work_dir, explicit_target=explicit_scan_target
            ):
                if not _state.json:
                    print(f"{CYAN}Using project: {resolved_work_dir}{RESET}")
                discovered_context = _collect_generic_discovery(resolved_work_dir)
            elif os.path.isdir(resolved_work_dir):
                if not _state.json:
                    print(
                        f"{YELLOW}Skipping repo scan for {resolved_work_dir} because it does not look like the relevant project root.{RESET}"
                    )
            elif not _state.json:
                source_label = source_value or resolved_work_dir
                print(f"{CYAN}Creating project from: {source_label}{RESET}")

            # Build description from context + source info
            desc_parts = []
            if context:
                desc_parts.append(context)
            if resource_url:
                desc_parts.append(f"Reference: {resource_url}")
            description = "\n\n".join(desc_parts)

            explicit_migration = _resolve_explicit_migration_context(
                description,
                as_migration=as_migration,
                source_type=source_type_override,
                target_type=target_type_override,
            )
            if explicit_migration:
                source_tech, target_tech, detected_driver = explicit_migration
                template_key = _find_migration_template(source_tech, target_tech)
                if not template_key:
                    if not _state.json:
                        print(
                            f"{DIM}No specific template found for {source_tech} → {target_tech}. "
                            f"Continuing as a custom migration path.{RESET}\n"
                        )
                        app_url = _current_app_url()
                        print(
                            f"{DIM}Browse templates: {app_url}/templates\n"
                            f"Compare targets: {app_url}/migrate{RESET}\n"
                        )
                    return _create_custom_migration_inner(
                        source_tech,
                        target_tech,
                        context,
                        github_url,
                        resource_url,
                        org_id,
                        work_dir,
                        clarifier_answers=provided_clarifier_answers,
                        resume_questions=resume_questions,
                        seed_answers=resume_seed_answers,
                        answers_file_path=answers_file,
                        skip_questions=skip_questions,
                        prompt_for_context=not context_entered_interactively,
                        agent=agent,
                    )
                if not _state.json:
                    # Restate scope right next to the template match so users
                    # never create a migration under the wrong scope. The
                    # earlier "Creating in org:" banner can get collapsed in
                    # agent-wrapped output; this line rides alongside the
                    # most prominent progress marker they'll see.
                    _print_creation_scope_banner(scope_label)
                    print(
                        f"{GREEN}Matched template:{RESET} {source_tech} → {target_tech} "
                        f"{DIM}(pre-built discovery, risks, and field definitions){RESET}\n"
                    )
                answers = _parse_field_assignments(field)
                if detected_driver:
                    answers["__detected_driver"] = detected_driver
                return _create_migration_inner(
                    template_key,
                    answers,
                    context,
                    github_url,
                    resource_url,
                    org_id,
                    work_dir,
                    clarifier_answers=provided_clarifier_answers,
                    resume_questions=resume_questions,
                    seed_answers=resume_seed_answers,
                    answers_file_path=answers_file,
                    skip_questions=skip_questions,
                    prompt_for_context=not context_entered_interactively,
                    agent=agent,
                )

            # Detect migration intent and offer the migration pipeline
            migration_match = _detect_migration_intent(description)
            if migration_match and not template:
                if len(migration_match) == 2:
                    source_tech, target_tech = migration_match
                    detected_driver = None
                else:
                    source_tech, target_tech, detected_driver = migration_match
                if _inside_coding_agent():
                    template_key = _find_migration_template(source_tech, target_tech)
                    _exit_for_agent_migration_confirmation(
                        source_tech=source_tech,
                        target_tech=target_tech,
                        template_key=template_key,
                        context=context or description,
                        agent=agent,
                    )
                if not _state.json:
                    print(
                        f"\n{CYAN}This looks like a migration ({source_tech} → {target_tech}).{RESET}"
                    )
                    print(
                        f"{DIM}Keshro has enhanced analysis for migrations — "
                        f"repo-based input discovery, migration-specific follow-up questions, "
                        f"risk and cost estimates, and step-by-step tasks.{RESET}\n"
                    )
                    migration_choice = "Treat as migration"
                    general_choice = "Treat as a general project"
                    print(f"  1. {migration_choice} {GREEN}(recommended){RESET}")
                    print(f"  2. {general_choice}")
                    try:
                        choice = input(f"\n  {CYAN}>{RESET} ").strip()
                    except (EOFError, KeyboardInterrupt):
                        choice = migration_choice
                    choice = _resolve_menu_choice(
                        choice,
                        [migration_choice, general_choice],
                        aliases={
                            "y": migration_choice,
                            "yes": migration_choice,
                            "n": general_choice,
                            "no": general_choice,
                        },
                    )
                    if choice != general_choice:
                        # Try to find a matching template
                        template_key = _find_migration_template(
                            source_tech, target_tech
                        )
                        if template_key:
                            if not _state.json:
                                print(
                                    f"{GREEN}Matched template:{RESET} {source_tech} → {target_tech} "
                                    f"{DIM}(pre-built discovery, risks, and field definitions){RESET}\n"
                                )
                            answers = _parse_field_assignments(field)
                            if detected_driver:
                                answers["__detected_driver"] = detected_driver
                            return _create_migration_inner(
                                template_key,
                                answers,
                                context,
                                github_url,
                                resource_url,
                                org_id,
                                work_dir,
                                clarifier_answers=provided_clarifier_answers,
                                resume_questions=resume_questions,
                                seed_answers=resume_seed_answers,
                                answers_file_path=answers_file,
                                skip_questions=skip_questions,
                                prompt_for_context=not context_entered_interactively,
                                agent=agent,
                            )
                        else:
                            if not _state.json:
                                print(
                                    f"{DIM}No specific template found for {source_tech} → {target_tech}. "
                                    f"Continuing as a custom migration path.{RESET}\n"
                                )
                                app_url = _current_app_url()
                                print(
                                    f"{DIM}Browse templates: {app_url}/templates\n"
                                    f"Compare targets: {app_url}/migrate{RESET}\n"
                                )
                            return _create_custom_migration_inner(
                                source_tech,
                                target_tech,
                                context,
                                github_url,
                                resource_url,
                                org_id,
                                work_dir,
                                clarifier_answers=provided_clarifier_answers,
                                resume_questions=resume_questions,
                                seed_answers=resume_seed_answers,
                                answers_file_path=answers_file,
                                skip_questions=skip_questions,
                                prompt_for_context=not context_entered_interactively,
                                agent=agent,
                            )

            client = make_client()

            # Step 2: Get clarifying questions from the preview endpoint
            questions: list[dict] = list(resume_questions)
            enrichment_context = resume_enrichment_context
            if not skip_questions and not questions:
                if not _state.json:
                    print(f"{CYAN}Generating clarifying questions...{RESET}")

                preview_payload: dict[str, Any] = {
                    "description": description,
                    "discovered_context": discovered_context,
                }
                if org_id:
                    preview_payload["org_id"] = org_id
                try:
                    resp = client.post(
                        "/v1/plans/describe/preview",
                        json=preview_payload,
                        timeout=60,
                    )
                    resp.raise_for_status()
                    preview = resp.json()
                    questions = preview.get("questions", [])
                    enrichment_context = preview.get("enrichment_context", "")
                    applied_context = preview.get("applied_context")
                    if applied_context:
                        _print_applied_context_banner(applied_context)
                except Exception as exc:
                    if not _state.json:
                        print(f"{YELLOW}Could not generate questions: {exc}{RESET}")

            # Step 3: Hand questions straight to the user with discovery's
            # repo-scan pre-fills baked into suggested_answers. Same change
            # made to the migration flow — discovery already had its shot at
            # every question; calling the agent again wastes 1-2 minutes
            # without producing new info, since the agent has the same
            # context the discovery pass had.
            answered: dict[str, str] = dict(provided_clarifier_answers)
            if questions and _inside_coding_agent():
                missing_ids = _missing_question_ids(questions, answered)
                preset = _preset_answers_from_repo_scan(questions)
                suggested_answers: dict[str, str] = {**preset, **answered}
                if missing_ids:
                    rerun_command = (
                        f"keshro create --context {shlex.quote(context or description)}"
                    )
                    if agent != "auto":
                        rerun_command += f" --agent {shlex.quote(agent)}"
                    _exit_for_agent_clarifier_feedback(
                        heading="Keshro needs user answers before it can generate this plan.",
                        questions=questions,
                        suggested_answers=suggested_answers,
                        rerun_command=rerun_command,
                        enrichment_context=enrichment_context,
                    )
                if not _state.json:
                    accepted_count = sum(
                        1 for v in answered.values() if v and v.lower() != "unknown"
                    )
                    preset_count = sum(
                        1 for v in preset.values() if v and v.lower() != "unknown"
                    )
                    print(
                        f"  Pre-filled {preset_count}/{len(questions)} answers from repo scan; accepted {accepted_count}/{len(questions)}."
                    )
            elif questions and sys.stdout.isatty() and not _state.json:
                # TTY mode — let the user answer interactively
                print(
                    f"\n{CYAN}Clarifying questions ({len(questions)}) — press Enter to skip any{RESET}"
                )
                for qi, q in enumerate(questions, 1):
                    qtext = q.get("question", "")
                    why = q.get("why_this_matters", "")
                    options = q.get("answers", [])
                    input_mode = q.get("input_mode", "select")
                    print(f"\n  {CYAN}{qi}.{RESET} {qtext}")
                    if why:
                        print(f"     {DIM}{why}{RESET}")
                    if options:
                        for oi, opt in enumerate(options, 1):
                            rec = (
                                f" {GREEN}(recommended){RESET}"
                                if opt.get("recommended")
                                else ""
                            )
                            print(
                                f"     {DIM}{oi}. {opt.get('answer_title', opt.get('value', ''))}{rec}{RESET}"
                            )
                        if input_mode != "free_text":
                            # Select questions show options + custom hint
                            print(
                                f"     {DIM}Or type your own answer{RESET}"
                            )
                    if input_mode == "free_text":
                        placeholder = q.get("placeholder", "")
                        if placeholder:
                            print(f"     {DIM}{placeholder}{RESET}")
                    try:
                        answer = input(f"  {CYAN}>{RESET} ").strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        break
                    if answer:
                        # If user typed a number and there are options, use the option value
                        if options and answer.isdigit():
                            idx = int(answer) - 1
                            if 0 <= idx < len(options):
                                answer = options[idx].get("value", answer)
                        answered[q.get("id", f"q{qi}")] = answer
                answered_count = sum(1 for v in answered.values() if v)
                if answered_count:
                    print(f"\n  {DIM}{answered_count}/{len(questions)} answered{RESET}")
                print()

            # Step 4: Generate the plan

            # Fold enrichment context + answered questions into the description
            full_description = description
            if enrichment_context:
                full_description += f"\n\n{enrichment_context}"
            if answered:
                qa_text = "\n".join(
                    f"Q: {q.get('question', '')}\nA: {answered.get(q.get('id', ''), 'skipped')}"
                    for q in questions
                )
                full_description += f"\n\n---\nClarifying question answers:\n{qa_text}"

            generate_payload: dict[str, Any] = {
                "description": full_description,
                "project_type": "generic",
                "discovered_context": discovered_context,
            }
            if org_id:
                generate_payload["org_id"] = org_id

            # Extract any clarifier answers whose IDs map to known team/cost
            # fields so the backend can persist them to org/user (closes the
            # loop on saved-context reuse for projects). When the LLM-generated
            # clarifier doesn't emit team/cost-shaped questions today, these
            # dicts stay empty and writeback is a no-op — but this keeps the
            # plumbing in place for when project_questions does start emitting
            # structured team/cost fields.
            team_payload = {
                key: answered[key]
                for key in _TEAM_CONTEXT_LABELS
                if key in answered and answered[key]
            }
            cost_payload = {
                key: answered[key]
                for key in _COST_CONTEXT_LABELS
                if key in answered and answered[key]
            }
            if team_payload:
                generate_payload["team_context"] = team_payload
            if cost_payload:
                generate_payload["cost_context"] = cost_payload

            with _Spinner("Generating execution plan..."):
                try:
                    resp = client.post(
                        "/v1/plans/generate", json=generate_payload, timeout=120
                    )
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    _print_http_error(exc)
                    raise typer.Exit(1) from exc

            plan = resp.json()
            plan_id = plan.get("id", "")
            steps = plan.get("plan_steps", [])

            _set_default_plan_after_create(plan)
            _delete_answers_file(answers_file)

            if _state.json:
                print_output(plan)
            else:
                print(f"\n{GREEN}Plan created: {plan.get('title', 'Untitled')}{RESET}")
                print(f"  ID: {plan_id}")
                print(f"  Tasks: {len(steps)}")
                print(f"  Status: {plan.get('status', 'draft')}")
                app_url = _app_url_from_api_url(_state.api_url)
                print(f"\n  {DIM}Dashboard: {app_url}/plans/{plan_id}{RESET}")
                print(f"  Run {CYAN}keshro continue{RESET} to start executing.")

    finally:
        if clone_dir:
            import shutil as _shutil

            _shutil.rmtree(clone_dir, ignore_errors=True)


_GRAPHIFY_PREFERRED_SECTION_KEYS = (
    "summary",
    "god nodes",
    "community",
    "communities",
    "surprising",
    "connections",
)
_GRAPHIFY_MAX_SECTION_CHARS = 1500
_GRAPHIFY_MAX_TOTAL_CHARS = 4500
_GRAPHIFY_REPORT_RELATIVE_PATH = Path("graphify-out") / "GRAPH_REPORT.md"  # legacy, checked first
_GRAPHIFY_TIMEOUT_SECONDS = 90


def _run_graphify(work_dir: str) -> Path | None:
    import tempfile

    # Check legacy in-repo path first (for backward compat)
    legacy_path = Path(work_dir) / _GRAPHIFY_REPORT_RELATIVE_PATH
    if legacy_path.is_file():
        return legacy_path

    # Check temp cache
    work_hash = hashlib.sha256(str(Path(work_dir).resolve()).encode()).hexdigest()[:12]
    temp_dir = Path(tempfile.gettempdir()) / "keshro-graphify" / work_hash
    cached_report = temp_dir / "GRAPH_REPORT.md"
    if cached_report.is_file():
        return cached_report

    try:
        from graphify.extract import collect_files, extract
        from graphify.build import build
        from graphify.cluster import cluster, score_all
        from graphify.analyze import god_nodes, surprising_connections
        from graphify.detect import detect as run_detect
        from graphify.report import generate
    except ImportError:
        return None

    try:
        import json as _json

        root = Path(work_dir)
        files = collect_files(root)
        if not files:
            return None
        extraction = extract(files)
        if not extraction.get("nodes"):
            return None
        G = build([extraction])
        result = cluster(G)
        communities, labels = (
            (result, {}) if not isinstance(result, tuple) else result
        )
        scores = score_all(G, communities)
        gods = god_nodes(G)
        surprises = surprising_connections(G)
        detection = run_detect(root)
        token_cost = {"before": 0, "after": 0}
        rpt = generate(
            G, communities, scores, labels, gods, surprises,
            detection, token_cost, str(root),
        )
        temp_dir.mkdir(parents=True, exist_ok=True)
        cached_report.write_text(rpt, encoding="utf-8")
        graph_path = temp_dir / "graph.json"
        graph_path.write_text(
            _json.dumps(extraction, default=str), encoding="utf-8"
        )
        return cached_report
    except Exception:
        return None


def _read_graphify_report(report_path: Path) -> str | None:
    try:
        report = report_path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return None
    return report or None


def _select_graphify_sections(report: str) -> str:
    lines = report.splitlines()
    sections: list[tuple[str, str]] = []
    current_heading = "Overview"
    current_lines: list[str] = []

    for line in lines:
        if line.startswith("#"):
            body = "\n".join(current_lines).strip()
            if body:
                sections.append((current_heading, body))
            current_heading = line.lstrip("#").strip() or "Overview"
            current_lines = []
            continue
        current_lines.append(line)

    body = "\n".join(current_lines).strip()
    if body:
        sections.append((current_heading, body))

    if not sections:
        return report[:_GRAPHIFY_MAX_TOTAL_CHARS].strip()

    preferred: list[tuple[str, str]] = []
    for heading, body in sections:
        normalized = heading.lower()
        if any(key in normalized for key in _GRAPHIFY_PREFERRED_SECTION_KEYS):
            preferred.append((heading, body))

    chosen = preferred or sections
    selected_chunks: list[str] = []
    total = 0
    for heading, body in chosen:
        chunk = f"## {heading}\n{body[:_GRAPHIFY_MAX_SECTION_CHARS].strip()}".strip()
        if not chunk:
            continue
        chunk_len = len(chunk)
        if selected_chunks and total + chunk_len > _GRAPHIFY_MAX_TOTAL_CHARS:
            break
        if not selected_chunks and chunk_len > _GRAPHIFY_MAX_TOTAL_CHARS:
            return chunk[:_GRAPHIFY_MAX_TOTAL_CHARS].strip()
        selected_chunks.append(chunk)
        total += chunk_len
    return "\n\n".join(selected_chunks).strip()


def _collect_graphify_context(work_dir: str) -> str | None:
    report_path = _run_graphify(work_dir)
    if not report_path or not report_path.is_file():
        return None
    report = _read_graphify_report(report_path)
    # Clean up temp graphify files after reading
    try:
        import tempfile as _tf

        temp_base = Path(_tf.gettempdir()) / "keshro-graphify"
        if report_path.resolve().is_relative_to(temp_base.resolve()):
            shutil.rmtree(report_path.parent, ignore_errors=True)
    except Exception:
        pass
    if not report:
        return None
    snippet = _select_graphify_sections(report)
    if not snippet:
        return None
    return "\n".join(
        [
            "Graphify repo graph context added.",
            "Selected planning-relevant sections:",
            snippet,
        ]
    )


def _collect_generic_discovery(work_dir: str) -> str | None:
    """Collect basic project facts from a directory for plan enrichment."""
    facts = []

    # Check for common project files
    project_files = [
        ("package.json", "Node.js project"),
        ("requirements.txt", "Python project"),
        ("pyproject.toml", "Python project"),
        ("go.mod", "Go project"),
        ("Cargo.toml", "Rust project"),
        ("pom.xml", "Java/Maven project"),
        ("build.gradle", "Java/Gradle project"),
        ("Gemfile", "Ruby project"),
    ]

    for filename, label in project_files:
        filepath = os.path.join(work_dir, filename)
        if os.path.exists(filepath):
            facts.append(f"Detected: {label} ({filename})")
            try:
                with open(filepath) as f:
                    content = f.read(4096)
                facts.append(f"Contents of {filename}:\n{content}")
            except Exception:
                pass
            break

    # Directory listing
    try:
        entries = sorted(os.listdir(work_dir))
        top_entries = [e for e in entries if not e.startswith(".")][:30]
        if top_entries:
            facts.append(f"Top-level files/dirs: {', '.join(top_entries)}")
    except Exception:
        pass

    graphify_context = _collect_graphify_context(work_dir)
    if graphify_context:
        facts.append(graphify_context)

    return "\n\n".join(facts) if facts else None


def _should_scan_default_work_dir(
    work_dir: str, *, explicit_target: bool = False
) -> bool:
    if explicit_target:
        return True
    root = Path(work_dir)
    if not root.is_dir():
        return False
    try:
        visible_entries = [
            entry for entry in root.iterdir() if not entry.name.startswith(".")
        ]
    except OSError:
        return False
    repo_like_children = 0
    for entry in visible_entries[:30]:
        if not entry.is_dir():
            continue
        if (entry / ".git").exists():
            repo_like_children += 1
            continue
        if any(
            (entry / marker).exists()
            for marker in ("package.json", "pyproject.toml", "go.mod", "Cargo.toml")
        ):
            repo_like_children += 1
    if repo_like_children >= 2:
        return False
    strong_markers = {
        ".git",
        "package.json",
        "requirements.txt",
        "pyproject.toml",
        "go.mod",
        "Cargo.toml",
        "pom.xml",
        "build.gradle",
        "Gemfile",
        "Makefile",
        "Dockerfile",
        ".github",
    }
    if any((root / marker).exists() for marker in strong_markers):
        return True
    return repo_like_children == 0


def _answer_questions_via_agent(
    questions: list[dict],
    description: str,
    discovered_context: str | None,
    work_dir: str,
    agent: str = "auto",
) -> dict[str, str]:
    """Use the coding agent to answer clarifying questions about the project."""
    q_lines = []
    for q in questions:
        qid = q.get("id", "")
        text = q.get("question", "")
        why = q.get("why_this_matters", "")
        options = q.get("answers", [])
        q_lines.append(f"Question ID: {qid}")
        q_lines.append(f"Question: {text}")
        if why:
            q_lines.append(f"Why it matters: {why}")
        if options:
            opt_strs = [
                f"  - {o.get('value', '')}: {o.get('answer_title', '')}"
                + (" (recommended)" if o.get("recommended") else "")
                for o in options
            ]
            q_lines.append("Options:\n" + "\n".join(opt_strs))
        q_lines.append("")

    prompt = f"""You are answering clarifying questions about a software project to help generate a better execution plan.

Project description: {description}

{"Discovered codebase context:" + chr(10) + discovered_context if discovered_context else ""}

Answer each question below based on what you know about this project and codebase. If a question has options, pick the best one. If you genuinely don't know, answer "unknown".

Reply in this exact format — one line per question, with the question ID and your answer:
ANSWER <question_id>: <your answer>

Questions:
{chr(10).join(q_lines)}"""

    try:
        raw = _run_prompt_in_agent(
            prompt,
            missing_binary_message="Claude binary not found — skipping auto-answers.",
            failure_message_prefix="Agent failed to answer questions: ",
            empty_message="",
            work_dir=work_dir,
            agent=agent,
        )
    except SystemExit:
        return {}

    if not raw:
        return {}

    # Parse ANSWER lines
    answers: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if line.upper().startswith("ANSWER "):
            rest = line[7:]
            if ":" in rest:
                qid, answer = rest.split(":", 1)
                qid = qid.strip()
                answer = answer.strip()
                if answer.lower() != "unknown":
                    answers[qid] = answer
    return answers


def _create_migration_inner(
    path: str,
    answers: dict,
    context: str | None,
    github_url: str | None,
    resource_url: str | None,
    org_id: str | None,
    work_dir: str | None,
    clarifier_answers: dict[str, str] | None = None,
    resume_questions: list[dict] | None = None,
    seed_answers: dict[str, str] | None = None,
    answers_file_path: str | None = None,
    skip_questions: bool = False,
    prompt_for_context: bool = True,
    agent: str = "auto",
) -> None:
    with make_client(_state.api_url, _state.token) as client:
        template_res = client.get(
            "/v1/migrations/path-template/lookup", params={"template_key": path}
        )
        template_res.raise_for_status()
        template = template_res.json()
        source = _clean(template.get("source"))
        target = _clean(template.get("target"))

        resolved_work_dir = str(Path(work_dir).resolve()) if work_dir else None
        scan_target = "the current working directory"
        if resolved_work_dir:
            cwd = str(Path.cwd().resolve())
            scan_target = (
                "the current working directory"
                if resolved_work_dir == cwd
                else f"the project directory ({resolved_work_dir})"
            )

        saved_answers = {
            _clean(str(key)): _clean(str(value))
            for key, value in (seed_answers or {}).items()
            if _clean(str(key)) and _clean(str(value))
        }
        discovered_answer = _clean(
            saved_answers.pop("__keshro_discovered_context", "")
        )
        if not discovered_answer:
            with _Spinner(
                f"Analyzing {scan_target} and generating {source} -> {target} "
                "migration inputs and follow-up questions..."
            ):
                try:
                    discovered_answer = _collect_discovery_answer_from_claude(
                        template, work_dir=resolved_work_dir, agent=agent
                    )
                except SystemExit as exc:
                    if not _inside_coding_agent():
                        raise
                    discovered_answer = ""
                    _print_agent_collection_warning(
                        f"Skipping automatic migration discovery: {exc}"
                    )

        extracted = _extract_discovery_answers(template, discovered_answer)
        for key, value in saved_answers.items():
            if key not in answers or not answers[key]:
                answers[key] = value
        # Don't overwrite manually provided -f values with empty extracted values
        for key, value in extracted.items():
            if key not in answers or not answers[key]:
                answers[key] = value
        missing_required_fields = _missing_required_template_fields(template, answers)
        if missing_required_fields:
            try:
                repaired_answers = _repair_missing_required_discovery_answers(
                    template,
                    answers,
                    work_dir=resolved_work_dir,
                    agent=agent,
                )
            except SystemExit as exc:
                if not _inside_coding_agent():
                    raise
                repaired_answers = {}
                _print_agent_collection_warning(
                    f"Skipping required-field discovery repair: {exc}"
                )
            for key, value in repaired_answers.items():
                if key not in answers or not answers[key]:
                    answers[key] = value
        if not answers_file_path:
            answers = _prompt_for_migration_template_fields(template, answers)

        required_fields = [
            _clean(item.get("label")) or _clean(item.get("id"))
            for item in (template.get("fields") or [])
            if item.get("required") and not answers.get(_clean(item.get("id")))
        ]
        if required_fields and not _state.json:
            print(
                f"Some fields couldn't be discovered automatically: {', '.join(required_fields)}"
            )

        if prompt_for_context:
            context = _prompt_for_optional_cli_context(f"{source} -> {target}", context)
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
        if not skip_questions:
            clarifier_questions = list(resume_questions or [])
            if not clarifier_questions:
                with _Spinner(
                    "Checking for high-impact follow-up questions (this can take a bit)..."
                ):
                    clarifier_questions = _get_migration_clarifiers(client, payload)
            if clarifier_questions:
                suggested_answers: dict[str, str] = {}
                provided_clarifier_answers = dict(clarifier_answers or {})
                if provided_clarifier_answers:
                    missing_ids = _missing_question_ids(
                        clarifier_questions, provided_clarifier_answers
                    )
                    if missing_ids:
                        raise SystemExit(
                            "Missing --answer values for: " + ", ".join(missing_ids)
                        )
                if _inside_coding_agent():
                    if not provided_clarifier_answers:
                        # Discovery already had the agent's shot at every
                        # question. Repo-scan answers it found are surfaced
                        # as recommended options on the clarifier itself —
                        # for the rest, the user is the one with the missing
                        # info, so a second agent call doesn't add signal
                        # and costs ~1-2 min per create. Hand questions
                        # straight to the user with discovery's pre-fills.
                        suggested_answers = _preset_answers_from_repo_scan(
                            clarifier_questions
                        )
                        rerun_command = (
                            f"keshro create --template {shlex.quote(path)}"
                            f" --context {shlex.quote(context or '')}"
                        ).rstrip()
                        if agent != "auto":
                            rerun_command += f" --agent {shlex.quote(agent)}"
                        _exit_for_agent_clarifier_feedback(
                            heading="Keshro needs user answers before it can create this migration.",
                            questions=clarifier_questions,
                            suggested_answers=suggested_answers,
                            rerun_command=rerun_command,
                            initial_answers=dict(payload.get("custom_fields") or {}),
                        )
                    resolved_clarifier_answers = provided_clarifier_answers
                elif provided_clarifier_answers:
                    resolved_clarifier_answers = provided_clarifier_answers
                else:
                    resolved_clarifier_answers = _prompt_for_migration_clarifiers(
                        clarifier_questions, suggested_answers
                    )
                payload = _merge_clarifier_answers(
                    payload, clarifier_questions, resolved_clarifier_answers
                )
            elif not _state.json:
                print("No additional follow-up questions needed.")
    _create_migration_from_payload(payload, template, work_dir=resolved_work_dir)
    _delete_answers_file(answers_file_path)


def _create_custom_migration_inner(
    source: str,
    target: str,
    context: str | None,
    github_url: str | None,
    resource_url: str | None,
    org_id: str | None,
    work_dir: str | None,
    clarifier_answers: dict[str, str] | None = None,
    resume_questions: list[dict] | None = None,
    seed_answers: dict[str, str] | None = None,
    answers_file_path: str | None = None,
    skip_questions: bool = False,
    prompt_for_context: bool = True,
    agent: str = "auto",
) -> None:
    with make_client(_state.api_url, _state.token) as client:
        resolved_work_dir = str(Path(work_dir).resolve()) if work_dir else None
        saved_answers = {
            _clean(str(key)): _clean(str(value))
            for key, value in (seed_answers or {}).items()
            if _clean(str(key)) and _clean(str(value))
        }
        discovered_context = _clean(
            saved_answers.pop("__keshro_discovered_context", "")
        )
        if not discovered_context and resolved_work_dir and os.path.isdir(resolved_work_dir):
            if _should_scan_default_work_dir(
                resolved_work_dir, explicit_target=bool(work_dir)
            ):
                scan_target = (
                    "the current working directory"
                    if resolved_work_dir == str(Path.cwd().resolve())
                    else f"the project directory ({resolved_work_dir})"
                )
                with _Spinner(
                    f"Analyzing {scan_target} for {source} -> {target} migration context..."
                ):
                    discovered_context = _collect_generic_discovery(resolved_work_dir)

        if prompt_for_context:
            context = _prompt_for_optional_cli_context(f"{source} -> {target}", context)
        merged_context = f"CLI bootstrap for {source} -> {target}."
        if _clean(context):
            merged_context = f"{merged_context}\n\n{_clean(context)}"

        custom_fields: dict[str, str] = {}
        if discovered_context:
            custom_fields["__keshro_discovered_context"] = discovered_context
            merged_context = "\n\n".join(
                [
                    merged_context,
                    "Discovered project context",
                    discovered_context,
                ]
            )
        for key, value in saved_answers.items():
            if key not in custom_fields:
                custom_fields[key] = value

        payload = {
            "source_type": source,
            "target_type": target,
            "input_method": "cli_agent",
            "context": merged_context,
            "files": [],
            "github_url": _clean(github_url) or None,
            "resource_url": _clean(resource_url) or None,
            "org_id": _clean(org_id) or None,
            "custom_fields": custom_fields or None,
        }
        if not skip_questions:
            clarifier_questions = list(resume_questions or [])
            if not clarifier_questions:
                with _Spinner(
                    "Checking for high-impact follow-up questions (this can take a bit)..."
                ):
                    clarifier_questions = _get_migration_clarifiers(client, payload)
            if clarifier_questions:
                suggested_answers: dict[str, str] = {}
                provided_clarifier_answers = dict(clarifier_answers or {})
                if provided_clarifier_answers:
                    missing_ids = _missing_question_ids(
                        clarifier_questions, provided_clarifier_answers
                    )
                    if missing_ids:
                        raise SystemExit(
                            "Missing --answer values for: " + ", ".join(missing_ids)
                        )
                if _inside_coding_agent():
                    if not provided_clarifier_answers:
                        suggested_answers = _preset_answers_from_repo_scan(
                            clarifier_questions
                        )
                        rerun_command = (
                            f"keshro create -m --context {shlex.quote(context or '')}"
                        ).rstrip()
                        if agent != "auto":
                            rerun_command += f" -a {shlex.quote(agent)}"
                        _exit_for_agent_clarifier_feedback(
                            heading="Keshro needs user answers before it can create this migration.",
                            questions=clarifier_questions,
                            suggested_answers=suggested_answers,
                            rerun_command=rerun_command,
                            initial_answers=dict(payload.get("custom_fields") or {}),
                        )
                    resolved_clarifier_answers = provided_clarifier_answers
                elif provided_clarifier_answers:
                    resolved_clarifier_answers = provided_clarifier_answers
                else:
                    resolved_clarifier_answers = _prompt_for_migration_clarifiers(
                        clarifier_questions, suggested_answers
                    )
                payload = _merge_clarifier_answers(
                    payload, clarifier_questions, resolved_clarifier_answers
                )
            elif not _state.json:
                print("No additional follow-up questions needed.")
    _create_migration_from_payload(payload, {}, work_dir=resolved_work_dir)
    _delete_answers_file(answers_file_path)


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
        res = client.get("/v1/migrations", params=params)
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
        res = client.get(f"/v1/migrations/{migration_id}")
        res.raise_for_status()
        migration = res.json()
        if _state.json:
            print_output(migration, True)
            return
        linked_plan = None
        try:
            plan_res = client.get(f"/v1/migrations/{migration_id}/plan")
            plan_res.raise_for_status()
            linked_plan = plan_res.json()
        except Exception:
            linked_plan = None
        context_label = _current_context_label() if _current_org_id() else None
        _print_migration_detail(
            migration,
            context_label=context_label,
            linked_plan=linked_plan,
            api_url=_state.api_url,
        )


@migration_app.command("history")
def _migration_history(
    migration_id: Annotated[str, typer.Argument(help="Migration ID.")],
):
    """Show the execution history / audit trail for a migration."""
    with make_client(_state.api_url, _state.token) as client:
        migration_res = client.get(f"/v1/migrations/{migration_id}")
        migration_res.raise_for_status()
        migration = migration_res.json()
        plan_res = client.get(f"/v1/migrations/{migration_id}/plan")
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
        # Look up linked plan before deletion so we can clear default if it matches
        linked_plan_id: str | None = None
        try:
            res = client.get(f"/v1/migrations/{migration_id}")
            if res.status_code == 200:
                data = res.json() or {}
                linked_plan_id = _clean(data.get("plan_id")) or None
        except Exception:
            pass

        res = client.delete(f"/v1/migrations/{migration_id}")
        res.raise_for_status()

        # Clear default context if the deleted migration's plan was the default
        saved_plan_id = _current_plan_id()
        if linked_plan_id and saved_plan_id == linked_plan_id:
            update_auth({"default_plan_id": None, "default_plan_title": None})

        if _state.json:
            print_output(res.json(), True)
            return
        print(f"Deleted migration {migration_id}.")


# ---------------------------------------------------------------------------
# Config commands
# ---------------------------------------------------------------------------


def _config_show():
    auth = load_auth()
    # Self-heal: if the saved default plan no longer exists, clear it
    saved_default = _clean(auth.get("default_plan_id"))
    if saved_default and not _plan_exists(saved_default):
        update_auth({"default_plan_id": None, "default_plan_title": None})
        auth = load_auth()
    repo_plan_id, repo_plan_title = _resolve_repo_linked_plan()
    orgs: list[dict] = []
    authenticated = False
    if auth.get("token"):
        try:
            with make_client(
                auth.get("api_url") or DEFAULT_API_URL, auth.get("token")
            ) as client:
                me_res = client.get("/v1/auth/me")
                me_res.raise_for_status()
                authenticated = True
                res = client.get("/v1/orgs")
                res.raise_for_status()
                orgs = res.json() or []
        except Exception:
            orgs = []
    payload = {
        "api_url": auth.get("api_url") or DEFAULT_API_URL,
        "authenticated": authenticated,
        "default_agent": _clean(auth.get("default_agent")).lower() or "auto",
        "default_org_id": auth.get("default_org_id"),
        "default_org_name": auth.get("default_org_name"),
        "default_plan_id": auth.get("default_plan_id"),
        "default_plan_title": auth.get("default_plan_title"),
        "repo_plan_id": repo_plan_id,
        "repo_plan_title": repo_plan_title,
        "user": auth.get("user") or {},
        "orgs": orgs,
    }
    plan_id = payload.get("default_plan_id") or ""
    repo_context = _load_plan_context_details(repo_plan_id) if repo_plan_id else {}
    default_context_details = _load_plan_context_details(plan_id) if plan_id else {}
    payload["repo_context_kind"] = repo_context.get("kind")
    payload["repo_context_migration_id"] = repo_context.get("migration_id")
    payload["default_context_kind"] = default_context_details.get("kind")
    payload["default_context_migration_id"] = default_context_details.get(
        "migration_id"
    )
    if _state.json:
        print_output(payload, True)
        return
    user = payload["user"] or {}
    print(
        f"{DIM}Authenticated:{RESET} "
        f"{GREEN if payload['authenticated'] else CYAN}{'yes' if payload['authenticated'] else 'no'}{RESET}"
    )
    # Scope: explicit org vs personal. "Default context: personal" was too
    # vague — users couldn't tell from the label whether migrations would
    # land in their personal space or under an org. The web UI shows the
    # active org prominently; this mirrors that.
    org_name = _clean(payload.get("default_org_name"))
    org_id_value = _clean(payload.get("default_org_id"))
    if org_name or org_id_value:
        scope_label = f"org ({YELLOW}{org_name or org_id_value}{RESET})"
    else:
        scope_label = f"{YELLOW}personal{RESET}"
    print(f"{DIM}Current scope:{RESET} {scope_label}")
    repo_plan = payload["repo_plan_title"] or payload["repo_plan_id"]
    default_plan = payload["default_plan_title"] or payload["default_plan_id"]
    repo_plan_id = payload.get("repo_plan_id") or ""
    if payload["authenticated"]:
        if repo_plan:
            app_url = _app_url_from_api_url(payload["api_url"])
            repo_migration_id = payload.get("repo_context_migration_id") or ""
            if repo_migration_id:
                print(f"{DIM}Current repo migration:{RESET} {YELLOW}{repo_plan}{RESET}")
                print(
                    f"{DIM}Migration URL:{RESET} {CYAN}{app_url}/migrations/{repo_migration_id}{RESET}"
                )
            else:
                repo_plan_url = f"{app_url}/plans/{repo_plan_id}" if repo_plan_id else ""
                print(f"{DIM}Current repo project:{RESET} {YELLOW}{repo_plan}{RESET}")
                if repo_plan_url:
                    print(f"{DIM}Project URL:{RESET} {CYAN}{repo_plan_url}{RESET}")
        if default_plan and plan_id != repo_plan_id:
            app_url = _app_url_from_api_url(payload["api_url"])
            default_migration_id = payload.get("default_context_migration_id") or ""
            if default_migration_id:
                print(f"{DIM}Current migration:{RESET} {YELLOW}{default_plan}{RESET}")
                print(
                    f"{DIM}Migration URL:{RESET} {CYAN}{app_url}/migrations/{default_migration_id}{RESET}"
                )
            else:
                plan_url = f"{app_url}/plans/{plan_id}" if plan_id else ""
                print(f"{DIM}Current project:{RESET} {YELLOW}{default_plan}{RESET}")
                if plan_url:
                    print(f"{DIM}Project URL:{RESET} {CYAN}{plan_url}{RESET}")
    if user.get("email"):
        print(f"{DIM}User:{RESET} {CYAN}{user['email']}{RESET}")
    if user.get("name"):
        print(f"{DIM}Name:{RESET} {user['name']}")
    if payload["authenticated"]:
        current_org = None
        if org_id_value:
            current_org = next(
                (org for org in payload["orgs"] if _clean(org.get("id")) == org_id_value),
                None,
            )
        org_executor = _clean((current_org or {}).get("default_executor")) or ""
        org_worktree = _clean((current_org or {}).get("worktree_policy")) or ""
        org_pr = _clean((current_org or {}).get("pr_policy")) or ""
        personal_executor_raw = _clean(user.get("default_executor"))
        personal_worktree_raw = normalize_worktree_policy(user.get("worktree_policy")) or ""
        personal_pr_raw = normalize_pr_policy(user.get("pr_policy")) or ""
        effective_executor = personal_executor_raw or org_executor or "local_claude_code"
        effective_worktree = (
            personal_worktree_raw or org_worktree or DEFAULT_WORKTREE_POLICY
        )
        effective_pr = personal_pr_raw or org_pr or DEFAULT_PR_POLICY

        print(
            f"{DIM}Execution defaults:{RESET} "
            f"{YELLOW}executor={effective_executor}; create_worktrees={effective_worktree}; open_prs={effective_pr}{RESET}"
        )
        if current_org:
            org_executor = org_executor or "unset"
            org_worktree = org_worktree or DEFAULT_WORKTREE_POLICY
            org_pr = org_pr or DEFAULT_PR_POLICY
            print(
                f"{DIM}Workspace defaults:{RESET} "
                f"{YELLOW}executor={org_executor}; create_worktrees={org_worktree}; open_prs={org_pr}{RESET}"
            )


@config_app.callback(invoke_without_command=True)
def _config_callback(
    ctx: typer.Context,
    clear_default: Annotated[
        bool,
        typer.Option(
            "--clear-default",
            help="Clear saved default project/migration context (plan id, title, work dir).",
        ),
    ] = False,
):
    if clear_default:
        update_auth(
            {
                "default_plan_id": None,
                "default_plan_title": None,
                "default_migration_id": None,
                "default_work_dir": None,
            }
        )
        repo_root = _discover_repo_root()
        if repo_root is not None:
            try:
                with make_client(_state.api_url, _state.token) as client:
                    client.request(
                        "DELETE",
                        "/v1/plans/repo-link",
                        params={
                            "repo_root": str(repo_root),
                            "git_remote_url": _discover_git_remote_url(repo_root),
                        },
                    )
            except Exception:
                pass
        print(f"{GREEN}✓{RESET} Cleared saved default project context.")
        return
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
    agent: Annotated[
        Optional[str],
        typer.Option(
            "--agent",
            "-a",
            help="Default coding agent for create/continue: auto, claude, or codex.",
        ),
    ] = None,
    personal: Annotated[
        bool, typer.Option("--personal", help="Use personal context.")
    ] = False,
    work_dir: Annotated[
        Optional[str], typer.Option("--dir", "-d", help="Default project directory.")
    ] = None,
    clear_plan: Annotated[
        bool, typer.Option("--clear-plan", help="Clear saved execution context.")
    ] = False,
):
    """Set default workspace context."""
    updates: dict = {}
    linked_repo = False
    context_changed = False
    resolved_plan_id: str | None = None
    resolved_plan_title: str | None = None
    if work_dir is not None:
        updates["default_work_dir"] = (
            str(Path(work_dir).resolve()) if work_dir else None
        )
    if api_url is not None:
        updates["api_url"] = _clean(api_url) or DEFAULT_API_URL
    if agent is not None:
        normalized_agent = _clean(agent).lower() or "auto"
        if normalized_agent not in {"auto", "claude", "codex"}:
            raise SystemExit(
                "Unsupported agent. Use --agent auto, --agent claude, or --agent codex."
            )
        updates["default_agent"] = normalized_agent
    if personal:
        updates["default_org_id"] = None
        updates["default_org_name"] = None
        context_changed = True
    elif org_id is not None or org is not None:
        resolved_id, resolved_name = _resolve_org_context(org_id, org)
        updates["default_org_id"] = resolved_id
        updates["default_org_name"] = resolved_name
        context_changed = True
    if clear_plan:
        updates["default_plan_id"] = None
        updates["default_plan_title"] = None
    elif plan_id is not None:
        _ensure_authenticated()
        resolved_plan_id, resolved_plan_title = _resolve_plan_or_migration_context(
            plan_id
        )
        updates["default_plan_id"] = resolved_plan_id
        updates["default_plan_title"] = resolved_plan_title
        effective_work_dir = (
            str(Path(work_dir).resolve())
            if work_dir
            else str(Path.cwd())
        )
        if _discover_repo_root(effective_work_dir) is not None:
            linked_repo = _link_current_repo_to_plan(
            resolved_plan_id or "",
            plan_title=resolved_plan_title,
            work_dir=effective_work_dir,
        )
            if not linked_repo:
                raise SystemExit(
                    "Failed to update the current repo link to the requested execution context. No mismatch was saved."
                )
    auth = update_auth(updates)
    payload = {
        "api_url": auth.get("api_url") or DEFAULT_API_URL,
        "default_agent": _clean(auth.get("default_agent")).lower() or "auto",
        "default_org_id": auth.get("default_org_id"),
        "default_org_name": auth.get("default_org_name"),
        "default_plan_id": auth.get("default_plan_id"),
        "default_plan_title": auth.get("default_plan_title"),
    }
    if _state.json:
        print_output(payload, True)
        return
    if context_changed:
        org_label = auth.get("default_org_name") or auth.get("default_org_id") or "personal"
        print(f"Saved default context: {org_label}")
    if api_url is not None:
        print(f"Saved API URL: {auth.get('api_url') or DEFAULT_API_URL}")
    if agent is not None:
        print(f"Saved default agent: {auth.get('default_agent') or 'auto'}")
    plan_label = auth.get("default_plan_title") or auth.get("default_plan_id")
    if plan_label:
        print(f"Saved default execution context: {plan_label}")
        if linked_repo:
            print("Linked the current repo to this execution context in Keshro.")
    elif clear_plan:
        print("Cleared default execution context.")


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def _cmd_plan_templates(
    template_name: str | None = None,
    name: str | None = None,
    verbose: bool = False,
):
    with make_client(_state.api_url, _state.token) as client:
        res = client.get("/v1/plans/templates")
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


@app.command("templates", hidden=True)
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


@migration_app.command("templates")
def _migration_templates(
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
    """List available migration templates and path keys"""
    _cmd_plan_templates(template_name, name, verbose)


@app.command("continue")
def _continue_command(
    plan_id: Annotated[
        Optional[str],
        typer.Option(
            "--plan-id",
            "-p",
            help="Standalone plan ID. Uses saved execution context if omitted.",
        ),
    ] = None,
    migration_id: Annotated[
        Optional[str],
        typer.Option(
            "--migration-id",
            "-m",
            help="Migration ID. Keshro resolves it to the linked execution plan.",
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
            help="Disable parallel execution and resume a single task.",
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
    agent: Annotated[
        str,
        typer.Option(
            "--agent",
            "-a",
            help="Coding agent to use for execution: auto, claude, or codex.",
        ),
    ] = "auto",
    visible: Annotated[
        bool,
        typer.Option(
            "--visible",
            help="Run agents in dedicated terminal sessions instead of headless. Intended for direct use from a normal user terminal, not from inside another agent session. Uses Collaborator tiles if installed (recommended: https://github.com/collaborator-ai/collab-public), otherwise opens native Terminal.app windows on macOS.",
        ),
    ] = False,
    executor: Annotated[
        Optional[str],
        typer.Option(
            "--executor",
            help=(
                "Override the per-task executor for this run. Values: "
                "'local_claude_code' (or 'local') to force every task to run in a "
                "local Claude Code subprocess, or 'managed_agent' (or 'managed') "
                "to route every task through Anthropic's hosted Claude Managed "
                "Agents (requires backend support). When omitted, each task runs "
                "on its configured executor, defaulting to local Claude Code."
            ),
        ),
    ] = None,
):
    """Resume execution of a plan."""

    if _clean(plan_id) and _clean(migration_id):
        raise SystemExit("Pass either --plan-id or --migration-id, not both.")

    resolved_plan_id = None
    if _clean(migration_id):
        resolved_plan_id, _ = _resolve_plan_or_migration_context(migration_id)
    elif _clean(plan_id):
        resolved_plan_id, _ = _resolve_plan_or_migration_context(plan_id)
    if not _clean(resolved_plan_id):
        resolved_plan_id = _current_plan_id(None, work_dir=work_dir)
        if resolved_plan_id:
            resolved_plan_id = _confirm_implicit_continue_plan(
                resolved_plan_id, work_dir=work_dir
            )

    # Parallel is the default everywhere. Use --no-parallel only when you explicitly
    # want a single-task prompt flow.
    use_parallel = not no_parallel
    requested_agent = _clean(agent).lower()
    resolved_agent = requested_agent or _default_agent_preference() or "auto"
    if resolved_agent == "auto":
        inherited_agent = _current_coding_agent_preference()
        if inherited_agent:
            resolved_agent = inherited_agent
    if resolved_agent not in {"auto", "claude", "codex"}:
        raise SystemExit(
            "Unsupported agent. Use --agent auto, --agent claude, or --agent codex."
        )
    if visible and _inside_coding_agent():
        visible = False
        if not _state.json:
            agent_name = _coding_agent_name() or "coding agent"
            print(
                f"{YELLOW}--visible is only supported from a direct user terminal; ignoring it inside {agent_name} and continuing headless.{RESET}"
            )
    # Resolve --executor override. We accept friendly aliases ('local',
    # 'managed') and normalize them via the shared executor module so the CLI
    # and backend agree on identifiers.
    from .executor import (
        DEFAULT_EXECUTOR,
        LOCAL_CLAUDE_CODE,
        MANAGED_AGENT,
        normalize_executor as _normalize_executor,
    )

    executor_override: str | None = None
    if executor is not None:
        raw = _clean(executor).lower()
        alias_map = {
            "local": LOCAL_CLAUDE_CODE,
            "claude_code": LOCAL_CLAUDE_CODE,
            "managed": MANAGED_AGENT,
            "anthropic": MANAGED_AGENT,
        }
        canonical = alias_map.get(raw, raw)
        normalized = _normalize_executor(canonical)
        if normalized is None:
            raise SystemExit(
                f"Unknown --executor value {executor!r}. "
                f"Use 'local_claude_code' (or 'local') or "
                f"'managed_agent' (or 'managed')."
            )
        executor_override = normalized

    # --executor managed_agent always runs Anthropic Claude on Anthropic's
    # infrastructure; pairing it with --agent codex would silently ignore the
    # user's agent choice. Fail fast so the mismatch is obvious.
    if executor_override == MANAGED_AGENT and resolved_agent not in ("auto", "claude"):
        raise SystemExit(
            f"--executor managed_agent only supports Claude; got --agent {resolved_agent}. "
            f"Managed Agents runs Anthropic Claude on Anthropic's infrastructure. "
            f"Drop --agent (or use --agent claude) to proceed, or switch to "
            f"--executor local_claude_code to run {resolved_agent} locally."
        )

    if not use_parallel:
        if executor_override == MANAGED_AGENT:
            raise SystemExit(
                "--executor managed_agent requires parallel mode; "
                "remove --no-parallel to enable managed execution "
                "(it runs through the parallel wave scheduler)."
            )
        _continue_with_claude(
            resolved_plan_id,
            work_dir=work_dir,
            auto_continue=auto_continue,
            parallel=False,
            confirm=confirm,
            agent=resolved_agent,
        )
    else:
        _ensure_authenticated()
        concurrency = max(1, min(concurrency, 30))
        if not _state.json:
            print(f"{DIM}Using {_prompt_agent_display_name(resolved_agent)}{RESET}\n")
        asyncio.run(
            _run_parallel(
                resolved_plan_id,
                work_dir=work_dir,
                max_concurrency=concurrency,
                run_all=auto_continue,
                dry_run=dry_run,
                agent=resolved_agent,
                visible=visible,
                executor_override=executor_override,
            )
        )


_SKILL_FILE = Path(__file__).parent / "data" / "SKILL.md"


@app.command("setup-claude", hidden=True)
def _setup_claude():
    """Install a global Claude Code slash command for Keshro"""
    target = _install_claude_integration()
    if _state.json:
        print_output({"status": "ok", "path": str(target)}, True)
    else:
        print(f"Installed Claude Code skill v{__version__} at {target}")
        print(
            "Keshro will auto-trigger in Claude Code for migration and refactor tasks."
        )


@app.command("setup-codex", hidden=True)
def _setup_codex():
    """Install global Keshro instructions for Codex"""
    try:
        target = _install_codex_integration()
        print(f"Installed Codex instructions: {target}")
    except Exception as exc:
        print(f"{RED}Failed: {exc}{RESET}", file=sys.stderr)
        raise typer.Exit(1) from exc


@app.command("setup-cursor", hidden=True)
def _setup_cursor():
    """Install Keshro instructions in .cursorrules for Cursor"""
    try:
        cwd = Path.cwd()
        cursor_file = cwd / ".cursorrules"
        marker = "# keshro-agent-instructions"
        keshro_block = f"{marker}\n{KESHRO_SLASH_COMMAND}\n# end-keshro"
        if cursor_file.exists():
            content = cursor_file.read_text()
            if marker in content:
                print(".cursorrules already has Keshro instructions.")
                return
            cursor_file.write_text(content.rstrip() + "\n\n" + keshro_block + "\n")
        else:
            cursor_file.write_text(keshro_block + "\n")
        print(f"Installed Keshro instructions: {cursor_file}")
    except Exception as exc:
        print(f"{RED}Failed: {exc}{RESET}", file=sys.stderr)
        raise typer.Exit(1) from exc


@app.command("setup")
def _setup_all():
    """Install Keshro instructions for all supported agents (Claude Code, Codex, Cursor)"""
    installed, already_present = _install_agent_integrations(silent=True)
    if installed:
        print("Installed Keshro agent instructions:")
        for target in installed:
            print(f"  {GREEN}✓{RESET} {target}")
        if already_present:
            print("Already present:")
            for target in already_present:
                print(f"  {DIM}•{RESET} {target}")
    elif already_present:
        print("All agent integrations already installed.")
    else:
        print("No agent integrations were installed.")


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
            res = client.post("/v1/plans/from-template", json=payload)
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
        res = client.post("/v1/plans", json=payload)
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
        res = client.get("/v1/plans", params=params)
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
                _format_plan_timestamp(plan.get("updated_at")),
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
        res = client.get(f"/v1/plans/{plan_id}")
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
    agent_sessions = plan.get("agent_sessions") or []

    done = [s for s in steps if _clean(s.get("status")).lower() == "completed"]
    in_progress = [s for s in steps if _clean(s.get("status")).lower() == "in_progress"]
    blocked = [s for s in steps if _clean(s.get("status")).lower() == "blocked"]
    todo = [s for s in steps if _clean(s.get("status")).lower() == "todo"]
    plan_status = _clean(plan.get("status")).lower()

    # Header
    if steps:
        progress_label = f"[{len(done)}/{len(steps)} done]"
    elif plan_status == "analyzing":
        progress_label = "[analyzing]"
    else:
        progress_label = "[no tasks]"
    print(f"\n{CYAN}{title}{RESET} {DIM}{path_label}{RESET} {progress_label}")
    print()

    _print_plan_enrichment(plan)
    _print_plan_analysis(plan)
    if plan.get("enrichment_sources") or _plan_analysis(plan):
        plan_id = _clean(plan.get("id"))
        if plan_id:
            dashboard_url = _execution_dashboard_url(plan, plan_id)
            print(f"  {DIM}Review in UI: {dashboard_url}{RESET}")
        print()

    if not steps:
        if plan_status == "analyzing":
            print(f"  {DIM}Plan is still being created from analysis. Tasks will appear when analysis completes.{RESET}")
        else:
            print(f"  {DIM}This execution context does not have any tasks yet.{RESET}")
        print()

    # Status symbols
    STATUS_ICON = {
        "completed": f"{GREEN}✓{RESET}",
        "in_progress": f"{YELLOW}●{RESET}",
        "stopped": f"{YELLOW}■{RESET}",
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

    task_latest_session: dict[str, dict] = {}
    for session in agent_sessions:
        tid = _clean(session.get("task_id"))
        if not tid:
            continue
        existing = task_latest_session.get(tid)
        session_time = (
            _clean(session.get("last_heartbeat_at"))
            or _clean(session.get("updated_at"))
            or _clean(session.get("created_at"))
        )
        existing_time = (
            _clean((existing or {}).get("last_heartbeat_at"))
            or _clean((existing or {}).get("updated_at"))
            or _clean((existing or {}).get("created_at"))
        )
        if not existing or session_time > existing_time:
            task_latest_session[tid] = session

    now = datetime.now(timezone.utc)
    detail_width = max(
        40, shutil.get_terminal_size(fallback=(100, 24)).columns - 8
    )

    for step in steps:
        step_id = _clean(step.get("id"))
        latest = task_latest_event.get(step_id)
        latest_session = task_latest_session.get(step_id)
        session_status = _clean((latest_session or {}).get("status")).lower()
        status = _clean(step.get("status") or "todo").lower()
        runtime_status = _clean(step.get("runtime_status")).lower()
        if session_status == "stopped" or runtime_status == "stopped":
            status = "stopped"
        elif session_status in {
            "running",
            "starting",
            "in_progress",
            "active",
        }:
            status = "in_progress"
        icon = STATUS_ICON.get(status, "?")
        order = step.get("order", 0)
        step_title = _clean(step.get("title")) or "Untitled"

        # Build right-side info
        info_parts: list[str] = []
        if latest_session:
            source_label = _clean(latest_session.get("source")) or "agent"
            if source_label:
                info_parts.append(source_label)
            session_time = (
                _clean(latest_session.get("last_heartbeat_at"))
                or _clean(latest_session.get("updated_at"))
                or _clean(latest_session.get("created_at"))
            )
            if session_time:
                try:
                    event_time = datetime.fromisoformat(
                        session_time.replace("Z", "+00:00")
                    )
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
            phase = _clean(latest_session.get("current_phase"))
            if phase and phase not in {"running", "starting"}:
                info_parts.append(phase)
        elif latest:
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

        if status == "in_progress":
            active_time = (
                _clean((latest_session or {}).get("last_heartbeat_at"))
                or _clean((latest_session or {}).get("updated_at"))
                or _clean((latest_session or {}).get("created_at"))
                or _clean((latest or {}).get("created_at"))
            )
            if active_time:
                try:
                    event_time = datetime.fromisoformat(
                        active_time.replace("Z", "+00:00")
                    )
                    elapsed = now - event_time
                    if elapsed.seconds > 60:
                        info_parts.append(f"({elapsed.seconds // 60}m)")
                except Exception:
                    pass

        if status == "stopped":
            stop_reason = (
                _clean(step.get("runtime_status_reason"))
                or _clean((latest_session or {}).get("progress_message"))
                or "Execution stopped before task completion."
            )
            info_parts.append(stop_reason)

        if status == "blocked":
            reason = _clean(step.get("blocked_reason"))
            if reason:
                info_parts.append(reason)

        if status == "completed":
            step_metrics = step.get("metrics") if isinstance(step.get("metrics"), dict) else {}
            latest_metrics_raw = (latest or {}).get("metrics")
            latest_metrics_dict = (
                latest_metrics_raw if isinstance(latest_metrics_raw, dict) else {}
            )
            latest_metrics = {
                "duration_seconds": latest_metrics_dict.get(
                    "duration_seconds", (latest or {}).get("duration_seconds")
                ),
                "tokens_used": latest_metrics_dict.get(
                    "tokens_used", (latest or {}).get("tokens_used")
                ),
                "model": _clean(
                    latest_metrics_dict.get("model", (latest or {}).get("model"))
                ),
                "cost_usd": latest_metrics_dict.get(
                    "cost_usd", (latest or {}).get("cost_usd")
                ),
            }
            metrics = {
                "duration_seconds": step_metrics.get("duration_seconds"),
                "tokens_used": step_metrics.get("tokens_used"),
                "model": _clean(step_metrics.get("model")),
                "cost_usd": step_metrics.get("cost_usd"),
            }
            for key, value in latest_metrics.items():
                if metrics.get(key) in (None, ""):
                    metrics[key] = value
            duration_seconds = metrics.get("duration_seconds")
            tokens_used = metrics.get("tokens_used")
            model_name = _clean(metrics.get("model"))
            cost_usd = metrics.get("cost_usd")

            if isinstance(duration_seconds, (int, float)) and duration_seconds > 0:
                info_parts.append(f"{int(round(float(duration_seconds)))}s")
            if isinstance(tokens_used, (int, float)) and tokens_used > 0:
                info_parts.append(f"{int(tokens_used):,} tokens")
            if model_name:
                info_parts.append(model_name)
            if isinstance(cost_usd, (int, float)) and cost_usd > 0:
                info_parts.append(f"${float(cost_usd):.2f}")

        print(f"  {icon} {order}. {step_title}")
        if step_id:
            print(f"    {DIM}task-id: {step_id}{RESET}")
        if info_parts:
            details = " · ".join(part for part in info_parts if part)
            for line in _format_full_value_lines(details, width=detail_width):
                print(f"    {DIM}{line}{RESET}")
        if status == "in_progress":
            notes = _clean(step.get("notes"))
            if notes:
                note_lines = [line.strip() for line in notes.splitlines() if line.strip()]
                if note_lines:
                    latest_note = note_lines[-1]
                    for line in _format_full_value_lines(latest_note, width=detail_width):
                        print(f"    {DIM}{line}{RESET}")

    # Footer
    print()
    summary_parts = []
    stopped_tasks = [
        s
        for s in steps
        if _clean(s.get("status")).lower() == "in_progress"
        and _clean(s.get("runtime_status")).lower() == "stopped"
    ]
    active_running = [
        s
        for s in in_progress
        if _clean(s.get("runtime_status")).lower() != "stopped"
    ]
    if active_running:
        summary_parts.append(f"{len(active_running)} active")
    if stopped_tasks:
        summary_parts.append(f"{YELLOW}{len(stopped_tasks)} stopped{RESET}")
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

    # Cost summary
    agent_cost = plan.get("agent_cost") or {}
    cost_usd = agent_cost.get("total_cost_usd") or 0
    total_tokens = agent_cost.get("total_tokens") or 0
    total_duration = agent_cost.get("total_duration_seconds") or 0
    tasks_tracked = agent_cost.get("tasks_tracked") or 0
    if cost_usd > 0 or total_duration > 0:
        cost_parts = []
        if total_duration > 0:
            mins = int(total_duration // 60)
            secs = int(total_duration % 60)
            cost_parts.append(f"{mins}m {secs}s" if mins > 0 else f"{secs}s")
        if total_tokens > 0:
            cost_parts.append(f"{total_tokens:,} tokens")
        if cost_usd > 0:
            cost_parts.append(f"${cost_usd:.2f}")
        if tasks_tracked > 1:
            avg_cost = cost_usd / tasks_tracked
            avg_dur = total_duration / tasks_tracked
            avg_mins = int(avg_dur // 60)
            avg_secs = int(avg_dur % 60)
            avg_time = f"{avg_mins}m {avg_secs}s" if avg_mins > 0 else f"{avg_secs}s"
            cost_parts.append(f"avg {avg_time} · ${avg_cost:.2f}/task")
        print(f"  {DIM}Cost: {' · '.join(cost_parts)}{RESET}")

        # Per-model breakdown
        by_model = agent_cost.get("by_model") or {}
        if by_model:
            for model, data in by_model.items():
                m_tasks = data.get("tasks", 0)
                m_cost = data.get("cost_usd", 0)
                m_tokens = data.get("tokens", 0)
                print(
                    f"    {DIM}{model}: {m_tasks} task{'s' if m_tasks != 1 else ''} · {m_tokens:,} tok · ${m_cost:.2f}{RESET}"
                )

    print()


def _watch_via_sse(plan_id: str) -> None:
    """Watch plan status via SSE stream."""

    from httpx_sse import connect_sse

    headers = {
        "Authorization": f"Bearer {_state.token}",
        "Accept": "text/event-stream",
    }
    plan = _get_plan_or_exit(plan_id)
    print("\033[2J\033[H", end="")
    _print_plan_status(plan)
    print(f"  {DIM}Connecting to SSE...{RESET}")
    try:
        with httpx.Client(
            base_url=_state.api_url, headers=headers, timeout=None
        ) as client:
            with connect_sse(client, "GET", f"/v1/plans/{plan_id}/stream") as sse:
                # Connected — show live indicator
                print("\033[2J\033[H", end="")
                _print_plan_status(plan)
                print(f"  {GREEN}● live{RESET} · SSE connected · Ctrl+C to stop")
                for event in sse.iter_sse():
                    if event.event and event.event != "comment":
                        plan = _get_plan_or_exit(plan_id)
                        print("\033[2J\033[H", end="")
                        _print_plan_status(plan)
                        print(
                            f"  {GREEN}● live{RESET} · SSE connected · Ctrl+C to stop"
                        )
    except KeyboardInterrupt:
        print("\nStopped watching.")


def _watch_via_polling(plan_id: str) -> None:
    """Watch plan status via polling (fallback)."""
    import time as _time

    try:
        while True:
            print("\033[2J\033[H", end="")
            plan = _get_plan_or_exit(plan_id)
            _print_plan_status(plan)
            print(f"  {YELLOW}● polling{RESET} · refreshes every 10s · Ctrl+C to stop")
            _time.sleep(10)
    except KeyboardInterrupt:
        print("\nStopped watching.")


def _run_status(plan_id: str | None, watch: bool = False, tui: bool = False) -> None:

    resolved_plan_id = _current_plan_id(plan_id)
    if not resolved_plan_id:
        if _repo_link_points_at_deleted_migration():
            print(
                "This repo's linked project pointed at a deleted migration, so it was skipped."
            )
            print(
                "Link it to something current with `keshro config set --plan-id <id>`, or create a new one with `keshro new`."
            )
        else:
            print(
                "No project linked to this repo. Pass --plan-id <id>, save one with `keshro config set --plan-id <id>`, or create one with `keshro new`."
            )
        return

    if tui:
        try:
            from .tui import run_tui
        except ImportError:
            print(
                f"{RED}TUI requires textual. Install with: pip install textual{RESET}",
                file=sys.stderr,
            )
            raise typer.Exit(1)

        from .client import get_api_url, get_token

        api_url = get_api_url(_state.api_url)
        token = get_token(_state.token)
        run_tui(api_url=api_url, token=token, plan_id=resolved_plan_id)
        return

    plan = _get_plan_or_exit(resolved_plan_id)
    if _state.json:
        print_output(plan, True)
        return

    if not watch:
        _print_plan_status(plan)
        return

    # Try SSE first, fall back to polling
    try:
        _watch_via_sse(resolved_plan_id)
    except ImportError:
        _watch_via_polling(resolved_plan_id)
    except Exception as exc:
        print(f"  {YELLOW}SSE failed ({exc}), falling back to polling{RESET}")
        _watch_via_polling(resolved_plan_id)


@plan_app.command("status")
def _plan_status(
    plan_id_arg: Annotated[
        Optional[str],
        typer.Argument(help="Execution context ID. Uses saved context if omitted."),
    ] = None,
    plan_id: Annotated[
        Optional[str],
        typer.Option(
            "--plan-id",
            "-p",
            help="Execution context ID. Uses saved context if omitted.",
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
    """Live status dashboard for the current execution context."""
    _run_status(plan_id_arg or plan_id, watch=watch, tui=tui)


@app.command("status")
def _status_alias(
    plan_id: Annotated[
        Optional[str],
        typer.Option(
            "--plan-id",
            "-p",
            help="Execution context ID. Uses saved context if omitted.",
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
    """Live status dashboard for the current execution context."""
    _run_status(plan_id, watch=watch, tui=tui)


@plan_app.command("next")
def _plan_next(
    plan_id: Annotated[
        Optional[str],
        typer.Argument(help="Execution context ID. Uses saved context if omitted."),
    ] = None,
):
    """Show the next actionable task in the current execution context."""
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
        res = client.patch(f"/v1/plans/{plan_id}", json=payload)
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
        res = client.patch(f"/v1/plans/{resolved_plan_id}", json=payload)
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
        res = client.delete(f"/v1/plans/{plan_id}")
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
    issue_id: str | None = None,
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
        "external_issue_id": issue_id,
        "blocked_reason": blocked_reason,
        "artifact_links": link or [],
    }
    with make_client(_state.api_url, _state.token) as client:
        res = client.post(f"/v1/plans/{plan_id}/tasks", json=payload)
        res.raise_for_status()
        plan = res.json()
        if _state.json:
            print_output(plan, True)
            return
        _print_task_detail(plan, title_hint=title)


# Shared option definitions for task commands
_task_add_options = dict(
    title=typer.Option(..., "--title", "-t", help="Task title."),
    description=typer.Option(..., "--description", "-d", help="Task description."),
    status=typer.Option(TaskStatus.TODO, "--status", "-s", help="Task status."),
    owner=typer.Option(None, "--owner", "-o", help="Task owner."),
    notes=typer.Option(None, "--notes", "-n", help="Task notes."),
    issue_id=typer.Option(
        None, "--issue-id", "-i", help="Linked issue ID."
    ),
    blocked_reason=typer.Option(
        None, "--blocked-reason", "-b", "-r", help="Blocked reason."
    ),
    link=typer.Option(
        None,
        "--link",
        "-l",
        help="Attach a URL to this task (PR, doc, issue, dashboard, CI run, etc.).",
    ),
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
        TaskStatus, typer.Option("--status", "-s", help="Task status.")
    ] = TaskStatus.TODO,
    owner: Annotated[
        Optional[str], typer.Option("--owner", "-o", help="Task owner.")
    ] = None,
    notes: Annotated[
        Optional[str], typer.Option("--notes", "-n", help="Task notes.")
    ] = None,
    issue_id: Annotated[
        Optional[str], typer.Option("--issue-id", "-i", help="Linked issue ID.")
    ] = None,
    blocked_reason: Annotated[
        Optional[str],
        typer.Option("--blocked-reason", "-b", "-r", help="Blocked reason."),
    ] = None,
    link: Annotated[
        Optional[list[str]], typer.Option("--link", "-l", help="Attach a URL to this task (PR, doc, issue, dashboard, CI run, etc.).")
    ] = None,
):
    """Add a new task to a plan."""
    _do_task_add(
        plan_id_option or plan_id,
        title,
        description,
        status.value,
        owner,
        notes,
        issue_id,
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
        TaskStatus, typer.Option("--status", "-s", help="Task status.")
    ] = TaskStatus.TODO,
    owner: Annotated[
        Optional[str], typer.Option("--owner", "-o", help="Task owner.")
    ] = None,
    notes: Annotated[
        Optional[str], typer.Option("--notes", "-n", help="Task notes.")
    ] = None,
    issue_id: Annotated[
        Optional[str], typer.Option("--issue-id", "-i", help="Linked issue ID.")
    ] = None,
    blocked_reason: Annotated[
        Optional[str],
        typer.Option("--blocked-reason", "-b", "-r", help="Blocked reason."),
    ] = None,
    link: Annotated[
        Optional[list[str]], typer.Option("--link", "-l", help="Attach a URL to this task (PR, doc, issue, dashboard, CI run, etc.).")
    ] = None,
):
    """Add a new task to a plan."""
    _do_task_add(
        plan_id_option or plan_id,
        title,
        description,
        status.value,
        owner,
        notes,
        issue_id,
        blocked_reason,
        link,
    )


@task_app.command("view")
def _task_view(
    task_id: Annotated[str, typer.Argument(help="Task ID.")],
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
):
    """Show task details for a plan task."""
    resolved_plan_id, resolved_task_id = _resolve_task_target(task_id, plan_id_option)
    _view_task(resolved_plan_id, resolved_task_id)


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
    task_id: Annotated[str, typer.Argument(help="Task ID.")],
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
    resolved_plan_id, resolved_task_id = _resolve_task_target(task_id, plan_id_option)
    _delete_task(resolved_plan_id, resolved_task_id, feedback_reason, assume_yes)


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
    task_id: Annotated[
        str,
        typer.Argument(help="Task ID."),
    ],
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
        Optional[TaskStatus], typer.Option("--status", "-s", help="Task status.")
    ] = None,
    owner: Annotated[
        Optional[str], typer.Option("--owner", "-o", help="Task owner.")
    ] = None,
    notes: Annotated[
        Optional[str], typer.Option("--notes", "-n", help="Task notes.")
    ] = None,
    issue_id: Annotated[
        Optional[str], typer.Option("--issue-id", "-i", help="Linked issue ID.")
    ] = None,
    blocked_reason: Annotated[
        Optional[str],
        typer.Option("--blocked-reason", "-b", "-r", help="Blocked reason."),
    ] = None,
    depends_on: Annotated[
        Optional[list[str]],
        typer.Option(
            "--depends-on",
            help="Task IDs this task depends on. Repeat or pass a comma-separated list.",
        ),
    ] = None,
    clear_dependencies: Annotated[
        bool,
        typer.Option("--clear-dependencies", help="Remove all task dependencies."),
    ] = False,
    parallelizable: Annotated[
        Optional[bool],
        typer.Option(
            "--parallel/--serial",
            help="Mark the task as parallelizable or explicitly serial.",
        ),
    ] = None,
    feedback_reason: Annotated[
        Optional[str], typer.Option("--reason", help="Why this task changed.")
    ] = None,
    link: Annotated[
        Optional[list[str]], typer.Option("--link", "-l", help="Attach a URL to this task (PR, doc, issue, dashboard, CI run, etc.).")
    ] = None,
):
    """Update an existing task's status, owner, or details."""
    if clear_dependencies and depends_on:
        raise typer.BadParameter(
            "Cannot combine --clear-dependencies with --depends-on. "
            "Use one or the other."
        )
    resolved_plan_id, resolved_task_id = _resolve_task_target(task_id, plan_id_option)
    _do_task_update(
        resolved_plan_id,
        resolved_task_id,
        title,
        description,
        status.value if status is not None else None,
        owner,
        notes,
        issue_id,
        blocked_reason,
        feedback_reason,
        link,
        [] if clear_dependencies else _parse_dependency_ids(depends_on) if depends_on is not None else None,
        parallelizable,
    )


@task_app.command("start")
def _task_start(
    task_id: Annotated[str, typer.Argument(help="Task ID.")],
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
    resolved_plan_id, resolved_task_id = _resolve_task_target(task_id, plan_id_option)
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
    task_id: Annotated[str, typer.Argument(help="Task ID.")],
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
    duration: Annotated[
        Optional[float], typer.Option("--duration", help="Session duration in seconds.")
    ] = None,
    model_name: Annotated[
        Optional[str],
        typer.Option("--model", help="Model used (e.g., claude-sonnet-4-20250514)."),
    ] = None,
):
    """Mark a task as completed. Use --cost/--tokens to report agent session cost."""
    resolved_plan_id, resolved_task_id = _resolve_task_target(task_id, plan_id_option)
    _do_task_done(
        resolved_plan_id,
        resolved_task_id,
        notes=notes,
        feedback_reason=feedback_reason,
        link=link,
    )
    inferred = _infer_task_done_context(resolved_plan_id, resolved_task_id)
    # Report completion metrics via agent API
    if (
        cost is not None
        or tokens is not None
        or duration is not None
        or model_name
        or inferred.get("agent_session_id")
        or inferred.get("duration_seconds") is not None
    ):
        try:
            with make_client(_state.api_url, _state.token) as c:
                c.post(
                    f"/v1/agent/plans/{resolved_plan_id}/task-event",
                    json={
                        "task_id": resolved_task_id,
                        "event": "done",
                        "note": f"Session cost: ${cost or 0:.4f} ({tokens or 0:,} tokens, {model_name or 'unknown'})",
                        "agent_session_id": inferred.get("agent_session_id") or "",
                        "tokens_used": tokens or 0,
                        "model": model_name or "",
                        "cost_usd": cost or 0,
                        "duration_seconds": duration
                        if duration is not None
                        else inferred.get("duration_seconds") or 0,
                    },
                    timeout=10,
                )
        except Exception:
            pass


@task_app.command("block")
def _task_block(
    task_id: Annotated[str, typer.Argument(help="Task ID.")],
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
    resolved_plan_id, resolved_task_id = _resolve_task_target(task_id, plan_id_option)
    _do_task_block(
        resolved_plan_id,
        resolved_task_id,
        blocked_reason=blocked_reason,
        feedback_reason=feedback_reason,
    )


@task_app.command("unblock")
def _task_unblock(
    task_id: Annotated[str, typer.Argument(help="Task ID.")],
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
        TaskStatus, typer.Option("--status", "-s", help="Status to use after unblocking.")
    ] = TaskStatus.IN_PROGRESS,
    feedback_reason: Annotated[
        Optional[str],
        typer.Option("--reason", help="Why the task is being unblocked now."),
    ] = None,
):
    """Clear a task blocker and resume work."""
    resolved_plan_id, resolved_task_id = _resolve_task_target(task_id, plan_id_option)
    _do_task_unblock(
        resolved_plan_id,
        resolved_task_id,
        notes=notes,
        feedback_reason=feedback_reason,
        status=status.value,
    )


@task_app.command("reopen")
def _task_reopen(
    task_id: Annotated[str, typer.Argument(help="Task ID.")],
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
    notes: Annotated[
        Optional[str],
        typer.Option(
            "--notes", "-n", help="Short note about why the task is being reopened."
        ),
    ] = None,
    status: Annotated[
        TaskStatus,
        typer.Option(
            "--status", "-s", help="Status to use after reopening (defaults to todo)."
        ),
    ] = TaskStatus.TODO,
    feedback_reason: Annotated[
        Optional[str],
        typer.Option("--reason", help="Why the task is being reopened now."),
    ] = None,
):
    """Move a task out of completed state so it can be worked again."""
    resolved_plan_id, resolved_task_id = _resolve_task_target(task_id, plan_id_option)
    _do_task_reopen(
        resolved_plan_id,
        resolved_task_id,
        notes=notes,
        feedback_reason=feedback_reason,
        status=status.value,
    )


@task_app.command("note")
def _task_note(
    task_id: Annotated[str, typer.Argument(help="Task ID.")],
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
    resolved_plan_id, resolved_task_id = _resolve_task_target(task_id, plan_id_option)
    _append_task_note(
        resolved_plan_id,
        resolved_task_id,
        note=note,
        feedback_reason=feedback_reason,
    )


@task_app.command("artifact")
def _task_artifact(
    task_id: Annotated[str, typer.Argument(help="Task ID.")],
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
    link: Annotated[
        str, typer.Option("--link", "-l", help="Attach a URL to this task (PR, doc, issue, dashboard, CI run, etc.).")
    ] = ...,
    feedback_reason: Annotated[
        Optional[str], typer.Option("--reason", help="Why this artifact matters.")
    ] = None,
):
    """Attach an artifact link to a task without overwriting existing links."""
    resolved_plan_id, resolved_task_id = _resolve_task_target(task_id, plan_id_option)
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


def _rollback_task(
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
            f"/v1/agent/plans/{resolved_plan_id}/task-event",
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


@task_app.command("rollback")
def _task_rollback(
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
    _rollback_task(task_id, plan_id_option, force)


@app.command("rollback", hidden=True)
def _rollback_alias(
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
    _rollback_task(task_id, plan_id_option, force)


def _explain_task(
    task_id: Annotated[str, typer.Argument(help="Task ID to explain decisions for.")],
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
):
    """Show the decision audit trail for a task."""
    resolved_plan_id = _require_plan_context(plan_id_option)
    client = make_client()

    try:
        resp = client.get(f"/v1/plans/{resolved_plan_id}")
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


@task_app.command("explain")
def _task_explain(
    task_id: Annotated[str, typer.Argument(help="Task ID to explain decisions for.")],
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
):
    """Show the decision audit trail for a task."""
    _explain_task(task_id, plan_id_option)


@app.command("explain", hidden=True)
def _explain_alias(
    task_id: Annotated[str, typer.Argument(help="Task ID to explain decisions for.")],
    plan_id_option: Annotated[
        Optional[str], typer.Option("--plan-id", "-p", help="Plan ID.")
    ] = None,
):
    """Show the decision audit trail for a task."""
    _explain_task(task_id, plan_id_option)


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

    detected_migration = _detect_migration_intent(description)
    if detected_migration:
        raise SystemExit(
            "This request looks like a migration. Use "
            f"`keshro create -m --context {shlex.quote(description)}` "
            "so Keshro can ask migration-specific follow-up questions and create a migration project."
        )

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
        resp = client.post("/v1/plans/generate", json=payload, timeout=120)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _print_http_error(exc)
        raise typer.Exit(1) from exc

    plan = resp.json()
    plan_id = plan.get("id", "")
    steps = plan.get("plan_steps", [])

    _set_default_plan_after_create(plan)

    print(f"\n{GREEN}Project created: {plan.get('title', 'Untitled')}{RESET}")
    print(f"  ID: {plan_id}")
    print(f"  Tasks: {len(steps)}")
    print(f"  Status: {plan.get('status', 'draft')}")

    _print_plan_enrichment(plan)
    _print_plan_analysis(plan)
    if plan.get("enrichment_sources") or _plan_analysis(plan):
        print(f"  Review in UI: {_execution_dashboard_url(plan, plan_id)}")

    if steps:
        print(f"\n{CYAN}Tasks:{RESET}")
    for step in steps:
        dep_info = ""
        if step.get("depends_on"):
            dep_info = f" {DIM}(depends on: {', '.join(step['depends_on'])}){RESET}"
        risk = step.get("risk_level") or ""
        risk_badge = ""
        if risk == "high":
            risk_badge = f" {RED}[high risk]{RESET}"
        elif risk == "medium":
            risk_badge = f" {YELLOW}[medium]{RESET}"
        print(
            f"  {step.get('order', 0):2d}. {step.get('title', 'Untitled')}{risk_badge}{dep_info}"
        )

    if not confirm and plan.get("status") == "draft":
        print(
            f"\n{DIM}Execution context is in draft. Run 'keshro continue -p {plan_id} --confirm' to start execution.{RESET}"
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
        resp = client.post("/v1/plans/import/preview", json=preview_payload, timeout=60)
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
    if questions and not skip_questions:
        if _inside_coding_agent():
            print(
                f"{CYAN}Asking AI agent to suggest answers for {len(questions)} clarifying questions...{RESET}"
            )
            import_context = "\n\n".join(
                part
                for part in [
                    preview.get("issues_text", ""),
                    enrichment,
                ]
                if _clean(part)
            )
            suggested_answers = _answer_questions_via_agent(
                questions,
                import_context or f"Imported project from {provider}",
                None,
                os.getcwd(),
            )
            answers = _review_agent_suggested_answers(
                questions,
                suggested_answers,
                heading="Clarifying questions",
                non_interactive_notice=(
                    "Non-interactive agent session detected; suggested clarifier answers were not auto-applied. "
                    "Re-run interactively or use the direct CLI if you want to review them before plan generation."
                ),
            )
            if not _state.json:
                suggested_count = sum(
                    1
                    for value in suggested_answers.values()
                    if value and value.lower() != "unknown"
                )
                answered_count = sum(
                    1
                    for value in answers.values()
                    if value and value.lower() != "unknown"
                )
                print(
                    f"  Agent suggested {suggested_count}/{len(questions)} answers; accepted {answered_count}/{len(questions)}."
                )
        elif sys.stdout.isatty():
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
                        title = opt.get("answer_title", opt.get("value", ""))
                        rec = _format_recommended_suffix(
                            str(title), bool(opt.get("recommended"))
                        )
                        print(f"    {idx}. {title}{rec}")
                    raw = input(
                        f"  {DIM}Enter number or type answer [{placeholder or 'skip'}]: {RESET}"
                    ).strip()
                    if raw:
                        option_map = {
                            _clean(opt.get("answer_title"))
                            or _clean(opt.get("value")): _clean(opt.get("value"))
                            or _clean(opt.get("answer_title"))
                            for opt in options
                            if _clean(opt.get("answer_title"))
                            or _clean(opt.get("value"))
                        }
                        selected = _resolve_menu_choice(raw, list(option_map))
                        answers[qid] = option_map.get(selected, raw)
                else:
                    raw = input(
                        f"  {DIM}Answer [{placeholder or 'skip'}]: {RESET}"
                    ).strip()
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
        resp = client.post("/v1/plans/import", json=import_payload, timeout=120)
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
# Plan push / sync-pull commands
# ---------------------------------------------------------------------------


@plan_app.command("push")
def _plan_push(
    plan_id: Annotated[
        Optional[str],
        typer.Option("--plan-id", "-p", help="Plan ID."),
    ] = None,
    provider: Annotated[
        str, typer.Option("--provider", help="Target: linear, jira, or github.")
    ] = "linear",
    team_id: Annotated[
        Optional[str],
        typer.Option("--team-id", help="Linear team ID or Jira project key."),
    ] = None,
    project_id: Annotated[
        Optional[str],
        typer.Option("--project-id", help="Linear project ID (optional)."),
    ] = None,
    sync_mode: Annotated[
        str,
        typer.Option(
            "--mode",
            help="'standalone' (individual issues) or 'grouped' (with parent).",
        ),
    ] = "standalone",
):
    """Push plan tasks to Linear, Jira, or GitHub as issues."""
    _ensure_authenticated()
    resolved = _current_plan_id(plan_id)
    if not resolved:
        print(f"{RED}Plan ID required.{RESET}", file=sys.stderr)
        raise typer.Exit(1)

    client = make_client()
    payload: dict = {"provider": provider, "sync_mode": sync_mode}
    if team_id:
        payload["team_id"] = team_id
    if project_id:
        payload["project_id"] = project_id

    print(f"{CYAN}Pushing plan to {provider}...{RESET}")
    try:
        resp = client.post(f"/v1/plans/{resolved}/push", json=payload, timeout=60)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _print_http_error(exc)
        raise typer.Exit(1) from exc

    data = resp.json()
    created = data.get("created", 0)
    updated = data.get("updated", 0)
    print(f"{GREEN}Done.{RESET} {created} issue(s) created, {updated} updated.")

    if _state.json:
        print_output(data)


@plan_app.command("sync-pull")
def _plan_sync_pull(
    plan_id: Annotated[
        Optional[str],
        typer.Option("--plan-id", "-p", help="Plan ID."),
    ] = None,
):
    """Pull status updates from linked Linear/Jira issues back into the plan."""
    _ensure_authenticated()
    resolved = _current_plan_id(plan_id)
    if not resolved:
        print(f"{RED}Plan ID required.{RESET}", file=sys.stderr)
        raise typer.Exit(1)

    client = make_client()
    print(f"{CYAN}Pulling status from linked issues...{RESET}")
    try:
        resp = client.post(f"/v1/plans/{resolved}/sync-pull", timeout=30)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _print_http_error(exc)
        raise typer.Exit(1) from exc

    data = resp.json()
    synced = data.get("synced", 0)
    changes = data.get("changes", [])

    if synced == 0:
        print(f"{DIM}No status changes detected.{RESET}")
    else:
        print(f"{GREEN}{synced} task(s) updated:{RESET}")
        for change in changes:
            print(
                f"  {change.get('external_key', '?')} → {change.get('external_status', '?')} "
                f"(was: {change.get('current_status', '?')})"
            )

    if _state.json:
        print_output(data)


# ---------------------------------------------------------------------------
# Task decide command
# ---------------------------------------------------------------------------


@task_app.command("decide")
def _task_decide(
    task_id: Annotated[str, typer.Argument(help="Task ID.")],
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
    resolved_plan_id, resolved_task_id = _resolve_task_target(task_id, plan_id_option)

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
        resp = client.post(f"/v1/agent/plans/{resolved_plan_id}/decide", json=payload)
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


# ---------------------------------------------------------------------------
# Saved-context subcommands (`keshro org context`, `keshro user context`)
# ---------------------------------------------------------------------------
#
# Backed by the team_context / cost_context JSON columns added to organizations
# and users. The migration / project clarifier reads these so users only fill
# team-size, experience, cost-overlap, etc. once per scope. These commands let
# the user inspect or wipe what's saved without hunting through the web UI.


@org_app.command("context")
def _org_context(
    clear_team: Annotated[
        bool, typer.Option("--clear-team", help="Clear saved org team context.")
    ] = False,
    clear_cost: Annotated[
        bool, typer.Option("--clear-cost", help="Clear saved org cost context.")
    ] = False,
    clear_all: Annotated[
        bool, typer.Option("--clear-all", help="Clear both team and cost context.")
    ] = False,
):
    """View or clear the saved team / cost context for the active org."""
    if clear_team or clear_cost or clear_all:
        raise SystemExit(
            _clear_saved_context(
                scope="org",
                clear_team=clear_team,
                clear_cost=clear_cost,
                clear_all=clear_all,
            )
        )
    raise SystemExit(_show_saved_context(scope="org"))


@user_app.command("context")
def _user_context(
    clear_team: Annotated[
        bool, typer.Option("--clear-team", help="Clear saved personal team context.")
    ] = False,
    clear_cost: Annotated[
        bool, typer.Option("--clear-cost", help="Clear saved personal cost context.")
    ] = False,
    clear_all: Annotated[
        bool, typer.Option("--clear-all", help="Clear both team and cost context.")
    ] = False,
):
    """View or clear the saved team / cost context for your personal account."""
    if clear_team or clear_cost or clear_all:
        raise SystemExit(
            _clear_saved_context(
                scope="user",
                clear_team=clear_team,
                clear_cost=clear_cost,
                clear_all=clear_all,
            )
        )
    raise SystemExit(_show_saved_context(scope="user"))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None):
    _enable_line_buffered_output()
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__version__)
        return 0
    if "--version" in argv or "--help" in argv:
        try:
            app(argv, standalone_mode=False)
            return 0
        except click.ClickException as exc:
            exc.show(file=sys.stderr)
            return exc.exit_code
        except SystemExit as exc:
            if isinstance(exc.code, str):
                print(f"{RED}{exc.code}{RESET}", file=sys.stderr)
                return 1
            return exc.code if isinstance(exc.code, int) and exc.code != 0 else 0
    if _should_refresh_agent_integrations([sys.argv[0], *argv]):
        _maybe_refresh_claude()
        _maybe_refresh_codex()
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
    except (click.Abort, KeyboardInterrupt):
        print(file=sys.stderr)
        return 130
    except httpx.HTTPStatusError as exc:
        _print_http_error(exc)
        return 1
    except httpx.RequestError as exc:
        _print_request_error(exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
