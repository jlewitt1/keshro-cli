#!/usr/bin/env python3
"""Spike 1: Analyze the PostToolUse hook payload.

Reads /tmp/keshro-spike1.json and reports what fields are available,
specifically whether tool_response contains stdout content.
"""

import json
import sys
from pathlib import Path

LOG = Path("/tmp/keshro-spike1.json")

if not LOG.exists():
    print("No spike data found. Run spike1_setup.sh first, then use Claude Code.")
    sys.exit(1)

entries = []
for line in LOG.read_text().strip().split("\n"):
    if line.strip():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass

if not entries:
    print("No valid entries in log file.")
    sys.exit(1)

print(f"Found {len(entries)} hook events\n")

for i, entry in enumerate(entries):
    payload = entry.get("payload", {})
    print(f"── Event {i + 1} ──")
    print(f"  hook_event_name: {payload.get('hook_event_name', '?')}")
    print(f"  tool_name:       {payload.get('tool_name', '?')}")
    print(f"  session_id:      {payload.get('session_id', '?')[:16]}...")

    # tool_input
    tool_input = payload.get("tool_input", {})
    print(f"  tool_input keys: {list(tool_input.keys())}")
    if "command" in tool_input:
        cmd = tool_input["command"]
        print(f"  tool_input.command: {cmd[:100]}{'...' if len(cmd) > 100 else ''}")

    # tool_response — this is what we're testing
    tool_response = payload.get("tool_response", {})
    print(f"  tool_response keys: {list(tool_response.keys())}")

    if "exit_code" in tool_response:
        print(f"  tool_response.exit_code: {tool_response['exit_code']}")

    # Check for stdout content
    for key in ["stdout", "output", "content", "text", "result"]:
        if key in tool_response:
            val = str(tool_response[key])
            print(
                f"  tool_response.{key}: {val[:200]}{'...' if len(val) > 200 else ''}"
            )

    # If we can't find stdout, dump all tool_response to see what's there
    known_keys = {"exit_code", "stdout", "output", "content", "text", "result"}
    unknown_keys = set(tool_response.keys()) - known_keys
    if unknown_keys:
        print(f"  tool_response OTHER keys: {unknown_keys}")
        for k in unknown_keys:
            val = str(tool_response[k])
            print(f"    .{k}: {val[:200]}{'...' if len(val) > 200 else ''}")

    print()

# Summary
print("=" * 60)
print("SPIKE 1 RESULT:")
has_stdout = any(
    any(
        k in entry.get("payload", {}).get("tool_response", {})
        for k in ["stdout", "output", "content", "text", "result"]
    )
    for entry in entries
)
if has_stdout:
    print("✅ PostToolUse includes stdout/output content")
    print("   → Learning extraction via hooks is FEASIBLE")
else:
    print("❌ PostToolUse does NOT include stdout content")
    print("   → Need fallback: agent writes learnings to file, or use transcript_path")
    # Check if transcript_path is available
    has_transcript = any(
        "transcript_path" in entry.get("payload", {}) for entry in entries
    )
    if has_transcript:
        tp = entries[0].get("payload", {}).get("transcript_path", "")
        print(f"   → transcript_path available: {tp}")
        print("   → Fallback: parse transcript file for learnings")
