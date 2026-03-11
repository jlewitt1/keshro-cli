import json

import httpx

from .config import DEFAULT_API_URL, load_auth


def get_token(token: str | None = None) -> str:
    return token or load_auth().get("token") or ""


def get_api_url(api_url: str | None = None) -> str:
    return api_url or load_auth().get("api_url") or DEFAULT_API_URL


def get_default_org_id(org_id: str | None = None) -> str:
    return org_id or load_auth().get("default_org_id") or ""


def make_client(api_url: str | None = None, token: str | None = None) -> httpx.Client:
    headers = {"Content-Type": "application/json"}
    resolved_token = get_token(token)
    if resolved_token:
        headers["Authorization"] = f"Bearer {resolved_token}"
    return httpx.Client(base_url=get_api_url(api_url), headers=headers, timeout=30)


def print_output(obj, as_json: bool = False) -> None:
    if as_json or isinstance(obj, (dict, list)):
        print(json.dumps(obj, indent=2))
        return
    print(obj)
