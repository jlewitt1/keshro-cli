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


def cmd_auth_login(
    api_url: str | None = None,
    token: str | None = None,
    email: str | None = None,
    password: str | None = None,
    json_output: bool = False,
):
    base_url = get_api_url(api_url)
    if token:
        token = token.strip()
    elif not email and not password:
        body = _browser_login(base_url)
        save_auth({"token": body["token"], "user": body["user"], "api_url": base_url})
        if json_output:
            print_output({"status": "ok", "user": body["user"]["email"]}, True)
        else:
            print(f"Successfully logged in to Keshro as {body['user']['email']}.")
        return
    else:
        token = ""

    if not token and email and not password:
        password = getpass("Keshro password: ")
    if not token and bool(email) != bool(password):
        raise SystemExit("Provide both --email and --password, or use --token.")

    with httpx.Client(base_url=base_url, timeout=30) as client:
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
                json={"email": email, "password": password},
            )
            res.raise_for_status()
            body = res.json()
        res.raise_for_status()
        save_auth({"token": body["token"], "user": body["user"], "api_url": base_url})
        if json_output:
            print_output({"status": "ok", "user": body["user"]["email"]}, True)
        else:
            print(f"Successfully logged in to Keshro as {body['user']['email']}.")


def cmd_auth_logout(json_output: bool = False):
    clear_auth()
    if json_output:
        print_output({"status": "ok", "detail": "Local auth cleared."}, True)
    else:
        print("Logged out of Keshro.")
