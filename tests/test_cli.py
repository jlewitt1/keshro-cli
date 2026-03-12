import json
from pathlib import Path

import httpx
import pytest

from keshro_cli import __version__, cli


def _auth_with_org():
    return {
        "default_org_id": "org-456",
        "default_org_name": "Demo Inc",
    }


def _auth_with_plan():
    return {
        "default_plan_id": "plan-123",
        "default_plan_title": "AWS Batch to Airflow pilot",
    }


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
            return _FakeResponse(
                [
                    {
                        "key": "aws-batch-to-airflow",
                        "title": "AWS Batch to Airflow",
                        "summary": "Saved migration template for AWS Batch to Airflow.",
                        "why_use_it": "Separate scheduling/orchestration logic from the containerized job payload first.",
                        "plan_steps": [
                            {"title": "Capture current AWS Batch migration context"},
                            {"title": "Capture migration outcome and follow-up work"},
                        ],
                    }
                ]
            )
        if path == "/api/orgs":
            return _FakeResponse(
                [
                    {"id": "org-123", "name": "Acme"},
                    {"id": "org-456", "name": "Demo Inc"},
                ]
            )
        if path == "/api/migrations":
            return _FakeResponse(
                [
                    {
                        "id": "migration-123",
                        "status": "completed",
                        "source_type": "AWS Batch",
                        "target_type": "Airflow",
                        "migration_mode": "software",
                        "input_method": "context",
                        "created_at": "2026-03-11T15:30:00Z",
                        "confidence_score": 72,
                        "outcome_status": "partial",
                        "org_id": "org-123",
                    }
                ]
            )
        if path == "/api/migrations/migration-123":
            return _FakeResponse(
                {
                    "id": "migration-123",
                    "status": "completed",
                    "source_type": "AWS Batch",
                    "target_type": "Airflow",
                    "migration_mode": "software",
                    "input_method": "context",
                    "created_at": "2026-03-11T15:30:00Z",
                    "confidence_score": 72,
                    "confidence_explanation": "Good source coverage and prior migration matches.",
                    "effort_estimate": {"total_hours": 36},
                    "cost_estimate": {"total_cost_low": 5000, "total_cost_high": 9000},
                    "notes": "Pilot DAG first, then full cutover.",
                    "migration_steps": [
                        {
                            "order": 1,
                            "title": "Inventory Batch jobs",
                            "description": "Review queues and job definitions",
                        }
                    ],
                    "outcome_status": "partial",
                    "org_id": "org-123",
                }
            )
        if path == "/api/plans":
            return _FakeResponse(
                [
                    {
                        "id": "plan-123",
                        "title": "AWS Batch to Airflow pilot",
                        "status": "draft",
                        "source_type": "AWS Batch",
                        "target_type": "Airflow",
                        "summary": "Pilot plan for the first DAG migration.",
                        "template_key": "aws-batch-to-airflow",
                        "org_id": "org-123",
                        "updated_at": "2026-03-11T15:30:00Z",
                    }
                ]
            )
        if path == "/api/plans/plan-123":
            return _FakeResponse(
                {
                    "id": "plan-123",
                    "title": "AWS Batch to Airflow pilot",
                    "status": "draft",
                    "source_type": "AWS Batch",
                    "target_type": "Airflow",
                    "summary": "Pilot plan for the first DAG migration.",
                    "template_key": "aws-batch-to-airflow",
                    "import_source": "template",
                    "org_id": "org-123",
                    "updated_at": "2026-03-11T15:30:00Z",
                    "plan_steps": [
                        {
                            "id": "review-schedules",
                            "order": 1,
                            "title": "Review EventBridge schedules",
                            "description": "Map cron schedules into DAG schedules",
                            "status": "todo",
                            "owner": None,
                            "notes": None,
                            "blocked_reason": "Waiting on environment access",
                            "artifact_links": [
                                "https://linear.app/acme/issue/ENG-42",
                                "https://github.com/acme/migrations/pull/19",
                            ],
                        }
                    ],
                }
            )
        return _FakeResponse({"ok": True})

    def post(self, path, json=None):
        self.calls.append(("POST", path, json))
        if path == "/api/plans/from-template":
            return _FakeResponse(
                {
                    "id": "plan-123",
                    "title": "AWS Batch to Airflow pilot",
                    "status": "draft",
                    "source_type": "AWS Batch",
                    "target_type": "Airflow",
                    "summary": "Pilot plan for the first DAG migration.",
                    "template_key": json.get("template_key"),
                    "migration_id": json.get("migration_id"),
                }
            )
        if path == "/api/plans":
            return _FakeResponse(
                {
                    "id": "plan-123",
                    "title": json.get("title") or "Execution Plan",
                    "status": json.get("status") or "draft",
                    "migration_id": json.get("migration_id"),
                    "plan_steps": json.get("plan_steps") or [],
                    "import_source": json.get("import_source"),
                }
            )
        if path.endswith("/tasks"):
            return _FakeResponse(
                {
                    "id": "plan-123",
                    "title": "AWS Batch to Airflow pilot",
                    "plan_steps": [
                        {
                            "id": "task-999",
                            "title": json.get("title"),
                            "status": json.get("status") or "todo",
                            "owner": json.get("owner"),
                            "blocked_reason": json.get("blocked_reason"),
                            "artifact_links": json.get("artifact_links") or [],
                        }
                    ],
                }
            )
        return _FakeResponse({"path": path, "payload": json})

    def patch(self, path, json=None):
        self.calls.append(("PATCH", path, json))
        return _FakeResponse({"path": path, "payload": json})

    def request(self, method, path, json=None):
        self.calls.append((method, path, json))
        if method == "DELETE" and path.endswith("/tasks/task-456"):
            return _FakeResponse({"id": "plan-123", "plan_steps": [], "payload": json})
        return _FakeResponse({"success": True, "path": path, "payload": json})

    def delete(self, path):
        return self.request("DELETE", path, json=None)


