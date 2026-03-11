import json
import os
from pathlib import Path


DEFAULT_API_URL = os.getenv("KESHRO_API_URL", "http://localhost:8000")
AUTH_PATH = Path.home() / ".keshro" / "auth.json"


def load_auth() -> dict:
    if not AUTH_PATH.exists():
        return {}
    try:
        return json.loads(AUTH_PATH.read_text())
    except Exception:
        return {}


def save_auth(payload: dict) -> None:
    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUTH_PATH.write_text(json.dumps(payload, indent=2))


def clear_auth() -> None:
    if AUTH_PATH.exists():
        AUTH_PATH.unlink()
