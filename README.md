# Keshro

Plan and run high-stakes engineering projects with AI agents.

```bash
pip install keshro
```

```bash
keshro login              # opens browser to authenticate
keshro create             # scan project, generate plan
keshro continue           # agents execute in parallel if possible
```

Keshro is built for migrations first. It scans the repo, asks the follow-up questions that actually matter for the migration, generates a migration-aware execution plan, then coordinates agents to execute it safely.

Works with your existing coding agent. Use Claude Code or Codex for planning, migration intake, and parallel execution.

Examples:
- AWS Batch -> Airflow
- Terraform -> Pulumi
- Jenkins -> GitHub Actions
- Express -> Fastify
- Apache Iceberg -> ClickHouse

What Keshro does:
1. Builds a migration-aware plan with risks, open questions, task ordering, and acceptance criteria
2. Runs agents in parallel in isolated git worktrees
3. Carries learnings from one task into related future tasks
4. Tracks progress, decisions, and rollback points through execution

## Create a migration

```bash
keshro create
```

Keshro scans the project, asks follow-up questions, and creates a migration with analysis, risks, open questions, and a linked execution plan.

## Execute

Keshro drives the full execution loop — picks up the next task, gives the agent context, validates the result, marks it done, and moves to the next one. You don't manage it.

```bash
keshro continue
```

## Monitor

```bash
keshro status
```

## Works with

Planning, execution, and parallel mode work with [Claude Code](https://claude.ai/code) and [Codex](https://openai.com/index/introducing-codex/). Both agents run in isolated git worktrees during parallel mode. If one agent is rate-limited, Keshro suggests switching and supports a saved default via `keshro config set --agent ...`.

Cursor is supported for in-editor context via `.cursorrules` (`keshro setup-cursor`), but does not have a headless CLI, so it cannot be used as an execution agent.

Keshro can also create general execution plans from repos, issues, and freeform descriptions, but the primary workflow is migrations.

## License

MIT
