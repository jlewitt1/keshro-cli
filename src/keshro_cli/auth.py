import httpx

from .client import get_api_url, get_token, print_output
from .config import clear_auth, save_auth


def cmd_auth_login(args):
    with httpx.Client(base_url=get_api_url(args), timeout=30) as client:
        res = client.post(
            "/api/auth/login",
            json={"email": args.email, "password": args.password},
        )
        res.raise_for_status()
        body = res.json()
        save_auth({"token": body["token"], "user": body["user"], "api_url": get_api_url(args)})
        print_output({"status": "ok", "user": body["user"]["email"]}, args.json)


def cmd_auth_whoami(args):
    with httpx.Client(base_url=get_api_url(args), timeout=30) as client:
        headers = {}
        token = get_token(args)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        res = client.get("/api/auth/me", headers=headers)
        res.raise_for_status()
        print_output(res.json(), args.json)


def cmd_auth_logout(args):
    clear_auth()
    print_output({"status": "ok", "detail": "Local auth cleared."}, args.json)
