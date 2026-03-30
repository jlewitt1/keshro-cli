# Keshro

Plan and run high-stakes engineering projects with AI agents.

```bash
pip install keshro
```

```bash
keshro login              # opens browser to authenticate
keshro create             # scan project, generate plan
keshro config set --agent codex  # optional: default prompt agent
keshro continue --all     # agents execute in parallel
```

Keshro is built for migrations first. It scans the repo, asks the follow-up questions that actually matter for the migration, generates a migration-aware execution plan, then coordinates agents to execute it safely.

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
Use `--agent claude`, `--agent codex`, or save a default with `keshro config set --agent ...`.

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

Parallel execution currently requires [Claude Code](https://claude.ai/code). Planning, migration intake, single-task resume prompts, task tracking, and the web dashboard work with any setup. If Claude is rate-limited during prompt-based flows, Keshro now suggests switching agents and supports a saved default via `keshro config set --agent ...`.

Keshro can also create general execution plans from repos, issues, and freeform descriptions, but the primary workflow is migrations.

## License

MIT
