#!/usr/bin/env python3
"""Spike 3: Unix socket latency test server.

Simulates the daemon's socket listener. Receives JSON, processes it,
returns an ack. Measures round-trip time.
"""

import asyncio
import json
import os
import time
from pathlib import Path

SOCKET_PATH = Path("/tmp/keshro-spike3.sock")


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Handle a single hook script connection."""
    start = time.perf_counter_ns()
    try:
        data = await asyncio.wait_for(reader.read(65536), timeout=5.0)
        if data:
            payload = json.loads(data.decode())
            elapsed_ns = time.perf_counter_ns() - start
            elapsed_ms = elapsed_ns / 1_000_000

            # Simulate daemon processing (match task, etc.)
            response = {"status": "ok", "latency_ms": round(elapsed_ms, 2)}
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()

            print(f"  Received {len(data)} bytes, processed in {elapsed_ms:.2f}ms")
    except Exception as e:
        print(f"  Error: {e}")
    finally:
        writer.close()
        await writer.wait_closed()


async def main():
    # Clean up stale socket
    SOCKET_PATH.unlink(missing_ok=True)

    server = await asyncio.start_unix_server(handle_client, path=str(SOCKET_PATH))
    print(f"Spike 3: Latency test server listening on {SOCKET_PATH}")
    print("Run spike3_client.sh to test. Ctrl+C to stop.\n")

    try:
        async with server:
            await server.serve_forever()
    finally:
        SOCKET_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
