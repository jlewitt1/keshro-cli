# Spike Validation for Phase 0.2

Three spikes must be validated before building Claude Code hook integration.

## How to run

Each spike has a setup script and a validation script. Run them in order.

### Spike 1: PostToolUse hook data
Tests whether PostToolUse hooks receive full Bash stdout in `tool_response`.

```bash
# 1. Install the test hook
./spikes/spike1_setup.sh

# 2. Start a Claude Code session and run a bash command
claude  # then ask it to run: echo "hello from spike test"

# 3. Check the log
cat /tmp/keshro-spike1.json

# 4. Clean up
./spikes/spike1_cleanup.sh
```

**Expected:** `/tmp/keshro-spike1.json` contains a JSON object with `tool_response`
that includes the stdout content from the bash command.

### Spike 2: Context file re-reading
Tests whether Claude Code re-reads `.claude/*.md` files mid-session.

**Result: CONFIRMED NEGATIVE (via documentation research)**

Claude Code only reads CLAUDE.md and `.claude/*.md` at session start.
Multiple open feature requests exist (anthropics/claude-code#22085, #15858).
No mid-session reload is available.

**Impact:** The context file (`.claude/keshro-context.md`) works for:
- New parallel agents starting fresh sessions
- New user sessions
- Inter-agent communication between sessions

It does NOT work for updating a currently-running agent mid-session.

**Workaround for Phase 0.2:** Use PreToolUse hooks to inject context by returning
a `decision` field in hook stdout (exit 0 + JSON). This lets the daemon communicate
with a running agent through the hook system instead of the context file.

### Spike 3: Hook-to-daemon latency
Tests Unix socket round-trip time from a hook script to the daemon.

```bash
# 1. Start the latency test server
python spikes/spike3_server.py &

# 2. Run the latency test
./spikes/spike3_client.sh

# 3. Check results (should be <100ms)
# Output prints latency per call
```
