import sys
import time

import httpx

from .client import get_api_url, print_output
from .config import clear_auth, load_auth, save_auth

RESET = "\033[0m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"


def _fetch_user(base_url: str, token: str) -> dict:
    with httpx.Client(base_url=base_url, timeout=30) as client:
        res = client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        res.raise_for_status()
        return res.json()


def _browser_login(base_url: str, json_output: bool = False) -> dict | None:
    """Device-code browser login flow. Returns {token, user} or None."""
    import webbrowser

    with httpx.Client(base_url=base_url, timeout=30) as client:
        # Start the device flow
        try:
            res = client.post("/v1/auth/cli/start")
            res.raise_for_status()
            start = res.json()
        except Exception as exc:
            if not json_output:
                print(f"{YELLOW}Browser login not available: {exc}{RESET}")
            return None

        device_code = start["device_code"]
        verification_url = start["verification_url"]
        user_code = start["user_code"]
        interval = start.get("interval", 3)
        expires_in = start.get("expires_in", 600)

        # Open browser
        if not json_output:
            print(f"\n{CYAN}Opening browser to authenticate...{RESET}")
            print(f"{DIM}If the browser doesn't open, visit:{RESET}")
            print(f"  {verification_url}\n")
            print(f"{DIM}Code: {user_code}{RESET}\n")

        try:
            webbrowser.open(verification_url)
        except Exception:
            pass

        # Poll for approval
        deadline = time.time() + expires_in
        frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        i = 0
        while time.time() < deadline:
            if sys.stdout.isatty() and not json_output:
                print(
                    f"\r  {CYAN}{frames[i % len(frames)]}{RESET} Waiting for approval...  ",
                    end="",
                    flush=True,
                )
                i += 1

            time.sleep(interval)

            try:
                res = client.get(
                    "/v1/auth/cli/poll", params={"device_code": device_code}
                )
                res.raise_for_status()
                poll = res.json()
            except Exception:
                continue

            status = poll.get("status", "")
            if status == "approved" and poll.get("token"):
                if sys.stdout.isatty() and not json_output:
                    print(f"\r  {GREEN}✓{RESET} Authenticated!              ")
                return {"token": poll["token"], "user": poll.get("user", {})}
            elif status == "expired":
                if sys.stdout.isatty() and not json_output:
                    print(f"\r  {YELLOW}Session expired.{RESET}              ")
                return None

        if not json_output:
            print(
                f"\n{YELLOW}Login timed out. Try again or use: keshro login <token>{RESET}"
            )
        return None


def cmd_auth_login(
    api_url: str | None = None,
    token: str | None = None,
    json_output: bool = False,
):
    base_url = get_api_url(api_url)
    token = (token or "").strip()
    if not token:
        # Check for existing session
        saved_auth = load_auth()
        saved_token = (saved_auth.get("token") or "").strip()
        if saved_token:
            try:
                user = _fetch_user(base_url, saved_token)
                if json_output:
                    print_output(
                        {
                            "status": "ok",
                            "user": user["email"],
                            "detail": "already_logged_in",
                        },
                        True,
                    )
                else:
                    print(f"Already logged in to Keshro as {user['email']}.")
                return
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {401, 403}:
                    raise

        # No token provided and no valid session — try browser login
        if sys.stdout.isatty():
            result = _browser_login(base_url, json_output)
            if result and result.get("token"):
                token = result["token"]
                user_data = result.get("user", {})
                save_auth({"token": token, "user": user_data, "api_url": base_url})
                email = user_data.get("email", "unknown")
                if json_output:
                    print_output({"status": "ok", "user": email}, True)
                else:
                    print(f"Logged in to Keshro as {email}.")
                # Install agent integrations
                _install_integrations(json_output)
                return

        raise SystemExit(
            "Usage: keshro login <api-token>\n"
            "Or run without a token to authenticate via browser.\n"
            "Get a token from Account -> API at keshro.com"
        )

    user = _fetch_user(base_url, token)
    body = {"token": token, "user": user}
    save_auth({"token": body["token"], "user": body["user"], "api_url": base_url})
    if json_output:
        print_output({"status": "ok", "user": body["user"]["email"]}, True)
    else:
        print(f"Logged in to Keshro as {body['user']['email']}.")

    _install_integrations(json_output)


def _install_integrations(json_output: bool = False):
    """Install agent integrations (Claude Code slash command, Codex instructions)."""
    try:
        from .cli import _install_claude_integration, _install_codex_integration

        claude_target = _install_claude_integration()
        codex_target = _install_codex_integration()
        if not json_output:
            print(f"  {GREEN}✓{RESET} Claude Code: {claude_target}")
            print(f"  {GREEN}✓{RESET} Codex: {codex_target}")
    except Exception:
        pass


def cmd_auth_logout(json_output: bool = False):
    clear_auth()
    if json_output:
        print_output({"status": "ok", "detail": "Local auth cleared."}, True)
    else:
        print("Logged out of Keshro.")
