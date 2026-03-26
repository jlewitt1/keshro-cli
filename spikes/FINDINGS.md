# Spike Findings

## Spike 1: PostToolUse hook data
**Status:** NEEDS MANUAL VALIDATION

PostToolUse hooks receive JSON with `tool_input` and `tool_response`.
For Bash, `tool_input.command` contains the command string.
`tool_response` contains at minimum `exit_code`.

**Unknown:** Whether `tool_response` contains full stdout content.

Run `spike1_setup.sh`, use Claude Code, then `spike1_analyze.py` to check.

**If stdout IS available:** Learning extraction can parse agent output directly from hooks.
**If stdout is NOT available:** Use `transcript_path` (if provided) to read the conversation log, or fall back to agents writing structured notes via `keshro task note`.

## Spike 2: Context file re-reading
**Status:** CONFIRMED NEGATIVE (no manual validation needed)

Claude Code reads `.claude/*.md` and `CLAUDE.md` files **only at session start**.
There is no mid-session reload. Multiple open feature requests exist:
- anthropics/claude-code#22085 (auto-reload on context compaction)
- anthropics/claude-code#15858 (RFC: config hot-reload)
- anthropics/claude-code#5513 (add /reloadSettings command)

**Impact on Phase 0.2:**

The context file (`.claude/keshro-context.md`) is still valuable for:
1. **New parallel agents** — each starts a fresh session, reads latest context
2. **New user sessions** — user starts Claude Code, gets current task info
3. **Inter-agent communication** — agent A finishes → daemon updates context → agent B starts

It does NOT work for:
- Updating a currently-running agent mid-session

**Approach for Phase 0.2:**
- Context file remains the primary mechanism (covers use cases 1-3)
- For mid-session updates, explore PreToolUse hook injection (return modified tool inputs
  or decision messages via hook stdout)
- Long-term: wait for Claude Code to ship mid-session reload (tracked upstream)

## Spike 3: Hook-to-daemon latency
**Status:** NEEDS MANUAL VALIDATION

Run `spike3_server.py` + `spike3_client.sh` to measure.

Expected: <10ms for Unix socket round-trip (well under 100ms threshold).
asyncio.start_unix_server is microsecond-level for local IPC.

**If latency > 100ms:** Use fire-and-forget (hook sends and doesn't wait for ack).