@pytest.fixture
def fake_client(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(cli, "make_client", lambda api_url=None, token=None: client)
    return client


def test_whoami_is_not_a_valid_command():
    code = cli.main(["whoami"])
    assert code != 0


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


def test_templates_alias_can_show_single_template_details_with_name_flag(
    fake_client, capsys
):
    cli.main(["templates", "-n", "aws-batch-to-airflow"])
    out = capsys.readouterr().out
    assert "aws-batch-to-airflow" in out
    assert "Why use it:" in out


def test_templates_list_alias_is_accepted(fake_client, capsys):
    cli.main(["templates", "list"])
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["aws-batch-to-airflow"]


def test_plan_create_from_template_hits_template_endpoint(fake_client, capsys):
    cli.main(
        [
            "--json",
            "plan",
            "create",
            "migration-123",
            "-T",
            "aws-batch-to-airflow",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == "plan-123"
    assert out["template_key"] == "aws-batch-to-airflow"
    assert out["migration_id"] == "migration-123"


def test_plan_create_saves_default_plan_automatically(fake_client, monkeypatch, capsys):
    saved = {}
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: {})
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: {})
    monkeypatch.setattr(
        "keshro_cli.cli.update_auth",
        lambda payload: saved.update(payload) or payload,
    )
    cli.main(
        [
            "plan",
            "create",
            "migration-123",
            "-T",
            "aws-batch-to-airflow",
        ]
    )
    out = capsys.readouterr().out
    assert "Saved default plan:" in out
    assert saved["default_plan_id"]
    assert saved["default_plan_title"]


def test_plan_create_does_not_save_default_plan_in_json_mode(
    fake_client, monkeypatch, capsys
):
    saved = {}
    monkeypatch.setattr(
        "keshro_cli.cli.update_auth",
        lambda payload: saved.update(payload) or payload,
    )
    cli.main(
        [
            "--json",
            "plan",
            "create",
            "migration-123",
            "-T",
            "aws-batch-to-airflow",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == "plan-123"
    assert saved == {}


def test_migration_list_is_concise_by_default(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: {})
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: {})
    cli.main(["migration", "list"])
    out = capsys.readouterr().out
    assert "ID" in out
    assert "PATH" in out
    assert "STATUS" in out
    assert "CREATED" in out
    assert "migration-123" in out
    assert "AWS Batch -> Airflow" in out
    assert "2026-03-11T15:30:00Z" in out
    assert "partial" not in out


def test_migration_list_verbose_includes_metadata(fake_client, capsys):
    cli.main(["migration", "list", "--verbose"])
    out = capsys.readouterr().out
    assert "Outcome:" in out
    assert "partial" in out
    assert "Confidence:" in out


def test_migration_list_latest_limits_rows(fake_client, capsys):
    cli.main(["migration", "list", "--latest", "1"])
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 3
    assert "migration-123" in out[-1]


def test_migration_view_shows_detail(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_org)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_org)
    cli.main(["migration", "view", "migration-123"])
    out = capsys.readouterr().out
    assert "migration-123" in out
    assert "Confidence explanation:" in out
    assert "Effort:" in out
    assert "Cost:" in out
    assert "Steps:" in out


def test_migration_delete_hits_endpoint(fake_client, capsys):
    cli.main(["migration", "delete", "migration-123"])
    out = capsys.readouterr().out.strip()
    assert out == "Deleted migration migration-123."
    assert ("DELETE", "/api/migrations/migration-123", None) in fake_client.calls


def test_migration_list_json_outputs_machine_readable_rows(fake_client, capsys):
    cli.main(["--json", "migration", "list"])
    out = json.loads(capsys.readouterr().out)
    assert out[0]["id"] == "migration-123"
    assert out[0]["source_type"] == "AWS Batch"


def test_plan_list_is_concise_by_default(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: {})
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: {})
    cli.main(["plan", "list"])
    out = capsys.readouterr().out
    assert "ID" in out
    assert "TITLE" in out
    assert "STATUS" in out
    assert "UPDATED" in out
    assert "plan-123" in out
    assert "AWS Batch to Airflow pilot" in out
    assert "AWS Batch -> Airflow" in out
    assert "2026-03-11T15:30:00Z" in out
    assert "Pilot plan for the first DAG migration." not in out
    assert "for org" not in out


