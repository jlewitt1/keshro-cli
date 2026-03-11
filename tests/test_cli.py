import json
from pathlib import Path

import pytest

from keshro_cli import __version__
from keshro_cli import cli


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if path == "/api/plans/templates":
            return _FakeResponse([{
                "key": "aws-batch-to-airflow",
                "title": "AWS Batch to Airflow",
                "summary": "Saved migration template for AWS Batch to Airflow.",
                "why_use_it": "Separate scheduling/orchestration logic from the containerized job payload first.",
                "plan_steps": [
                    {"title": "Capture current AWS Batch migration context"},
                    {"title": "Capture migration outcome and follow-up work"},
                ],
            }])
        return _FakeResponse({"ok": True})

    def post(self, path, json=None):
        self.calls.append(("POST", path, json))
        return _FakeResponse({"path": path, "payload": json})

    def patch(self, path, json=None):
        self.calls.append(("PATCH", path, json))
        return _FakeResponse({"path": path, "payload": json})


@pytest.fixture
def fake_client(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(cli, "make_client", lambda args: client)
    return client


def test_whoami_command_removed_from_parser():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["whoami"])


def test_main_without_args_prints_version(capsys):
    cli.main([])
    assert capsys.readouterr().out.strip() == __version__


def test_templates_alias_lists_template_ids_by_default(fake_client, capsys):
    cli.main(["templates"])
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["aws-batch-to-airflow"]


def test_templates_alias_supports_verbose_output(fake_client, capsys):
    cli.main(["templates", "--verbose"])
    out = capsys.readouterr().out
    assert "aws-batch-to-airflow" in out
    assert "AWS Batch to Airflow" in out


def test_templates_alias_json_output(fake_client, capsys):
    cli.main(["--json", "templates"])
    out = json.loads(capsys.readouterr().out)
    assert out[0]["key"] == "aws-batch-to-airflow"


def test_templates_alias_can_show_single_template_details(fake_client, capsys):
    cli.main(["templates", "aws-batch-to-airflow"])
    out = capsys.readouterr().out
    assert "aws-batch-to-airflow" in out
    assert "Title:" in out
    assert "Plan steps:" in out


def test_templates_alias_can_show_single_template_details_with_name_flag(fake_client, capsys):
    cli.main(["templates", "-n", "aws-batch-to-airflow"])
    out = capsys.readouterr().out
    assert "aws-batch-to-airflow" in out
    assert "Why use it:" in out


