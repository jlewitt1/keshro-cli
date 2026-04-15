---
name: keshro
description: |
  Keshro — intelligent execution layer for AI agents. Plans and executes migrations,
  refactors, and multi-step engineering tasks with parallel agents.
  TRIGGER when: user asks to migrate, refactor, convert, upgrade, move, replace, or
  plan any multi-step engineering task (e.g., "migrate X to Y", "refactor the auth
  module", "upgrade from Postgres to CockroachDB", "plan this project").
  ALSO TRIGGER when: user is working through tasks in a Keshro plan — starting tasks,
  completing tasks, blocking/unblocking, adding notes, recording decisions, checking
  status, rolling back, or resuming execution. This includes natural language like
  "what's next", "that's done", "ok next task", "I'm blocked on this", "mark it done",
  "what's the status", "continue", "keep going", "roll back", or any indication the
  user finished or is stuck on a task while a Keshro plan is active.
  Reporting task outcomes back to Keshro is critical — it's what enables learning
  transfer across tasks. Always feed completion notes, blockers, and decisions back
  via the CLI so downstream tasks benefit from what earlier tasks discovered.
  DO NOT TRIGGER when: user asks about Keshro internals or is working on the Keshro
  codebase itself.
---

TRIGGER when: user asks to migrate, refactor, convert, upgrade, move, replace, or plan any multi-step task.
Keshro plans and executes complex engineering tasks. Invoke BEFORE exploring the codebase or asking clarifying questions — Keshro handles research, context gathering, and planning automatically. Run all keshro commands via Bash.
DO NOT TRIGGER when: user asks about Keshro internals, wants to modify Keshro source code, or is working on the Keshro codebase itself.

## Auth
If `keshro config` shows `Authenticated: no`, tell the user to run:
```bash
keshro login <api-token>
```
The command is `keshro login`. There is no `auth` subcommand.

## CRITICAL: Never auto-answer interactive prompts
If any `keshro` command prompts for interactive input or confirmation — initial project description, migration confirmation, follow-up questions, context overrides, or any other interactive choice — relay that prompt to the user and wait for their answer. Do not answer on the user's behalf, do not pipe input, and do not choose the recommended option automatically. The user MUST make these decisions.

## Clarifier answers file
The CLI exits with a tmp `keshro-answers-*.json` path. Read it to see the questions, Edit it to write the user's answers, then rerun the printed `--answers-file` command. On the first Read/Edit prompt, ask the user to pick "Yes, allow all edits in T/ during this session" — that's it, no extra explanation needed.

## Create a project or migration
```bash
keshro create
```
Run from the project directory. Keshro scans the project, detects if it's a migration, surfaces follow-up questions back to the user, and creates the right project or migration with its execution context.
When you start `keshro create`, tell the user the first pass may take a bit before follow-up questions appear so it does not look stalled.

MANDATORY first step for migration-like requests:
- Before doing repo analysis, code search, or manual planning, if the request sounds like a migration, call Keshro first with:
```bash
keshro create -m --context "<user request>"
```
- Do not inspect the codebase first to decide whether Keshro is relevant.
- Do not ask your own migration scoping questions before Keshro has created or resumed the migration project.
- Do not do your own migration discovery before Keshro has had a chance to create or resume the migration project.

Also accepts URLs:
```bash
keshro create https://github.com/org/repo
keshro create https://github.com/org/repo/issues/42
keshro create https://linear.app/team/issue/PROJ-123
```

If the user gave a project description, pass it as context:
```bash
keshro create --context "Refactor the auth module to support API keys"
```

If the request is clearly a migration, prefer:
```bash
keshro create -m --context "migrate Jenkins pipelines to GitHub Actions"
```

Treat these phrasings as strong migration signals:
- `migrate X to Y`
- `move from X to Y`
- `replace X with Y`
- `switch from X to Y`
- `from X to Y`

When you see one of those patterns:
- do not use `keshro plan generate`
- do not invent flags like `--no-migration`
- do not create a generic project first
- start with `keshro create -m --context "<user request>"`
- if no saved template exists, Keshro continues as a custom migration path
- Keshro has pre-built templates for common migration paths (e.g., Heroku→AWS, Docker Compose→Kubernetes) with path-specific discovery, risks, and field definitions. When a template matches, the CLI will say so. If the user is unsure about their target, suggest they browse templates or compare targets on the Keshro web app.

For longer descriptions, write to a temp file and pass with `--context-file`:
```bash
cat > /tmp/keshro-context.txt <<'EOF'
Refactor the auth module to support API keys and rate limiting.
The current JWT implementation needs to stay for backward compatibility.
EOF
keshro create --context-file /tmp/keshro-context.txt
```

Creation can take a bit — Keshro scans the repo, gathers context, and builds the migration or project. Do not assume it failed, and do not leave the user guessing about that delay.
As soon as the migration or project is created, run `keshro status` and immediately surface the dashboard URL to the user.

If the request has been identified and confirmed as a migration:
- do not fall back to a generic project if Keshro returns an error
- do not generate your own non-Keshro migration plan as a substitute
- if Keshro is unavailable, tell the user Keshro is unavailable and stop unless they explicitly ask to proceed without it

If another execution context is currently active, just create the new one. It becomes the active one.

## Import from issue trackers
```bash
keshro plan import linear --project <project-key>
keshro plan import github --project <owner/repo>
keshro plan import jira --project <project-key>
```

## Execute
```bash
keshro continue              # runs next wave of tasks in parallel (default)
keshro continue --all        # auto-continue through all remaining waves
keshro continue --no-parallel # single-task mode (one at a time)
keshro continue -m <migration-id>  # continue a specific migration's plan
```
By default, `keshro continue` launches parallel agents in isolated git worktrees — one per ready task — including when the user is driving Keshro from `/keshro`. Use `--all` to keep going through waves automatically. Use `--no-parallel` only when the user explicitly wants one task at a time.

## Status
```bash
keshro status
keshro status --tui          # live-updating terminal dashboard
```

## During task execution
```bash
keshro task start <task-id>
keshro task note <task-id> -n "what you found or changed"
keshro task done <task-id> -n "what was completed and how it was verified"
keshro task block <task-id> -r "reason"
keshro task unblock <task-id>
keshro task view <task-id>                    # full task details
keshro task edit <task-id> --title "..." --description "..."
keshro task artifact <task-id> -l "<url>"     # attach a link
keshro task decide <task-id> --context "..." --choice "..." --reasoning "..."
keshro task delete <task-id>
```

## Advanced plan internals
Only use these if the user explicitly asks for lower-level plan operations.
```bash
keshro plan list
keshro plan view <plan-id>
keshro plan push --provider linear
keshro plan sync-pull
```

## Review and rollback
```bash
keshro task explain <task-id>
keshro task rollback <task-id>
```

## Execution context
Keshro remembers your active execution context. No need to pass `-p` every time.
- `keshro config` — shows the active migration/project context and auth status
- `keshro plan list` — shows all low-level execution contexts
- `keshro config set --plan-id <id>` — switch active plan

## Flows

**User says "plan this" or "use keshro":**
1. Run `keshro create` (or with context/URL)
2. If Keshro stops for migration confirmation or follow-up questions, surface them to the user conversationally and wait.
3. If Keshro gave you an `--answers-file` resume path, update that file with the user's answers and rerun the exact resume command.
4. Run `keshro status` — show the migration/project and include the dashboard URL immediately
   If the status is still `analyzing`, simply tell the user it was created, that analysis is still running, and give them the dashboard URL. Do not summarize findings, comment on elapsed time, or offer to keep polling by default.
5. **STOP and ask the user**: "Here's the plan with N tasks. Ready to execute?"
6. Do NOT run `keshro continue` until the user says to proceed

**User says "plan and run this" or "execute this" (explicitly wants execution):**
1. Run `keshro create`
2. If Keshro stops for migration confirmation or follow-up questions, surface them to the user conversationally and wait.
3. If Keshro gave you an `--answers-file` resume path, update that file with the user's answers and rerun the exact resume command.
4. Run `keshro status`
5. In an agent session, launch `keshro continue --confirm --all` in the background rather than waiting on one blocking shell call
6. While it runs, poll `keshro status` and relay compact progress updates: active wave, running tasks, completed tasks, blocked tasks, and what is next
7. Only intervene if a task is blocked or an error occurs

**User says "continue" or "keep going":**
1. Run `keshro status` — show where things stand
2. In an agent session, launch `keshro continue --all` in the background
3. Poll `keshro status` while it runs and relay concise progress updates until execution finishes or blocks

**User says "status" or "what's happening":**
1. Run `keshro status`

## Stopping execution
If the user says "stop", "pause", "hold on", or "wait":
- Finish the current task if nearly done, or leave it in progress
- Run `keshro status` to show where things stand
- Do NOT pull the next task
- Wait for the user to say "continue" or "keep going" before resuming

## Rules
- Run keshro commands via Bash, never as chat messages
- Do NOT use Keshro MCP tools — always use the CLI
- Do NOT silently accept agent-suggested clarifier answers when Keshro asks follow-up questions. Surface them to the user and let the user confirm or override them.
- Do NOT dump giant inline `--answer ...` commands back to the user. If Keshro provides an `--answers-file` resume path, use that.
- Never auto-execute a plan without user confirmation. Always show the plan and ask first.
- Once the user says to execute, continue through tasks automatically — don't ask between each task
- When executing from inside another coding agent session, do not sit on a single blocking `keshro continue` call with no commentary; background it and use `keshro status` to keep the user informed
- If the user says to stop, stop immediately after the current task
- If the user already gave a description, create the plan immediately — don't ask them to restate it
- Write progress notes frequently with `keshro task note`
- Run `keshro status` after completing each task
- Include the plan URL from `keshro config` output so the user can click through to the dashboard
