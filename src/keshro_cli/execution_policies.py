"""CLI-side execution policy helpers.

Mirrors the backend's execution policy normalization so the CLI can consume
plan-level resolved defaults without hardcoding behavior in the launcher.
"""

from __future__ import annotations

from typing import Any


WORKTREE_ALWAYS = "always"
WORKTREE_CODE_CHANGES_ONLY = "code_changes_only"
DEFAULT_WORKTREE_POLICY = WORKTREE_ALWAYS

PR_AUTO = "auto"
PR_MANUAL = "manual"
PR_DISABLED = "disabled"
DEFAULT_PR_POLICY = PR_AUTO


def normalize_worktree_policy(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {WORKTREE_ALWAYS, WORKTREE_CODE_CHANGES_ONLY}:
        return normalized
    return None


def normalize_pr_policy(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {PR_AUTO, PR_MANUAL, PR_DISABLED}:
        return normalized
    return None


def resolve_worktree_policy(plan: dict) -> str:
    return (
        normalize_worktree_policy((plan or {}).get("effective_worktree_policy"))
        or DEFAULT_WORKTREE_POLICY
    )


def resolve_pr_policy(plan: dict) -> str:
    return normalize_pr_policy((plan or {}).get("effective_pr_policy")) or DEFAULT_PR_POLICY


def task_likely_requires_code_changes(task: dict) -> bool:
    task = task or {}
    related_files = [
        str(path).strip()
        for path in (task.get("related_files") or [])
        if str(path or "").strip()
    ]
    if related_files:
        for path in related_files:
            lowered = path.lower()
            if lowered.startswith("docs/") or lowered.endswith(".md") or lowered.endswith(".txt"):
                continue
            return True
        return False

    text = " ".join(
        [
            str(task.get("title") or ""),
            str(task.get("description") or ""),
            " ".join(str(item or "") for item in (task.get("acceptance_criteria") or [])),
        ]
    ).lower()

    non_code_markers = (
        "document",
        "docs",
        "runbook",
        "status page",
        "stakeholder",
        "validate",
        "analysis",
        "research",
        "compare",
        "audit",
        "inventory",
    )
    if any(marker in text for marker in non_code_markers):
        return False

    code_markers = (
        "implement",
        "update code",
        "refactor",
        "add test",
        "fix",
        "patch",
        "modify",
        "rename",
        "migrate",
        "change config",
        "terraform",
        "dag",
        "workflow",
        "schema",
    )
    return any(marker in text for marker in code_markers)


def should_use_isolated_worktree(task: dict, *, policy: str) -> bool:
    normalized = normalize_worktree_policy(policy) or DEFAULT_WORKTREE_POLICY
    if normalized == WORKTREE_ALWAYS:
        return True
    return task_likely_requires_code_changes(task)
