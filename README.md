# Keshro CLI

Keshro is the execution layer for coding agents.

It turns a plan into a coordinated execution loop: the next task is selected, the agent gets the right context, progress is tracked as work happens, and the plan stays readable in both the CLI and the web app.

## What it does

- turns project plans into executable task queues
- gives agents compact, task-specific execution briefs
- tracks notes, blockers, artifacts, decisions, and completion state
- launches parallel agents in isolated git worktrees
- carries forward relevant context from earlier completed tasks
- links back to the Keshro web UI when review is easier there

## Who this is for

Use the CLI if you want Keshro to drive execution from your terminal or inside an existing coding-agent workflow.

This is the default Keshro interface for:

- Claude Code
- Codex
- Cursor
- terminal-first agent workflows

If you specifically want an MCP server instead, use [`keshro-mcp`](../keshro-mcp).

## Install

```bash
pip install keshro
```

Or with `uv`:

```bash
uv tool install keshro
```

Or from source:

```bash
git clone https://github.com/jlewitt1/keshro-cli.git
cd keshro-cli
pip install -e ".[dev]"
```

## Quickstart

### 1. Log in

```bash
keshro login ksh_pat_...
```

Get your token from **Account -> API** at [keshro.com](https://keshro.com).

Auth is stored at `~/.keshro/auth.json`.

`keshro login` also installs:

- the global Claude Code slash command
- global Codex instructions in `~/.codex/AGENTS.md`

If you also want Cursor repo rules, run `keshro setup` inside the repo you want Cursor to use.

### 2. Create or import a plan

```bash
keshro plan generate "Refactor the auth layer to support API keys and rate limiting"
```

Or import from a tracker:

```bash
keshro import linear --project <key>
```

### 3. Review status

```bash
keshro status -p <plan-id>
```

### 4. Execute

```bash
keshro continue -p <plan-id>
```

If the plan is still a draft:

```bash
keshro continue -p <plan-id> --confirm
```

## Core commands

### Execution

```bash
keshro continue -p <plan-id>
keshro continue -p <plan-id> --confirm
keshro continue -p <plan-id> --all
keshro continue -p <plan-id> --concurrency 10
keshro continue -p <plan-id> --dry-run
keshro continue -p <plan-id> --dir /path/to/repo
keshro continue -p <plan-id> --no-parallel
```

### Status and monitoring

```bash
keshro status -p <plan-id>
keshro status --watch
keshro status --tui
```

`keshro status` surfaces:

- task progress
- blocked work
- enrichment context
- top risks
- open questions
- direct links to the Keshro web app when the UI is the better place to review something

### Task lifecycle

```bash
keshro task next -p <plan-id>
keshro task start <task-id> -p <plan-id>
keshro task note <task-id> -p <plan-id> -n "..."
keshro task artifact <task-id> -p <plan-id> -l "<url>"
keshro task block <task-id> -p <plan-id> -r "..."
keshro task unblock <task-id> -p <plan-id>
keshro task done <task-id> -p <plan-id> -n "Acceptance criteria met: ... Verification: ..."
keshro task decide <task-id> -p <plan-id> --context "..." --choice "..." --reasoning "..."
```

### Plan management

```bash
keshro plan view <plan-id>
keshro plan list
keshro plan update <plan-id> --status active
keshro plan create --title "..." --source-type "..." --target-type "..."
```

### Review and rollback

```bash
keshro explain <task-id> -p <plan-id>
keshro rollback <task-id> -p <plan-id>
```

## How `keshro continue` works

When run from your terminal, `keshro continue` acts as the coordinator:

- it finds the next actionable task
- can launch parallel agents in isolated git worktrees
- respects dependency order
- keeps task state synced back to the plan

When the same command runs inside a coding agent, it switches to worker mode automatically and prints a compact task brief instead of spawning more agents.

That brief includes:

1. the current task
2. acceptance criteria
3. relevant notes from completed tasks
4. explicit handoff notes
5. topical context from related earlier work
6. repo and git state when available

If the agent needs full task detail, it can fetch it explicitly:

```bash
keshro task view <task-id> -p <plan-id>
```

## Execution model

Keshro is designed around real execution, not just static planning.

Key behaviors:

- **Parallel worktrees**: multiple agents can run safely at once
- **Topical context routing**: relevant learnings flow by domain, not just task order
- **Task splitting**: parallelizable work can be broken into subtasks
- **Git checkpoints**: rollback points exist before task execution
- **Validation gates**: agents can verify changes before closing tasks
- **Draft gating**: draft plans require explicit confirmation before execution
- **Session tracking**: work is associated with agent sessions and execution metadata

## Configuration

By default, the CLI talks to:

```text
https://api.keshro.com
```

Override with:

```bash
export KESHRO_API_URL="https://your-keshro-api"
```

## Publishing

1. Update the version in `pyproject.toml`
2. Create a GitHub release (or run the `Publish CLI` workflow manually)
3. The package is published to PyPI automatically via trusted publishing
