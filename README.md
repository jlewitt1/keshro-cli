# Keshro

Make AI agents execute intelligently. Structured plans, parallel execution, cross-task learning.

```bash
pip install keshro
keshro login <token>      # from keshro.com/account
keshro create             # scan project, generate plan
keshro continue --all     # agents execute in parallel
```

## The problem

Your AI agent is great at one task at a time. But real projects have 10-20 tasks with dependencies, shared context, and files that shouldn't be edited by two agents at once.

Run them manually and you're the bottleneck. Run them in parallel and they conflict. The more edge cases a project has, the worse this gets.

## What it's built for

- **Stack migrations** — Express to Fastify, Terraform to Pulumi, Jenkins to GitHub Actions, and more. Framework-specific gotchas, config differences, and breaking changes that cascade if ordered wrong.
- **Monolith decomposition** — extracting services from a shared codebase. Shared database tables, cross-module signals, feature flag dependencies, and more.
- **Infrastructure overhauls** — adding autoscaling, PDBs, topology constraints to Helm charts, and more. Interactions between components that silently break in production.
- **Auth refactors** — replacing custom JWT with NextAuth across dozens of routes, and more. Cookie vs header auth, SSR compatibility, role migration, token scheme changes.
- **Any multi-task engineering project** where the edge cases compound across tasks and one agent session can't hold all the context.

## What Keshro does

1. **Plans the work** — generates a dependency graph with task ordering, file assignments, acceptance criteria, and risk flags
2. **Runs agents in parallel** — each in an isolated git worktree, respecting dependency order
3. **Shares context across tasks** — when one agent discovers something, related future tasks inherit that knowledge
4. **Tracks everything** — git checkpoints before each task, decision audit trails, one-command rollback

## Create from anything

```bash
keshro create                                         # current directory
keshro create https://github.com/org/repo             # GitHub repo
keshro create https://github.com/org/repo/issues/42   # GitHub issue
keshro create https://linear.app/team/issue/PROJ-123  # Linear issue
```

Your AI agent scans the project, answers clarifying questions, and generates the plan.

## Execute

Keshro drives the full execution loop — picks up the next task, gives the agent context, validates the result, marks it done, and moves to the next one. You don't manage it.

```bash
keshro continue                    # execute next task, then the next, then the next
keshro continue --all              # run everything — parallel agents, each in its own worktree
keshro continue --all -c 10        # cap at 10 concurrent agents
keshro continue --dry-run          # preview what would run
```

## Monitor

```bash
keshro status                      # progress summary
keshro status --tui                # live terminal dashboard
keshro explain <task-id>           # decision audit trail
keshro rollback <task-id>          # revert to pre-task state
```

## Works with

Claude Code, Codex, Cursor, Devin, or any AI agent that runs in a terminal.

## License

MIT
