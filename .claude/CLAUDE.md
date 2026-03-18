# Keshro CLI

Thin CLI for the Keshro API. The main command is `keshro continue` which prints an execution prompt for Claude Code to follow.

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
2. Fetching the plan and finding the next task
3. Printing an execution prompt to stdout
4. Claude Code reads stdout and follows the instructions
5. When stdout is a TTY (not Claude Code), it shows a short status line instead

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
