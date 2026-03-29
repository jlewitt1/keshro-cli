# Keshro CLI

The intelligent execution layer for coding agents. Keshro makes agents learn from each other, execute safely in parallel, and not ship garbage.

## Install

```bash
curl -fsSL https://api.keshro.com/v1/cli/install | sh -s -- ksh_pat_...
```

This downloads, installs, and authenticates in one step. Get your token from **Account → API** in the Keshro app.

### From source

```bash
pip install -e ".[dev]"
# or with uv:
uv tool install .
```

## Auth

```bash
keshro login ksh_pat_...     # First-time login
keshro login                 # Reuse saved session
keshro logout
```

Auth state is stored in `~/.keshro/auth.json`. `keshro login` installs the global Claude Code slash command and global Codex instructions in `~/.codex/AGENTS.md`. If you also want Cursor integration, run `keshro setup` inside each repo where you want `.cursorrules` instructions.

When you save or create a plan from a repo, Keshro stores that repo-to-plan link server-side so CLI commands can resolve the active plan from the current checkout.

The CLI talks to `https://api.keshro.com` by default — override with `KESHRO_API_URL`.

## Commands

### Execution

```bash
keshro continue -p <plan-id>                      # Coordinator mode: launch parallel agents from your terminal
keshro continue -p <plan-id> --confirm             # Approve and execute a draft plan
keshro continue -p <plan-id> --all                 # Auto-continue through all tasks
keshro continue -p <plan-id> --concurrency 10      # Up to 10 agents at once (default 5)
keshro continue -p <plan-id> --dry-run             # Preview which tasks would launch
keshro continue -p <plan-id> --dir /path/to/repo   # Point agent(s) at a different codebase
keshro continue -p <plan-id> --no-parallel         # Coordinator mode: force one task at a time
keshro status -p <plan-id>                         # Live dashboard of all tasks and agents
keshro status --watch                              # Auto-refresh every 10 seconds
keshro status --tui                                # Interactive dashboard with plan insights and live events
keshro setup-claude                                # Install global Claude Code slash command
```

`keshro status` also surfaces plan enrichment, top risks, open questions, and a direct app URL when the web UI is the better place to review or edit that context.

### What happens when you run `keshro continue`

From your terminal, `keshro continue` acts as the coordinator: it can launch parallel Claude Code agents in isolated git worktrees, one per actionable task, respecting dependency order. When the same command runs inside a coding agent (piped stdout), it switches to worker mode automatically and resumes exactly one task instead of spawning more agents.

Inside a coding agent, `keshro continue` prints a compact task brief instead of the full internal execution prompt. If the agent needs the extra task context, it can fetch it explicitly with `keshro task view <task-id> -p <plan-id>`. Users can monitor progress separately with `keshro status -p <plan-id> --watch` or `keshro status -p <plan-id> --tui`.

**Intelligent execution features:**

- **Topical context** — when one agent discovers something about IAM, every future IAM-related task gets that context automatically. Learnings route by domain tag, not just sequence.
- **Parallel worktrees** — multiple agents run simultaneously in isolated git worktrees. No merge conflicts.
- **Git checkpoints** — auto-commit before each task for one-command rollback.
- **Task splitting** — parallelizable tasks auto-decompose into sub-tasks for other agents.
- **Task handoff** — "Next task should know:" notes flow to the next task's prompt.
- **Validation gates** — agents verify changes (lint, tests, syntax) before marking done.
- **Session history** — completed task summaries + git changes since last checkpoint.
- **Draft plan gate** — draft plans require `--confirm` before first execution.
- **Auto-continue** — `--all` runs through all tasks without pausing.
- **Agent session IDs** — each session gets a unique ID for multi-agent tracking.
- **Multi-repo** — `--dir` points Claude at a codebase in a different directory.

### Plan management

```bash
keshro plan view <plan-id>
keshro plan list
keshro plan update <plan-id> --status ready
keshro plan create --title "..." --source-type "..." --target-type "..."
keshro import linear --project <key>               # Import from Linear
```

### Task management

```bash
keshro task next -p <plan-id>
keshro task start <task-id> -p <plan-id>
keshro task note <task-id> -p <plan-id> -n "..."
keshro task artifact <task-id> -p <plan-id> -l "<url>"
keshro task block <task-id> -p <plan-id> -r "..."
keshro task unblock <task-id> -p <plan-id>
keshro task done <task-id> -p <plan-id> -n "Acceptance criteria met: ... Verification: ..."
keshro task decide <task-id> -p <plan-id> --context "..." --choice "..." --reasoning "..."
keshro explain <task-id> -p <plan-id>
keshro rollback <task-id> -p <plan-id>
```

## How it works

The CLI is a thin HTTP client over the Keshro API with an intelligent prompt builder on top. Every command calls the hosted Keshro backend — the CLI doesn't store plans or state locally.

The execution engine (`keshro continue`) builds a structured prompt that includes:
1. The current task with description and acceptance criteria
2. Prior progress from completed tasks
3. **Sequential handoff** — explicit "Next task should know:" notes from previous tasks
4. **Topical context** — learnings from any completed task that shares domain tags (e.g. IAM, Airflow, Terraform)
5. Git state since the last checkpoint
6. Workspace configuration

The CLI prints a compact execution brief to stdout for the coding agent to follow, with `keshro task view` available when the agent needs the fuller task context.

## Publishing

### Via GitHub Actions (recommended)

1. Update version in `pyproject.toml` and push
2. Go to Actions → "Publish CLI" → Run workflow

### Manual

```bash
python -m build --sdist
curl -X PUT -H "X-Deploy-Secret: $DEPLOY_SECRET" \
  -F "file=@dist/keshro-0.1.0.tar.gz" \
  https://api.keshro.com/v1/cli/upload
```