def test_plan_list_verbose_includes_summary_and_timestamp(fake_client, capsys):
    cli.main(["plan", "list", "--verbose"])
    out = capsys.readouterr().out
    assert "Pilot plan for the first DAG migration." in out
    assert "Updated:" in out
    assert "2026-03-11T15:30:00Z" in out


def test_plan_list_latest_limits_rows(fake_client, capsys):
    cli.main(["plan", "list", "--latest", "1"])
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 3
    assert "plan-123" in out[-1]


def test_plan_list_empty_state_shows_org_context(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_org)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_org)

    class _EmptyClient(_FakeClient):
        def get(self, path, params=None):
            if path == "/api/plans":
                return _FakeResponse([])
            return super().get(path, params=params)

    monkeypatch.setattr(
        cli, "make_client", lambda api_url=None, token=None: _EmptyClient()
    )
    cli.main(["plan", "list"])
    out = capsys.readouterr().out.strip()
    assert out == "No plans found for org Demo Inc."


def test_plan_view_shows_active_org_context(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_org)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_org)
    cli.main(["plan", "view", "plan-123"])
    out = capsys.readouterr().out
    assert "for org Demo Inc" in out


def test_plan_view_is_human_readable_by_default(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: {})
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: {})
    cli.main(["plan", "view", "plan-123"])
    out = capsys.readouterr().out
    assert "AWS Batch to Airflow pilot" in out
    assert "Steps:" in out
    assert "task-id: review-schedules" in out
    assert "Owner: Unassigned" in out
    assert "Map cron schedules into DAG schedules" in out
    assert "Blocked: Waiting on environment access" in out
    assert "Artifacts:" in out
    assert "https://github.com/acme/migrations/pull/19" in out
    assert "for org" not in out


