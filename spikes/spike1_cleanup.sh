#!/bin/bash
# Spike 1: Remove the test hook from settings.json

SETTINGS_FILE=".claude/settings.json"

if [ ! -f "$SETTINGS_FILE" ]; then
    echo "No settings file found."
    exit 0
fi

if command -v jq &>/dev/null; then
    # Remove any PostToolUse entry that references spike1_hook
    jq 'if .hooks.PostToolUse then
        .hooks.PostToolUse |= [.[] | select(.hooks[0].command | test("spike1") | not)]
    else . end' "$SETTINGS_FILE" > "${SETTINGS_FILE}.tmp" && mv "${SETTINGS_FILE}.tmp" "$SETTINGS_FILE"
else
    python3 -c "
import json
with open('$SETTINGS_FILE') as f:
    settings = json.load(f)
hooks = settings.get('hooks', {}).get('PostToolUse', [])
settings['hooks']['PostToolUse'] = [h for h in hooks if 'spike1' not in str(h)]
with open('$SETTINGS_FILE', 'w') as f:
    json.dump(settings, f, indent=2)
"
fi

echo "✓ Spike 1 hook removed"
echo ""
echo "Results in: /tmp/keshro-spike1.json"
echo "Analyze with: python3 spikes/spike1_analyze.py"
