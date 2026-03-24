"""Collaborator desktop app integration.

Registers agent sessions with Collaborator so users can visually
monitor all agents working in the Collaborator UI.

Collaborator exposes a JSON-RPC 2.0 server over a Unix domain socket.
Socket path is read from ~/.collaborator/socket-path.
"""

import json
import socket
from pathlib import Path


def _get_socket_path() -> str | None:
    path_file = Path.home() / ".collaborator" / "socket-path"
    if not path_file.exists():
        return None
    return path_file.read_text().strip() or None


def _rpc_call(method: str, params: dict | None = None) -> dict | None:
    """Send a JSON-RPC call to Collaborator. Returns result or None on failure."""
    sock_path = _get_socket_path()
    if not sock_path:
        return None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(sock_path)
        payload = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params:
            payload["params"] = params
        s.sendall(json.dumps(payload).encode() + b"\n")
        data = b""
        while True:
            try:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
                # Check if we got a complete JSON response
                try:
                    json.loads(data)
                    break
                except json.JSONDecodeError:
                    continue
            except socket.timeout:
                break
        s.close()
        if data:
            return json.loads(data).get("result")
        return None
    except Exception:
        return None


def is_available() -> bool:
    """Check if Collaborator is running."""
    result = _rpc_call("ping")
    return bool(result and result.get("pong"))


def session_start(session_id: str, cwd: str) -> bool:
    """Register a new agent session with Collaborator."""
    result = _rpc_call("agent.sessionStart", {
        "session_id": session_id,
        "cwd": cwd,
    })
    return result is not None


def session_end(session_id: str) -> bool:
    """End an agent session in Collaborator."""
    result = _rpc_call("agent.sessionEnd", {
        "session_id": session_id,
    })
    return result is not None


def notify(body: str, title: str = "Keshro") -> bool:
    """Send a native macOS notification via Collaborator."""
    result = _rpc_call("app.notify", {
        "title": title,
        "body": body,
    })
    return result is not None
