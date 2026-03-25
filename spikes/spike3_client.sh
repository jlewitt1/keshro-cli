#!/bin/bash
# Spike 3: Test Unix socket round-trip latency.
# Sends 10 JSON payloads to the test server and measures response time.
# Uses $EPOCHREALTIME (Bash 5+) to avoid Python subprocess startup overhead.

SOCKET="/tmp/keshro-spike3.sock"

if [ ! -S "$SOCKET" ]; then
    echo "Socket not found. Start the server first: python spikes/spike3_server.py"
    exit 1
fi

# Check for EPOCHREALTIME support (Bash 5+)
if [ -z "$EPOCHREALTIME" ]; then
    echo "Warning: \$EPOCHREALTIME not available (need Bash 5+). Falling back to Python."
    USE_PYTHON=1
else
    USE_PYTHON=0
fi

echo "Spike 3: Latency test (10 iterations)"
echo ""

TOTAL=0
for i in $(seq 1 10); do
    PAYLOAD='{"hook_event_name":"PostToolUse","tool_name":"Bash","tool_input":{"command":"echo test"},"tool_response":{"exit_code":0}}'

    if [ "$USE_PYTHON" = "1" ]; then
        # Single Python process wrapping the nc call for accurate timing
        ELAPSED=$(python3 -c "
import subprocess, time
start = time.perf_counter_ns()
result = subprocess.run(
    ['nc', '-U', '$SOCKET', '-w', '1'],
    input=b'$PAYLOAD',
    capture_output=True, timeout=5
)
end = time.perf_counter_ns()
print((end - start) // 1_000_000)
" 2>/dev/null)
        RESPONSE=""
    else
        START=$EPOCHREALTIME
        RESPONSE=$(echo "$PAYLOAD" | nc -U "$SOCKET" -w 1 2>/dev/null)
        END=$EPOCHREALTIME
        ELAPSED=$(awk "BEGIN {printf \"%d\", ($END - $START) * 1000}")
    fi

    TOTAL=$(( TOTAL + ELAPSED ))
    echo "  Round $i: ${ELAPSED}ms"
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
