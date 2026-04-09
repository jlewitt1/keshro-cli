# Keshro

Plan and run high-stakes engineering projects with AI agents.

```bash
keshro login              # authenticate and install Claude Code + Codex integrations
keshro create             # scan project, create the right migration/project
keshro continue           # agents execute in parallel if possible
```

`keshro login` authenticates the CLI and installs agent integrations on the current machine. For Codex, Keshro adds a managed block to `~/.codex/AGENTS.md` and preserves any non-Keshro content already in that file.

Keshro is built for migrations first. It scans the repo, asks the follow-up questions that actually matter for the migration, creates the right migration or project, then coordinates agents to execute it safely.

Works with your existing coding agent. Use Claude Code or Codex for planning, migration intake, and parallel execution.

Examples:
- AWS Batch -> Airflow
- Terraform -> Pulumi
- Jenkins -> GitHub Actions
- Express -> Fastify
- Apache Iceberg -> ClickHouse

What Keshro does:
1. Builds a migration-aware execution context with risks, open questions, task ordering, and acceptance criteria
2. Runs agents in parallel in isolated git worktrees
3. Detects live file overlap in Codex worktrees, pauses the lagging agent, and resumes after rebasing onto the winning task's changes
4. Carries learnings from one task into related future tasks
5. Tracks progress, decisions, rollback points, and live per-agent telemetry through execution

## Create a migration or project

```bash
keshro create
```

Keshro scans the project, detects whether this is a migration, asks the follow-up questions that matter, and creates the right migration or project with a linked execution context.

If Keshro stops to ask follow-up questions in `/keshro` or another agent session, surface those questions back to the user and resume with the generated `--answers-file` command instead of building a giant shell command by hand.

## Execute

Keshro drives the full execution loop — picks up the next task, gives the agent context, validates the result, marks it done, and moves to the next one. You don't manage it.

```bash
keshro continue
```

By default, `keshro continue` runs the next ready wave in parallel when the environment supports it. Each launched agent gets its own session ID and heartbeats live status back to Keshro, including touched files, progress messages, recent errors, and mid-task conflict detection. Use `--no-parallel` only when you explicitly want one task at a time.

### Executors

Each task in a plan runs on an *executor* — the runtime that actually executes it. Two executors ship today:

- **`local_claude_code`** (default) — a local Claude Code subprocess running inside an isolated git worktree on your machine.
- **`managed_agent`** — Anthropic's hosted Claude Managed Agents (the `managed-agents-2026-04-01` beta). Runs the task in a hosted cloud container instead of a local subprocess. Requires Managed Agents config (Agent ID + Environment ID) in your Keshro Account or Org settings; sessions run against your own Anthropic account.

The executor is resolved per task in this order: `--executor` flag → `task.executor` (set per-task in the plan view) → personal default → workspace default → `local_claude_code` as the safe fallback.

```bash
keshro continue --executor local_claude_code   # Force every task in this run onto local Claude Code
keshro continue --executor managed_agent       # Force every task onto Anthropic Managed Agents
keshro continue --executor local               # Friendly alias
keshro continue --executor managed             # Friendly alias
```

If a task was marked done too early, reopen it with `keshro task reopen <task-id> -p <plan-id>`. That clears any stale blocker and moves it back to `todo` by default, or you can pass `--status in_progress` to resume work immediately.

## Monitor

```bash
keshro status
```

## Works with

Planning, execution, and parallel mode work with [Claude Code](https://claude.ai/code) and [Codex](https://openai.com/index/introducing-codex/). Both agents run in isolated git worktrees during parallel mode. If one agent is rate-limited, Keshro suggests switching and supports a saved default via `keshro config set --agent ...`.

Cursor is supported for in-editor context via `.cursorrules` (`keshro setup-cursor`), but does not have a headless CLI, so it cannot be used as an execution agent.

Keshro can also create general projects from repos, issues, and freeform descriptions, but the primary workflow is migrations.

## License

MIT
