"""Git and PR helper functions extracted from cli.py."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys

import httpx

from ._state import (
    CYAN,
    DIM,
    RESET,
    YELLOW,
    _clean,
)
from .context import _current_app_url


def _parse_git_status_changed_files(raw_status: str) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()
    for raw_line in raw_status.splitlines():
        line = raw_line.rstrip()
        if len(line) < 4:
            continue
        path_text = line[3:].strip()
        if not path_text:
            continue
        candidates = (
            [part.strip().strip('"') for part in path_text.split(" -> ", 1)]
            if " -> " in path_text
            else [path_text.strip('"')]
        )
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            files.append(candidate)
            seen.add(candidate)
    return files


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
        res = await client.patch(f"/v1/plans/{plan_id}/tasks/{task_id}", json=body)
        res.raise_for_status()
    except Exception as exc:
        print(f"  {DIM}[warn] status update failed: {exc}{RESET}", file=sys.stderr)


async def _post_agent_note_async(
    client: httpx.AsyncClient,
    plan_id: str,
    task_id: str,
    note: str,
    *,
    session_id: str = "",
) -> None:
    payload: dict[str, str] = {
        "task_id": task_id,
        "event": "note",
        "note": note,
    }
    if session_id:
        payload["agent_session_id"] = session_id
    try:
        await client.post(f"/v1/agent/plans/{plan_id}/task-event", json=payload)
    except Exception:
        pass


async def _cleanup_worktree(repo_dir: str, worktree_path: str) -> None:
    """Remove a manually-created git worktree."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "worktree",
            "remove",
            "--force",
            worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=repo_dir,
        )
        await proc.communicate()
    except Exception:
        pass


async def _create_codex_worktree(
    repo_dir: str,
    worktree_path: str,
    branch_name: str,
    base_rev: str,
) -> tuple[bool, str]:
    wt_proc = await asyncio.create_subprocess_exec(
        "git",
        "worktree",
        "add",
        "-b",
        branch_name,
        worktree_path,
        base_rev,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=repo_dir,
    )
    wt_stdout, wt_stderr = await wt_proc.communicate()
    output = ((wt_stderr or b"") + (wt_stdout or b"")).decode(errors="replace").strip()
    return wt_proc.returncode == 0, output


async def _git_stdout(*args: str, cwd: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            (stderr or b"").decode(errors="replace").strip() or "git command failed"
        )
    return (stdout or b"").decode(errors="replace").strip()


def _parse_github_remote(remote_url: str) -> tuple[str, str] | None:
    """Extract owner/repo from a GitHub remote URL. Returns (owner, repo) or None."""
    patterns = [
        r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?$",
    ]
    for pattern in patterns:
        m = re.search(pattern, remote_url)
        if m:
            return m.group(1), m.group(2)
    return None


def _resolve_default_branch(cwd: str) -> str:
    """Detect the remote's default branch. Falls back to common names."""
    # Preferred: ask the remote directly for its HEAD symref
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--symref", "origin", "HEAD"],
            capture_output=True, text=True, cwd=cwd, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("ref: refs/heads/"):
                    return line.split("refs/heads/", 1)[1].split()[0]
    except Exception:
        pass
    # Next: locally-cached origin/HEAD symref
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            capture_output=True, text=True, cwd=cwd, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip().startswith("origin/"):
            return result.stdout.strip().split("origin/", 1)[1]
    except Exception:
        pass
    # Last resort: probe common branch names
    for candidate in ("main", "master", "dev", "develop"):
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--verify", f"origin/{candidate}"],
                capture_output=True, cwd=cwd, timeout=5,
            )
            if r.returncode == 0:
                return candidate
        except Exception:
            continue
    return "main"


