import httpx

from .client import get_api_url, print_output
from .config import clear_auth, load_auth, save_auth


def _fetch_user(base_url: str, token: str) -> dict:
    with httpx.Client(base_url=base_url, timeout=30) as client:
        res = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        res.raise_for_status()
        return res.json()


def cmd_auth_login(
    api_url: str | None = None,
    token: str | None = None,
    json_output: bool = False,
):
    base_url = get_api_url(api_url)
    token = (token or "").strip()
    if not token:
        saved_auth = load_auth()
        saved_token = (saved_auth.get("token") or "").strip()
        if not saved_token:
            raise SystemExit(
                "No saved Keshro session found.\n"
                "Usage: keshro login <api-token>\n"
                "You can create or reuse a token from Account -> API in the Keshro app."
            )
        try:
            user = _fetch_user(base_url, saved_token)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                raise SystemExit(
                    "Your saved Keshro session has expired.\n"
                    "Run: keshro login <api-token>\n"
                    "You can create or reuse a token from Account -> API in the Keshro app."
                ) from exc
            raise
        if json_output:
            print_output(
                {"status": "ok", "user": user["email"], "detail": "already_logged_in"},
                True,
            )
        else:
            print(f"Already logged in to Keshro as {user['email']}.")
        return

    user = _fetch_user(base_url, token)
    body = {"token": token, "user": user}
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
