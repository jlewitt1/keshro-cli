# Keshro CLI

Thin CLI for the Keshro API. The main command is `keshro continue` which prints an execution prompt for your coding agent to follow.

## Important: Keep docs in sync

When changing CLI commands (adding flags, renaming, changing behavior), always update:
- `../keshro/frontend/src/app/developers/page.tsx` — the `/developers` page command reference
- `README.md` — this repo's README
- `../keshro-mcp/README.md` — if the CLI vs MCP comparison table is affected

## Related repos

- **`../keshro`** — Main backend + frontend
- **`../keshro-mcp`** — Optional MCP server
- **`../batch-to-airflow-demo`** — Demo repo for testing execution loop

## Development

```bash
# Install in dev mode
pip install -e ".[dev]"

# Run tests
python -m pytest tests/test_cli.py -v

# Install globally (editable, picks up source changes)
uv tool install /Users/josh.l/dev/keshro-cli --force --editable

# Build for distribution
python -m build --sdist
```

## Key files

- `src/keshro_cli/cli.py` — All commands. The execution prompt is built by `_build_cli_agent_skill_text()` and `_build_continue_prompt()`.
- `src/keshro_cli/client.py` — HTTP client (httpx) with auth headers
- `src/keshro_cli/config.py` — Auth storage at `~/.keshro/auth.json`
- `src/keshro_cli/auth.py` — Login/logout, token validation
- `tests/test_cli.py` — All tests. Uses `_FakeClient` to mock API responses.

## Architecture

The CLI is a thin HTTP client. It does not contain business logic — all state lives in the Keshro backend API.

`keshro continue` works by:
1. Checking auth (`_ensure_authenticated`)
2. Generating a unique session ID (`agent-<hex>`) for multi-agent tracking
3. Fetching the plan and finding the next task (skips in-progress tasks in parallel mode)
4. Detecting git changes since the last keshro checkpoint (`_get_git_state_summary`)
5. Building session history from completed tasks
6. Printing an execution prompt to stdout
7. The coding agent reads stdout and follows the instructions
8. When stdout is a TTY (not an agent), it shows a short status line instead

Key execution features in the prompt:
- Git checkpoints before each task
- Validation gates before marking done
- Standardized completion note format
- Auto-continue mode (`--all`)
- Parallel agent assignment (default on)
- Error retry for connection failures

## Code conventions

- Python 3.11+, typed
- Typer for CLI framework
- httpx for HTTP
- Tests use monkeypatch + capsys, no external calls

## Publishing

Use the GitHub Action (Actions → "Publish CLI" → Run workflow) or manually:

```bash
python -m build --sdist
curl -X PUT -H "X-Deploy-Secret: $DEPLOY_SECRET" \
  -F "file=@dist/keshro-0.1.0.tar.gz" \
  https://api.keshro.com/api/cli/upload
```