async def _create_task_pr(
    *,
    exec_dir: str,
    task_id: str,
    task_title: str,
    plan_title: str,
    task: dict,
    api_client: httpx.AsyncClient,
    plan_id: str,
    pr_policy: str = "auto",
) -> tuple[str | None, bool]:
    """Push the current branch and (optionally) create a PR.

    Returns a tuple of (pr_url, branch_pushed). pr_url is the created or
    existing PR URL, or None when no PR was created (disabled policy, no
    commits ahead, etc). branch_pushed indicates whether the branch was
    pushed to origin so callers can condition worktree cleanup on it.

    Tries gh CLI first, then GitHub API via GITHUB_TOKEN env var. Warns if
    neither is available.
    """
    # Detect current branch
    try:
        branch_name = await _git_stdout(
            "git", "rev-parse", "--abbrev-ref", "HEAD", cwd=exec_dir
        )
        if not branch_name or branch_name == "HEAD":
            return None, False
    except Exception:
        return None, False

    default_branch = _resolve_default_branch(exec_dir)

    if branch_name == default_branch:
        return None, False

    # Check for commits ahead
    try:
        log_output = await _git_stdout(
            "git", "log", f"origin/{default_branch}..HEAD", "--oneline", cwd=exec_dir
        )
        if not log_output.strip():
            return None, False
    except Exception:
        return None, False

    normalized_pr_policy = str(pr_policy or "auto").strip().lower() or "auto"
    if normalized_pr_policy == "disabled":
        print(
            f"    {DIM}PR policy disabled; leaving branch {branch_name} local only.{RESET}"
        )
        return None, False

    # Push
    try:
        await _git_stdout(
            "git", "push", "-u", "origin", branch_name, cwd=exec_dir
        )
    except Exception as exc:
        print(f"    {DIM}Could not push branch {branch_name}: {exc}{RESET}")
        return None, False

    # Detect remote provider
    try:
        remote_url = await _git_stdout(
            "git", "remote", "get-url", "origin", cwd=exec_dir
        )
    except Exception:
        remote_url = ""

    # Build a useful PR body from the task data
    app_url = _current_app_url()
    body_parts = [f"## {task_title}", ""]
    description = _clean(task.get("description"))
    if description:
        body_parts.append(description)
        body_parts.append("")
    criteria = [_clean(c) for c in (task.get("acceptance_criteria") or []) if _clean(c)]
    if criteria:
        body_parts.append("### Acceptance criteria")
        for c in criteria:
            body_parts.append(f"- [ ] {c}")
        body_parts.append("")
    related_files = [_clean(f) for f in (task.get("related_files") or []) if _clean(f)]
    if related_files:
        body_parts.append("### Related files")
        for f in related_files:
            body_parts.append(f"- `{f}`")
        body_parts.append("")
    body_parts.append(f"**Plan:** {plan_title}")
    body_parts.append(f"**Task:** [{task_id}]({app_url}/plans/{plan_id})")
    body_parts.append("")
    body_parts.append("---")
    body_parts.append(f"*Generated by [Keshro]({app_url}) parallel execution*")
    pr_body = "\n".join(body_parts)

    pr_url: str | None = None

    # Check for existing PR on this branch (avoid duplicates on retry)
    pr_url = await _find_existing_pr(exec_dir=exec_dir, branch_name=branch_name)
    if pr_url:
        # PR already exists — just link it
        try:
            existing_links: list[str] = []
            try:
                task_resp = await api_client.get(f"/v1/plans/{plan_id}/tasks/{task_id}")
                if task_resp.status_code == 200:
                    task_data = task_resp.json()
                    existing_links = [
                        str(link).strip()
                        for link in (task_data.get("artifact_links") or [])
                        if str(link or "").strip()
                    ]
            except Exception:
                pass
            if pr_url not in existing_links:
                await api_client.patch(
                    f"/v1/plans/{plan_id}/tasks/{task_id}",
                    json={
                        "github_pr_url": pr_url,
                        "artifact_links": [*existing_links, pr_url],
                    },
                )
        except Exception:
            pass
        return pr_url, True

    if normalized_pr_policy == "manual":
        print(
            f"    {DIM}Branch {branch_name} pushed; PR policy is manual, so no pull request was opened.{RESET}"
        )
        return None, True

    # Strategy 1: try gh CLI
    pr_url = await _create_pr_via_gh(
        exec_dir=exec_dir,
        task_title=task_title,
        pr_body=pr_body,
        default_branch=default_branch,
        branch_name=branch_name,
    )

    # Strategy 2: try GitHub API directly
    if not pr_url:
        github_info = _parse_github_remote(remote_url) if remote_url else None
        github_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        if github_info and github_token:
            pr_url = await _create_pr_via_github_api(
                owner=github_info[0],
                repo=github_info[1],
                token=github_token,
                title=task_title,
                body=pr_body,
                base=default_branch,
                head=branch_name,
            )

    if not pr_url:
        provider = "GitHub" if "github" in remote_url else "remote"
        print(
            f"    {YELLOW}Branch {CYAN}{branch_name}{YELLOW} pushed but could not create PR.{RESET}"
        )
        print(
            f"    {DIM}Changes are on branch {branch_name} in {exec_dir}{RESET}"
        )
        print(
            f"    {DIM}Install gh CLI or set GITHUB_TOKEN to enable auto-PR for {provider}.{RESET}"
        )
        return None, True

    # Link PR to task — merge with existing artifact links
    try:
        existing_links: list[str] = []
        try:
            task_resp = await api_client.get(f"/v1/plans/{plan_id}/tasks/{task_id}")
            if task_resp.status_code == 200:
                task_data = task_resp.json()
                existing_links = [
                    str(link).strip()
                    for link in (task_data.get("artifact_links") or [])
                    if str(link or "").strip()
                ]
        except Exception:
            pass
        merged_links = existing_links if pr_url in existing_links else [*existing_links, pr_url]
        await api_client.patch(
            f"/v1/plans/{plan_id}/tasks/{task_id}",
            json={
                "github_pr_url": pr_url,
                "artifact_links": merged_links,
            },
        )
    except Exception:
        pass

    return pr_url, True


