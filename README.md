# Keshro

Migration execution for AI coding agents. Structured plans, parallel execution, and cross-task learning for complex stack moves.

```bash
pip install keshro
keshro login              # opens browser to authenticate
keshro create             # scan project, generate plan
keshro config set --agent codex  # optional: default prompt agent
keshro continue --all     # agents execute in parallel
```

## The problem

Your AI agent is good at one task at a time. Migrations are not one task.

An AWS Batch to Airflow move, Terraform to Pulumi rewrite, or Jenkins to GitHub Actions cutover usually means:
- discovery across the repo and infrastructure definitions
- migration-specific risks and open questions
- staged execution with dependency ordering
- multiple agents touching related code without stepping on each other

Run that manually and you're the bottleneck. Run it in parallel without coordination and agents conflict. The more edge cases the migration has, the worse this gets.

## What it's built for

- **Stack and platform migrations** — Express to Fastify, Terraform to Pulumi, Jenkins to GitHub Actions, AWS Batch to Airflow, Docker Compose to Kubernetes, and similar moves where configuration, runtime behavior, and rollout sequencing all change together.
- **Migration planning with execution attached** — not just “what should we do,” but “what are the blockers, what still needs answering, and what should run first.”
- **Adjacent multi-task engineering work** — monolith decomposition, infrastructure overhauls, auth refactors, and other projects where one agent session cannot hold all the context.

## What Keshro does

1. **Builds a migration-aware plan** — generates a dependency graph with task ordering, file assignments, acceptance criteria, migration risks, and open questions
2. **Runs agents in parallel** — each in an isolated git worktree, respecting dependency order
3. **Shares context across tasks** — when one agent discovers something, related future tasks inherit that knowledge
4. **Tracks everything** — git checkpoints before each task, decision audit trails, one-command rollback

## Create a migration

```bash
keshro create --path aws-batch-to-airflow               # migration template
keshro create --path aws-batch-to-airflow --agent codex # use Codex for discovery
keshro create                                           # detect migration intent from repo + prompt
```

Keshro scans the project, asks follow-up questions, and creates a migration with analysis, risks, open questions, and a linked execution plan.
Use `--agent claude`, `--agent codex`, or save a default with `keshro config set --agent ...`.

## Create from other inputs

```bash
keshro create                                         # current directory
keshro create https://github.com/org/repo             # GitHub repo
keshro create https://github.com/org/repo/issues/42   # GitHub issue
keshro create https://linear.app/team/issue/PROJ-123  # Linear issue
```
Keshro can still create general execution plans from issues, repos, and freeform project descriptions, but migrations are the primary workflow.

## Execute

Keshro drives the full execution loop — picks up the next task, gives the agent context, validates the result, marks it done, and moves to the next one. You don't manage it.

```bash
keshro continue                    # execute next task, then the next, then the next
keshro continue -p <migration-id>  # execute a migration by migration ID
keshro continue --all              # run everything — parallel agents, each in its own worktree
keshro continue --all -c 10        # cap at 10 concurrent agents
keshro continue --dry-run          # preview what would run
keshro continue --no-parallel --agent codex
```

For migration workflows, `-p` accepts the migration ID directly. Keshro resolves it to the linked execution plan behind the scenes.

## Monitor

```bash
keshro status                      # progress summary
keshro status --tui                # live terminal dashboard
keshro migration view <id>         # migration detail, risks, unknowns, linked plan
keshro explain <task-id>           # decision audit trail
keshro rollback <task-id>          # revert to pre-task state
```

## Works with

Parallel execution currently requires [Claude Code](https://claude.ai/code). Planning, migration intake, single-task resume prompts, task tracking, and the web dashboard work with any setup. If Claude is rate-limited during prompt-based flows, Keshro now suggests switching agents and supports a saved default via `keshro config set --agent ...`.

## License

MIT