def test_plan_create_from_template_hits_template_endpoint(fake_client, capsys):
    cli.main(
        [
            "--json",
            "plan",
            "create",
            "--from-template",
            "aws-batch-to-airflow",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/api/plans/from-template"
    assert out["payload"]["template_key"] == "aws-batch-to-airflow"


def test_plan_create_from_claude_markdown_parses_steps(
    fake_client, tmp_path: Path, capsys
):
    source = tmp_path / "claude.md"
    source.write_text(
        "# AWS Batch to Airflow\n\n"
        "- Inventory Batch jobs\n"
        "- Map schedules to DAGs\n"
    )
    cli.main(
        [
            "--json",
            "plan",
            "create",
            "--from-claude",
            str(source),
        ]
    )
    out = json.loads(capsys.readouterr().out)
    payload = out["payload"]
    assert out["path"] == "/api/plans"
    assert payload["title"] == "AWS Batch to Airflow"
    assert payload["source_type"] == "AWS Batch"
    assert payload["target_type"] == "Airflow"
    assert payload["import_source"] == "claude"
    assert len(payload["plan_steps"]) == 2


def test_plan_task_alias_posts_task_update(fake_client, capsys):
    cli.main(
        [
            "--json",
            "plan_task",
            "plan-123",
            "--title",
            "Validate pilot DAG",
            "--description",
            "Run the pilot DAG end to end",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/api/plans/plan-123/tasks"
    assert out["payload"]["title"] == "Validate pilot DAG"


def test_edit_task_alias_patches_existing_task(fake_client, capsys):
    cli.main(
        [
            "--json",
            "edit_task",
            "plan-123",
            "task-456",
            "--status",
            "in_progress",
            "--owner",
            "Platform team",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/api/plans/plan-123/tasks/task-456"
    assert out["payload"]["status"] == "in_progress"


def test_config_prints_saved_auth_metadata(monkeypatch, capsys):
    monkeypatch.setattr(
        "keshro_cli.cli.load_auth",
        lambda: {
            "api_url": "https://app.keshro.test",
            "token": "jwt-123",
            "user": {"email": "cli@example.com", "name": "CLI User"},
        },
    )

    cli.main(["config"])
    out = capsys.readouterr().out
    assert "API URL:" in out
    assert "https://app.keshro.test" in out
    assert "Authenticated:" in out
    assert "yes" in out
    assert "User:" in out
    assert "cli@example.com" in out


def test_auth_login_with_token_prints_human_text_by_default(monkeypatch, capsys):
    saved = {}

    class _AuthClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, headers=None):
            assert path == "/api/auth/me"
            assert headers == {"Authorization": "Bearer ksh_pat_test"}
            return _FakeResponse({"email": "cli@example.com", "id": "user-1"})

    monkeypatch.setattr("keshro_cli.auth.httpx.Client", lambda **kwargs: _AuthClient())
    monkeypatch.setattr("keshro_cli.auth.save_auth", lambda payload: saved.update(payload))

    cli.main(["login", "--token", "ksh_pat_test"])
    out = capsys.readouterr().out.strip()
    assert out == "Successfully logged in to Keshro as cli@example.com."
    assert saved["token"] == "ksh_pat_test"


def test_auth_login_with_token_validates_with_auth_me(monkeypatch, capsys):
    saved = {}

    class _AuthClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, headers=None):
            assert path == "/api/auth/me"
            assert headers == {"Authorization": "Bearer ksh_pat_test"}
            return _FakeResponse({"email": "cli@example.com", "id": "user-1"})

    monkeypatch.setattr("keshro_cli.auth.httpx.Client", lambda **kwargs: _AuthClient())
    monkeypatch.setattr("keshro_cli.auth.save_auth", lambda payload: saved.update(payload))

    cli.main(["--json", "login", "--token", "ksh_pat_test"])
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert saved["token"] == "ksh_pat_test"


def test_config_json_outputs_machine_readable_metadata(monkeypatch, capsys):
    monkeypatch.setattr(
        "keshro_cli.cli.load_auth",
        lambda: {
            "api_url": "https://app.keshro.test",
            "token": "jwt-123",
            "user": {"email": "cli@example.com"},
        },
    )

    cli.main(["--json", "config"])
    out = json.loads(capsys.readouterr().out)
    assert out["api_url"] == "https://app.keshro.test"
    assert out["authenticated"] is True
    assert out["user"]["email"] == "cli@example.com"


def test_auth_login_without_args_uses_browser_flow(monkeypatch, capsys):
    saved = {}

    class _BrowserClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json=None):
            assert path == "/api/auth/cli/start"
            return _FakeResponse(
                {
                    "device_code": "device-123",
                    "user_code": "ABCD1234",
                    "verification_url": "http://localhost:3000/auth?cli_code=ABCD1234&mode=login",
                    "expires_in": 60,
                    "interval": 0,
                }
            )

        def get(self, path, params=None, headers=None):
            assert path == "/api/auth/cli/poll"
            assert params == {"device_code": "device-123"}
            return _FakeResponse(
                {
                    "status": "approved",
                    "token": "jwt-123",
                    "user": {"email": "cli@example.com"},
                }
            )

    monkeypatch.setattr("keshro_cli.auth.httpx.Client", lambda **kwargs: _BrowserClient())
    monkeypatch.setattr("keshro_cli.auth.save_auth", lambda payload: saved.update(payload))
    monkeypatch.setattr("keshro_cli.auth.webbrowser.open", lambda url: True)
    monkeypatch.setattr("keshro_cli.auth.time.sleep", lambda _: None)

    cli.main(["--json", "login"])
    out_text = capsys.readouterr().out
    out = json.loads(out_text[out_text.index("{") :])
    assert out["status"] == "ok"
    assert saved["token"] == "jwt-123"