async def _find_existing_pr(*, exec_dir: str, branch_name: str) -> str | None:
    """Check if a PR already exists for this branch via gh CLI or GitHub API."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "pr", "list", "--head", branch_name, "--json", "url", "-q", ".[0].url",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=exec_dir,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            url = (stdout or b"").decode(errors="replace").strip()
            return url if (url and url != "null") else None
    except Exception:
        pass
    try:
        remote_url = await _git_stdout("git", "remote", "get-url", "origin", cwd=exec_dir)
    except Exception:
        remote_url = ""
    github_info = _parse_github_remote(remote_url) if remote_url else None
    if not github_info:
        return None
    github_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    try:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if github_token:
            headers["Authorization"] = f"Bearer {github_token}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.github.com/repos/{github_info[0]}/{github_info[1]}/pulls",
                headers=headers,
                params={
                    "head": f"{github_info[0]}:{branch_name}",
                    "state": "open",
                },
                timeout=15,
            )
        if resp.status_code == 200:
            pulls = resp.json()
            if isinstance(pulls, list):
                for pr in pulls:
                    pr_url = _clean((pr or {}).get("html_url"))
                    if pr_url:
                        return pr_url
    except Exception:
        pass
    return None


async def _create_pr_via_gh(
    *,
    exec_dir: str,
    task_title: str,
    pr_body: str,
    default_branch: str,
    branch_name: str,
) -> str | None:
    """Create a PR using the gh CLI. Returns PR URL or None."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        if proc.returncode != 0:
            return None
    except Exception:
        return None

    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "pr", "create",
            "--title", task_title,
            "--body", pr_body,
            "--base", default_branch,
            "--head", branch_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=exec_dir,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return None
        pr_url = (stdout or b"").decode(errors="replace").strip()
        return pr_url if pr_url else None
    except Exception:
        return None


async def _create_pr_via_github_api(
    *,
    owner: str,
    repo: str,
    token: str,
    title: str,
    body: str,
    base: str,
    head: str,
) -> str | None:
    """Create a PR using the GitHub REST API directly. Returns PR URL or None."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.github.com/repos/{owner}/{repo}/pulls",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json={
                    "title": title,
                    "body": body,
                    "base": base,
                    "head": head,
                },
                timeout=15,
            )
            if resp.status_code in (201, 200):
                data = resp.json()
                return data.get("html_url")
            return None
    except Exception:
        return None


async def _git_changed_files(cwd: str, max_files: int = 25) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()

    async def _collect(*args: str) -> None:
        try:
            output = await _git_stdout(*args, cwd=cwd)
        except Exception:
            return
        for raw_line in output.splitlines():
            candidate = raw_line.strip().strip('"')
            if not candidate or candidate in seen:
                continue
            files.append(candidate)
            seen.add(candidate)

    await _collect("git", "diff", "--name-only", "HEAD", "--")
    await _collect("git", "ls-files", "--others", "--exclude-standard")
    return files[:max_files]


async def _collect_git_changed_files(cwd: str) -> list[str]:
    return await _git_changed_files(cwd)


def _classify_file_edit_from_diff(cwd: str, path: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "diff", "--unified=0", "--", path],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
        diff_text = proc.stdout or ""
    except Exception:
        return f"editing {path}"
    added_lines = [
        line[1:].strip()
        for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    changed_lines = [
        line[1:].strip()
        for line in diff_text.splitlines()
        if (line.startswith("+") or line.startswith("-"))
        and not line.startswith("+++")
        and not line.startswith("---")
    ]
    lowered = "\n".join(added_lines).lower()
    if not changed_lines:
        return f"editing {path}"

    nonempty_changed = [line for line in changed_lines if line]
    if nonempty_changed and all(
        line.startswith(("import ", "from ")) for line in nonempty_changed
    ):
        return f"updating imports in {path}"

    config_tokens = (
        "=",
        ":",
        "config",
        "setting",
        "option",
        "param",
        "env",
        "variable",
        "timeout",
        "path",
    )
    if any(token in lowered for token in config_tokens):
        return f"updating configuration in {path}"
    return f"editing {path}"


async def _summarize_file_edits(cwd: str, changed_files: list[str], max_files: int = 2) -> str:
    if not changed_files:
        return ""
    summaries: list[str] = []
    for path in changed_files[:max_files]:
        summaries.append(await asyncio.to_thread(_classify_file_edit_from_diff, cwd, path))
    if not summaries:
        return ""
    if len(changed_files) > max_files:
        summaries.append(f"and {len(changed_files) - max_files} more file(s)")
    return "; ".join(summaries)
