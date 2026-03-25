#!/bin/bash
# Spike 1: Install a PostToolUse hook that logs the full JSON payload.
# This tells us exactly what data Claude Code sends to hooks.

set -e

SETTINGS_DIR=".claude"
SETTINGS_FILE="$SETTINGS_DIR/settings.json"
HOOK_SCRIPT="$(cd "$(dirname "$0")" && pwd)/spike1_hook.sh"
LOG_FILE="/tmp/keshro-spike1.json"

# Clean previous run
rm -f "$LOG_FILE"

# Create .claude dir if needed
mkdir -p "$SETTINGS_DIR"

# Read existing settings or start fresh
if [ -f "$SETTINGS_FILE" ]; then
    EXISTING=$(cat "$SETTINGS_FILE")
else
    EXISTING='{}'
fi

# Add the spike hook using jq if available, otherwise python
if command -v jq &>/dev/null; then
    echo "$EXISTING" | jq --arg script "$HOOK_SCRIPT" '
        .hooks.PostToolUse //= [] |
        .hooks.PostToolUse += [{
            "matcher": "Bash",
            "hooks": [{
                "type": "command",
                "command": $script
            }]
        }]
    ' > "$SETTINGS_FILE"
else
    python3 -c "
import json, sys
settings = json.loads('''$EXISTING''') if '''$EXISTING'''.strip() else {}
hooks = settings.setdefault('hooks', {})
post = hooks.setdefault('PostToolUse', [])
post.append({
    'matcher': 'Bash',
    'hooks': [{'type': 'command', 'command': '$HOOK_SCRIPT'}]
})
json.dump(settings, sys.stdout, indent=2)
" > "$SETTINGS_FILE"
fi

echo "✓ Spike 1 hook installed"
echo "  Settings: $SETTINGS_FILE"
echo "  Hook: $HOOK_SCRIPT"
echo "  Log: $LOG_FILE"
echo ""
echo "Next: Start a Claude Code session and run a bash command."
echo "Then check: cat $LOG_FILE"
