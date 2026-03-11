import time
import webbrowser
from getpass import getpass

import httpx

from .client import get_api_url, print_output
from .config import clear_auth, save_auth


def _browser_login(base_url: str) -> dict:
    with httpx.Client(base_url=base_url, timeout=30) as client:
        start = client.post("/api/auth/cli/start")
        start.raise_for_status()
        flow = start.json()
        verification_url = flow["verification_url"]
        user_code = flow["user_code"]
        print(
            f"Open this URL to sign in: {verification_url}\n" f"CLI code: {user_code}"
        )
        try:
            webbrowser.open(verification_url)
        except Exception:
            pass
        deadline = time.time() + int(flow.get("expires_in", 600))
        interval = int(flow.get("interval", 3))
        while time.time() < deadline:
            res = client.get(
                "/api/auth/cli/poll", params={"device_code": flow["device_code"]}
            )
            res.raise_for_status()
            body = res.json()
            if body.get("status") == "approved" and body.get("token"):
                return body
            if body.get("status") == "expired":
                raise SystemExit("CLI browser login expired. Start again.")
            time.sleep(interval)
    raise SystemExit("CLI browser login timed out. Start again.")


def cmd_auth_login(args):
    if args.token:
        token = args.token.strip()
    elif not args.email and not args.password:
        body = _browser_login(get_api_url(args))
        save_auth(
            {"token": body["token"], "user": body["user"], "api_url": get_api_url(args)}
        )
        if args.json:
            print_output({"status": "ok", "user": body["user"]["email"]}, True)
        else:
            print(f"Successfully logged in to Keshro as {body['user']['email']}.")
        return
    else:
        token = ""

    if not token and args.email and not args.password:
        args.password = getpass("Keshro password: ")
    if not token and bool(args.email) != bool(args.password):
        raise SystemExit("Provide both --email and --password, or use --token.")

    with httpx.Client(base_url=get_api_url(args), timeout=30) as client:
        if token:
            res = client.get(
                "/api/auth/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            res.raise_for_status()
            body = {"token": token, "user": res.json()}
        else:
            res = client.post(
                "/api/auth/login",
                json={"email": args.email, "password": args.password},
            )
            res.raise_for_status()
            body = res.json()
        res.raise_for_status()
        save_auth(
            {"token": body["token"], "user": body["user"], "api_url": get_api_url(args)}
        )
        if args.json:
            print_output({"status": "ok", "user": body["user"]["email"]}, True)
        else:
            print(f"Successfully logged in to Keshro as {body['user']['email']}.")


def cmd_auth_logout(args):
    clear_auth()
    if args.json:
        print_output({"status": "ok", "detail": "Local auth cleared."}, True)
    else:
        print("Logged out of Keshro.")
