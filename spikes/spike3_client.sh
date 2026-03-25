#!/bin/bash
# Spike 3: Test Unix socket round-trip latency.
# Sends 10 JSON payloads to the test server and measures response time.

SOCKET="/tmp/keshro-spike3.sock"

if [ ! -S "$SOCKET" ]; then
    echo "Socket not found. Start the server first: python spikes/spike3_server.py"
    exit 1
fi

echo "Spike 3: Latency test (10 iterations)"
echo ""

TOTAL=0
for i in $(seq 1 10); do
    PAYLOAD='{"hook_event_name":"PostToolUse","tool_name":"Bash","tool_input":{"command":"echo test"},"tool_response":{"exit_code":0}}'

    START=$(python3 -c "import time; print(int(time.perf_counter_ns()))")

    RESPONSE=$(echo "$PAYLOAD" | nc -U "$SOCKET" -w 1 2>/dev/null)

    END=$(python3 -c "import time; print(int(time.perf_counter_ns()))")
    ELAPSED=$(( (END - START) / 1000000 ))
    TOTAL=$(( TOTAL + ELAPSED ))

    echo "  Round $i: ${ELAPSED}ms (response: $RESPONSE)"
done

AVG=$(( TOTAL / 10 ))
echo ""
echo "Average: ${AVG}ms"
echo ""
if [ "$AVG" -lt 100 ]; then
    echo "✅ SPIKE 3 PASSED: Latency under 100ms threshold"
else
    echo "❌ SPIKE 3 FAILED: Latency exceeds 100ms — use async fire-and-forget"
fi