def test_task_view_shows_plan_association(fake_client, capsys):
    cli.main(["task", "view", "plan-123", "review-schedules"])
    out = capsys.readouterr().out
    assert "AWS Batch to Airflow pilot" in out
    assert "plan-123" in out
    assert "Review EventBridge schedules" in out
    assert "review-schedules" in out


def test_task_delete_accepts_plan_id_option(fake_client, capsys):
    cli.main(["--json", "task", "delete", "task-456", "-p", "plan-123"])
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == "plan-123"
    assert out["plan_steps"] == []


def test_plan_task_delete_uses_saved_plan_context(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    cli.main(["plan", "task", "delete", "task-456"])
    out = capsys.readouterr().out
    assert "Deleted task task-456 from plan plan-123." in out


def test_plan_task_view_uses_saved_plan_context(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    cli.main(["plan", "task", "view", "review-schedules"])
    out = capsys.readouterr().out
    assert "plan-123" in out
    assert "review-schedules" in out


def test_plan_delete_calls_delete_endpoint(fake_client, capsys):
    cli.main(["--json", "plan", "delete", "plan-123"])
    out = json.loads(capsys.readouterr().out)
    assert out["success"] is True
    assert out["path"] == "/api/plans/plan-123"


def test_json_flag_can_appear_after_subcommand(fake_client, capsys):
    cli.main(["plan", "list", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out[0]["id"] == "plan-123"


def test_config_set_persists_default_org_by_id(monkeypatch, capsys):
    monkeypatch.setattr(
        "keshro_cli.cli.update_auth",
        lambda payload: {"api_url": "http://localhost:8000", **payload},
    )
    code = cli.main(["config", "set", "--org-id", "org-123"])
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == "Saved default context: org-123"


def test_config_set_can_resolve_default_org_by_name(fake_client, monkeypatch, capsys):
    monkeypatch.setattr(
        "keshro_cli.cli.update_auth",
        lambda payload: {"api_url": "http://localhost:8000", **payload},
    )
    code = cli.main(["config", "set", "--org", "Acme"])
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == "Saved default context: Acme"


def test_config_set_can_resolve_default_org_by_partial_name(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setattr(
        "keshro_cli.cli.update_auth",
        lambda payload: {"api_url": "http://localhost:8000", **payload},
    )
    code = cli.main(["config", "set", "--org", "demo"])
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == "Saved default context: Demo Inc"


def test_config_set_can_save_default_plan(fake_client, monkeypatch, capsys):
    monkeypatch.setattr(
        "keshro_cli.cli.update_auth",
        lambda payload: {"api_url": "http://localhost:8000", **payload},
    )
    code = cli.main(["config", "set", "-p", "plan-123"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Saved default context: personal" in out
    assert "Saved default plan: AWS Batch to Airflow pilot" in out


def test_config_set_can_save_api_url(monkeypatch, capsys):
    monkeypatch.setattr(
        "keshro_cli.cli.update_auth",
        lambda payload: {"default_org_id": None, "default_org_name": None, **payload},
    )
    code = cli.main(["config", "set", "--api-url", "https://api.keshro.test"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Saved default context: personal" in out
    assert "Saved API URL: https://api.keshro.test" in out


def test_config_set_short_alias_can_save_api_url(monkeypatch, capsys):
    monkeypatch.setattr(
        "keshro_cli.cli.update_auth",
        lambda payload: {"default_org_id": None, "default_org_name": None, **payload},
    )
    code = cli.main(["config", "set", "-u", "https://api.keshro.test"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Saved API URL: https://api.keshro.test" in out


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
            "migration-123",
            "-c",
            str(source),
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == "plan-123"
    assert out["title"] == "AWS Batch to Airflow"
    assert out["migration_id"] == "migration-123"
    assert out["import_source"] == "claude"
    assert len(out["plan_steps"]) == 2


def test_plan_create_accepts_positional_migration_id(fake_client, capsys):
    cli.main(
        [
            "--json",
            "plan",
            "create",
            "migration-123",
            "--title",
            "AWS Batch to Airflow",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["migration_id"] == "migration-123"
    assert out["title"] == "AWS Batch to Airflow"


def test_task_plan_posts_task_update(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "plan",
            "plan-123",
            "--title",
            "Validate pilot DAG",
            "--description",
            "Run the pilot DAG end to end",
            "--blocked-reason",
            "Waiting on staging DAG deployment",
            "--link",
            "https://linear.app/acme/issue/ENG-42",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == "plan-123"
    assert out["plan_steps"][0]["title"] == "Validate pilot DAG"
    assert out["plan_steps"][0]["blocked_reason"] == "Waiting on staging DAG deployment"
    assert out["plan_steps"][0]["artifact_links"] == [
        "https://linear.app/acme/issue/ENG-42"
    ]


def test_task_edit_patches_existing_task(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "edit",
            "plan-123",
            "task-456",
            "-s",
            "in_progress",
            "-o",
            "Platform team",
            "-l",
            "https://github.com/acme/migrations/pull/19",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/api/plans/plan-123/tasks/task-456"
    assert out["payload"]["status"] == "in_progress"
    assert out["payload"]["artifact_links"] == [
        "https://github.com/acme/migrations/pull/19"
    ]


def test_task_edit_accepts_blocked_reason_short_alias_r(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "edit",
            "plan-123",
            "task-456",
            "-s",
            "blocked",
            "-r",
            "Waiting on Airflow access",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/api/plans/plan-123/tasks/task-456"
    assert out["payload"]["status"] == "blocked"
    assert out["payload"]["blocked_reason"] == "Waiting on Airflow access"


def test_task_edit_accepts_feedback_reason(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "edit",
            "plan-123",
            "task-456",
            "--reason",
            "Needed a more implementation-specific task",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert (
        out["payload"]["feedback_reason"]
        == "Needed a more implementation-specific task"
    )


def test_task_edit_uses_saved_plan_context(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    cli.main(
        [
            "--json",
            "task",
            "edit",
            "task-456",
            "--status",
            "in_progress",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/api/plans/plan-123/tasks/task-456"
    assert out["payload"]["status"] == "in_progress"


def test_task_edit_accepts_plan_id_option(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "edit",
            "task-456",
            "--plan-id",
            "plan-123",
            "--status",
            "in_progress",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/api/plans/plan-123/tasks/task-456"


def test_task_delete_accepts_feedback_reason(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "delete",
            "plan-123",
            "task-456",
            "--reason",
            "not_relevant",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["payload"]["feedback_reason"] == "not_relevant"


def test_task_plan_human_output_shows_owner(fake_client, capsys):
    cli.main(
        [
            "task",
            "plan",
            "plan-123",
            "--title",
            "Validate pilot DAG",
            "--description",
            "Run the pilot DAG end to end",
            "--owner",
            "Platform team",
        ]
    )
    out = capsys.readouterr().out
    assert "Task:" in out
    assert "Task ID:" in out
    assert "task-999" in out
    assert "Platform team" in out


def test_outcome_view_uses_saved_plan_context(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    cli.main(["outcome", "view"])
    method, path, _ = fake_client.calls[-1]
    assert method == "GET"
    assert path == "/api/plans/plan-123/outcome"


def test_outcome_save_accepts_plan_id_option(fake_client, capsys):
    cli.main(
        [
            "--json",
            "outcome",
            "save",
            "--plan-id",
            "plan-123",
            "--status",
            "partial",
            "--summary",
            "Pilot DAG completed",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/api/plans/plan-123/outcome"
    assert out["payload"]["status"] == "partial"


def test_task_edit_without_plan_context_fails(capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: {})
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: {})
    code = cli.main(["task", "edit", "task-456", "--status", "in_progress"])
    assert code == 1
    assert "Plan ID required" in capsys.readouterr().err


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


def test_config_prints_org_memberships(fake_client, monkeypatch, capsys):
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
    assert "Organizations:" in out
    assert "Acme" in out
    assert "Demo Inc" in out


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
    monkeypatch.setattr(
        "keshro_cli.auth.save_auth", lambda payload: saved.update(payload)
    )

    cli.main(["login", "ksh_pat_test"])
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
    monkeypatch.setattr(
        "keshro_cli.auth.save_auth", lambda payload: saved.update(payload)
    )

    cli.main(["--json", "login", "ksh_pat_test"])
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert saved["token"] == "ksh_pat_test"


def test_config_json_outputs_machine_readable_metadata(
    fake_client, monkeypatch, capsys
):
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
    assert len(out["orgs"]) == 2


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

    monkeypatch.setattr(
        "keshro_cli.auth.httpx.Client", lambda **kwargs: _BrowserClient()
    )
    monkeypatch.setattr(
        "keshro_cli.auth.save_auth", lambda payload: saved.update(payload)
    )
    monkeypatch.setattr("keshro_cli.auth.webbrowser.open", lambda url: True)
    monkeypatch.setattr("keshro_cli.auth.time.sleep", lambda _: None)

    cli.main(["--json", "login"])
    out_text = capsys.readouterr().out
    out = json.loads(out_text[out_text.index("{") :])
    assert out["status"] == "ok"
    assert saved["token"] == "jwt-123"


def test_http_404_errors_render_cleanly(monkeypatch, capsys):
    class _ErrorClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json=None):
            request = httpx.Request("POST", f"http://localhost:8000{path}")
            response = httpx.Response(
                404, request=request, json={"detail": "Template not found"}
            )
            raise httpx.HTTPStatusError("not found", request=request, response=response)

    monkeypatch.setattr(
        cli, "make_client", lambda api_url=None, token=None: _ErrorClient()
    )

    code = cli.main(
        [
            "plan",
            "create",
            "migration-123",
            "--from-template",
            "missing-template",
        ]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err.strip() == "Keshro API error (404): Template not found"


def test_plan_create_requires_migration_id(fake_client, capsys):
    code = cli.main(["plan", "create", "--title", "Manual plan"])
    captured = capsys.readouterr()
    assert code == 2
    assert (
        "Migration ID is required. Run `keshro plan create <migration-id>`."
        in captured.err
    )


def test_plan_create_allows_migration_seed_without_title(monkeypatch, capsys):
    seen = {}

    class _CreatePlanClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, path, json=None):
            seen["path"] = path
            seen["json"] = json
            return _FakeResponse(
                {
                    "id": "plan-123",
                    "title": "Heroku to AWS",
                    "status": "draft",
                    "source_type": "Heroku",
                    "target_type": "AWS",
                    "summary": "Execution plan derived from the migration analysis.",
                    "migration_id": "migration-123",
                    "plan_steps": [],
                    "external_links": [],
                    "updated_at": "2026-03-12T00:00:00",
                }
            )

    monkeypatch.setattr(
        cli, "make_client", lambda api_url=None, token=None: _CreatePlanClient()
    )
    monkeypatch.setattr(cli, "_set_default_plan_after_create", lambda created: None)

    cli.main(["plan", "create", "migration-123"])
    captured = capsys.readouterr()
    assert captured.err == ""
    assert seen["path"] == "/api/plans"
    assert seen["json"]["migration_id"] == "migration-123"
    assert seen["json"]["title"] is None
    assert seen["json"]["import_source"] == "analysis"


def test_request_errors_render_connection_help(monkeypatch, capsys):
    class _OfflineClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, params=None):
            request = httpx.Request("GET", f"http://localhost:8000{path}")
            raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(
        cli, "make_client", lambda api_url=None, token=None: _OfflineClient()
    )

    code = cli.main(["templates"])
    captured = capsys.readouterr()
    assert code == 1
    assert (
        "Could not reach Keshro at http://localhost:8000/api/plans/templates."
        in captured.err
    )
