# Keshro

Plan and run high-stakes engineering projects with AI agents.

```bash
pip install keshro
```

```bash
keshro login              # opens browser to authenticate
keshro create             # scan project, create the right migration/project
keshro continue           # agents execute in parallel if possible
```

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
3. Carries learnings from one task into related future tasks
4. Tracks progress, decisions, and rollback points through execution

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

By default, `keshro continue` runs the next ready wave in parallel when the environment supports it. Use `--no-parallel` only when you explicitly want one task at a time.

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
# Releases

Publish the CLI with one GitHub Actions run after you bump `pyproject.toml`:

```bash
gh workflow run "Publish CLI"
```

That workflow reads the package version from `pyproject.toml`, publishes the package to PyPI, then creates the matching `vX.Y.Z` GitHub release automatically.
