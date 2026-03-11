import json

import httpx

from .config import DEFAULT_API_URL, load_auth


def get_token(args) -> str:
    return (
        getattr(args, "token", None)
        or load_auth().get("token")
        or ""
    )


def get_api_url(args) -> str:
    return getattr(args, "api_url", None) or load_auth().get("api_url") or DEFAULT_API_URL


def make_client(args) -> httpx.Client:
    headers = {"Content-Type": "application/json"}
    token = get_token(args)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=get_api_url(args), headers=headers, timeout=30)


def print_output(obj, as_json: bool = False) -> None:
    if as_json or isinstance(obj, (dict, list)):
        print(json.dumps(obj, indent=2))
        return
    print(obj)
