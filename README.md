# keshro-cli

Thin CLI for Keshro's hosted API.

## Install

Users install the CLI with their Keshro API token:

```bash
curl -fsSL https://api.keshro.com/api/cli/install | sh -s -- ksh_pat_...
```

This downloads, installs, and authenticates in one step. Tokens are available in Account -> API in the Keshro app.

### Local development install

```bash
pip install -e ".[dev]"
```

Or with uv:

```bash
uv tool install .
```

## Publishing a new CLI version

### Via GitHub Actions (recommended)

1. Update the version in `pyproject.toml` and push
2. Go to Actions → "Publish CLI" → Run workflow
3. Select production or staging and run

Requires `DEPLOY_SECRET` in the repo's GitHub secrets (must match the `DEPLOY_SECRET` env var on the backend).

### Manual

1. Update the version in `pyproject.toml`
2. Build the sdist:
   ```bash
   python -m build --sdist
   ```
3. Upload to the backend:
   ```bash
   curl -X PUT -H "X-Deploy-Secret: $DEPLOY_SECRET" \
     -F "file=@dist/keshro-0.1.0.tar.gz" \
     https://api.keshro.com/api/cli/upload
   ```

Uploading the same filename overwrites the previous version. The `/api/cli/package` endpoint always serves the newest `keshro-*.tar.gz` file.

## Configure

By default the CLI talks to `https://api.keshro.com`.

Set a different API base URL with:

```bash
export KESHRO_API_URL="http://localhost:8000"
```

## Auth

```bash
keshro login ksh_pat_...     # First-time login
keshro login                 # Reuse saved session
keshro logout
```

Auth state is stored in `~/.keshro/auth.json`.

`keshro login` reuses your saved session when the token in `~/.keshro/auth.json` is still valid, so you only need `keshro login <api-token>` for first-time sign-in or when refreshing an expired session.

## Commands

### Execution

```bash
keshro continue -p <plan-id>                      # Resume next task
keshro continue -p <plan-id> --all                 # Auto-continue through all tasks
keshro continue -p <plan-id> --dir /path/to/repo   # Point agent at a different codebase
keshro continue -p <plan-id> --no-parallel         # Resume your own in-progress task
keshro setup-claude                                # Install global Claude Code slash command
```

Features built into `keshro continue`:
- **Parallel agents** — multiple agents can run `continue` on the same plan; each picks up a different task automatically (default behavior)
- **Session history** — includes completed task summaries so your agent knows what was already done
- **Git checkpoints** — creates a commit before each task so changes can be rolled back
- **Validation gates** — verifies changes (linters, tests, syntax) before marking done
- **Auto-continue** — `--all` flag works through tasks without pausing between them
- **Multi-repo** — `--dir` flag points the agent at a codebase in a different directory

### Plan management

```bash
keshro plan view <plan-id>
keshro plan list
keshro plan update <plan-id> --status ready
keshro plan create --title "..." --source-type "..." --target-type "..."
```

### Task management

```bash
keshro task next -p <plan-id>
keshro task start <task-id> -p <plan-id>
keshro task note <task-id> -p <plan-id> -n "..."
keshro task artifact <task-id> -p <plan-id> -l "<url>"
keshro task block <task-id> -p <plan-id> -r "..."
keshro task unblock <task-id> -p <plan-id>
keshro task done <task-id> -p <plan-id>
keshro migration history <migration-id>
```

## Execution loop behavior

The CLI is designed to let Claude Code or another coding agent keep Keshro current while the work is happening, not after the fact.

Default write-now events:

- `keshro task start`
- `keshro task note`
- `keshro task artifact`
- `keshro task block`
- `keshro task unblock`

Ask first before writing:

- `keshro task done`
- `keshro task delete`
- optional `keshro plan replan-notes` when the change materially alters scope or sequencing
