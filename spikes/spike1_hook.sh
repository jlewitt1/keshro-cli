#!/bin/bash
# Spike 1: PostToolUse hook that captures the full JSON payload.
# Logs everything Claude Code sends to /tmp/keshro-spike1.json
#
# This hook is installed by spike1_setup.sh and fires on every Bash tool use.
# It captures stdin (the hook event JSON) to see exactly what fields are available.

LOG="/tmp/keshro-spike1.json"

# Read the full JSON from stdin
INPUT=$(cat)

# Log it with a timestamp
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
echo "{\"timestamp\": \"$TIMESTAMP\", \"payload\": $INPUT}" >> "$LOG"

# Exit 0 = success, don't interfere with Claude Code
exit 0
