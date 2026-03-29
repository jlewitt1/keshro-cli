import base64
import json
import re
import subprocess
from pathlib import Path

import httpx
import pytest

from keshro_cli import __version__, cli


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


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

    def get(self, path, params=None, headers=None, timeout=None):
        self.calls.append(("GET", path, params))
        if path == "/v1/plans/templates":
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
        if path == "/v1/orgs":
            return _FakeResponse(
                [
                    {"id": "org-123", "name": "Acme"},
                    {"id": "org-456", "name": "Demo Inc"},
                ]
            )
        if path == "/v1/migrations":
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
        if path == "/v1/migrations/migration-123":
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
        if path == "/v1/migrations/path-template/lookup":
            template_key = (params or {}).get("template_key")
            if template_key == "aws-batch-to-airflow":
                return _FakeResponse(
                    {
                        "template_key": "aws-batch-to-airflow",
                        "source": "AWS Batch",
                        "target": "Airflow",
                        "title": "AWS Batch to Airflow",
                        "description": "Move orchestration into Airflow.",
                        "fields": [
                            {
                                "id": "batch_workloads",
                                "label": "AWS Batch workloads",
                                "type": "textarea",
                                "required": True,
                            },
                            {
                                "id": "target_airflow_deployment",
                                "label": "Target Airflow deployment",
                                "type": "select",
                                "options": ["AWS MWAA", "Self-hosted"],
                                "required": False,
                            },
                        ],
                        "tips": [],
                        "required_outputs": [],
                        "status": "curated",
                        "is_auto_generated": False,
                    }
                )
            return _FakeResponse({"detail": "not found"})
        if path == "/v1/migrations/migration-123/plan":
            return _FakeResponse(
                {
                    "id": "plan-123",
                    "title": "AWS Batch to Airflow pilot",
                    "migration_id": "migration-123",
                    "task_feedback_events": [
                        {
                            "event_type": "task_updated",
                            "task_id": "task-456",
                            "task_title": "Translate pilot DAG",
                            "source": "cli",
                            "feedback_reason": "Pilot scope changed after DAG review",
                            "changed_fields": ["status", "notes"],
                            "created_at": "2026-03-15T16:00:00Z",
                        }
                    ],
                }
            )
        if path == "/v1/plans":
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
        if path == "/v1/plans/plan-123":
            return _FakeResponse(
                {
                    "id": "plan-123",
                    "title": "AWS Batch to Airflow pilot",
                    "status": "ready",
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
                        },
                        {
                            "id": "task-456",
                            "order": 2,
                            "title": "Translate pilot DAG",
                            "description": "Move the first batch workflow into Airflow",
                            "status": "todo",
                            "owner": None,
                            "notes": None,
                            "linear_issue_id": "KES-42",
                            "external_issue_provider": "linear",
                            "external_issue_id": "lin_123",
                            "external_issue_key": "KES-42",
                            "external_issue_url": "https://linear.app/keshro/issue/KES-42",
                            "blocked_reason": None,
                            "artifact_links": [],
                        },
                    ],
                }
            )
        return _FakeResponse({"ok": True})

    def post(self, path, json=None, timeout=None):
        self.calls.append(("POST", path, json))
        if "/push" in path:
            return _FakeResponse({"created": 3, "updated": 1})
        if "/sync-pull" in path:
            return _FakeResponse(
                {
                    "synced": 2,
                    "changes": [
                        {
                            "external_key": "KES-101",
                            "external_status": "completed",
                            "current_status": "in_progress",
                        },
                        {
                            "external_key": "KES-102",
                            "external_status": "in_progress",
                            "current_status": "todo",
                        },
                    ],
                }
            )
        if path == "/v1/migrations/clarifiers":
            return _FakeResponse({"questions": []})
        if path == "/v1/migrations":
            return _FakeResponse(
                {
                    "id": "migration-999",
                    "status": "analyzing",
                    "source_type": json.get("source_type"),
                    "target_type": json.get("target_type"),
                    "input_method": json.get("input_method"),
                }
            )
        if path == "/v1/plans/from-template":
            return _FakeResponse(
                {
                    "id": "plan-123",
                    "title": "AWS Batch to Airflow pilot",
                    "status": "ready",
                    "source_type": "AWS Batch",
                    "target_type": "Airflow",
                    "summary": "Pilot plan for the first DAG migration.",
                    "template_key": json.get("template_key"),
                    "migration_id": json.get("migration_id"),
                }
            )
        if path == "/v1/plans":
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


def test_create_migration_from_path_key_prompts_and_posts_payload(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "1")
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/local/bin/claude" if name == "claude" else None,
    )

    def _fake_run(cmd, capture_output, text, cwd, check):
        assert cmd[0] == "/usr/local/bin/claude"
        assert "--add-dir" in cmd
        assert "--permission-mode" in cmd
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="\n".join(
                [
                    "## Versions",
                    "- Source version: 1.0",
                    "- Target version: 2.9",
                    "",
                    "## AWS Batch to Airflow details",
                    "- AWS Batch workloads: scheduled ETL jobs",
                    "- Target Airflow deployment: AWS MWAA",
                    "",
                    "## Additional context",
                    "- Anything else that materially affects risk, effort, validation, cutover, rollback, or delivery: none",
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", _fake_run)
    monkeypatch.setattr("webbrowser.open", lambda url: None)
    cli.main(["create", "--path", "aws-batch-to-airflow"])

    out = capsys.readouterr().out
    assert "Prepared migration draft for AWS Batch -> Airflow." in out
    clarifier_call = next(
        call for call in fake_client.calls if call[1] == "/v1/migrations/clarifiers"
    )
    payload = clarifier_call[2]
    assert payload["input_method"] == "cli_agent"
    assert payload["custom_fields"]["batch_workloads"] == "scheduled ETL jobs"
    assert payload["custom_fields"]["__keshro_discovered_context"]
    assert "Prepared migration draft for AWS Batch -> Airflow." in out


def test_create_migration_from_path_key_applies_shared_clarifiers(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "1")
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/local/bin/claude" if name == "claude" else None,
    )

    call_count = {"count": 0}

    def _fake_run(cmd, capture_output, text, cwd, check):
        call_count["count"] += 1
        if call_count["count"] == 1:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="\n".join(
                    [
                        "## Versions",
                        "- Source version: 1.0",
                        "- Target version: 2.9",
                        "",
                        "## AWS Batch to Airflow details",
                        "- AWS Batch workloads: scheduled ETL jobs",
                    ]
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="- rollback_strategy: switch back to Batch scheduling immediately",
            stderr="",
        )

    def _post(path, json=None):
        fake_client.calls.append(("POST", path, json))
        if path == "/v1/migrations/clarifiers":
            return _FakeResponse(
                {
                    "questions": [
                        {
                            "id": "rollback_strategy",
                            "question": "What rollback strategy do you want if validation fails?",
                            "field_target": "rollback_strategy",
                            "answers": [],
                            "allow_custom": True,
                        }
                    ]
                }
            )
        return _FakeResponse({"path": path, "payload": json})

    monkeypatch.setattr("subprocess.run", _fake_run)
    monkeypatch.setattr(fake_client, "post", _post)

    opened_urls = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened_urls.append(url))

    cli.main(["create", "--path", "aws-batch-to-airflow"])

    assert len(opened_urls) == 1
    match = re.search(r"draft=([A-Za-z0-9_-]+)", opened_urls[0])
    assert match
    encoded = match.group(1)
    padded = encoded + "=" * ((4 - len(encoded) % 4) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    assert (
        decoded["custom_fields"]["rollback_strategy"]
        == "switch back to Batch scheduling immediately"
    )
    assert "Critical clarifications" in decoded["context"]
    assert call_count["count"] == 2


def test_create_migration_from_path_key_requires_claude_code(fake_client, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    exit_code = cli.main(["create", "--path", "aws-batch-to-airflow"])
    assert exit_code == 1


def _bypass_auth(monkeypatch):
    """Skip the auth check in continue so tests can focus on prompt output."""
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)


def test_continue_prints_prompt_with_task_context(fake_client, monkeypatch, capsys):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    _bypass_auth(monkeypatch)

    cli.main(["continue"])

    out = capsys.readouterr().out
    assert "Task: Review EventBridge schedules" in out
    assert "Task ID: review-schedules" in out
    assert "Execution reminders:" in out
    assert "keshro task start review-schedules -p plan-123" in out


def test_continue_prompt_omits_full_skill_boilerplate_in_non_tty_mode(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    _bypass_auth(monkeypatch)

    cli.main(["continue"])

    out = capsys.readouterr().out
    assert "Do NOT use Keshro MCP tools" not in out
    assert "The current task and plan context are provided below" not in out


def test_continue_prompt_mentions_status_tracking_and_blocking_rule(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    _bypass_auth(monkeypatch)

    cli.main(["continue"])

    out = capsys.readouterr().out
    assert "keshro status -p plan-123 --watch" in out
    assert "Only mark the task blocked if work cannot continue" in out


def test_continue_prompt_surfaces_plan_risks_unknowns_and_ui_link(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setattr(
        "keshro_cli.cli.load_auth",
        lambda: {**_auth_with_plan(), "api_url": "http://localhost:8000"},
    )
    monkeypatch.setattr(
        "keshro_cli.client.load_auth",
        lambda: {**_auth_with_plan(), "api_url": "http://localhost:8000"},
    )
    _bypass_auth(monkeypatch)

    original_get = fake_client.get

    def _get(path, params=None, headers=None, timeout=None):
        response = original_get(path, params=params, headers=headers, timeout=timeout)
        if path != "/v1/plans/plan-123":
            return response
        plan = response.json()
        plan["enrichment_sources"] = [
            {
                "name": "Web research",
                "detail": "Best practices for AWS Batch -> https://docs.aws.amazon.com/batch/latest/userguide/best-practices.html",
            }
        ]
        plan["decisions"] = {
            "risks": [
                {
                    "title": "Rollback path is unclear",
                    "description": "Current cutover steps do not define a clean rollback.",
                }
            ],
            "unknowns": [
                {
                    "question": "Which environments need phased rollout first?",
                }
            ],
        }
        return _FakeResponse(plan)

    fake_client.get = _get
    cli.main(["continue"])
    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "Top plan risks:" in out
    assert "Open questions:" in out
    assert (
        "Review full risks/questions in UI: http://localhost:3000/plans/plan-123" in out
    )
    assert "Source highlights:" not in out


def test_continue_in_agent_mode_resumes_in_progress_task_before_next_todo(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    _bypass_auth(monkeypatch)

    original_get = fake_client.get

    def _get(path, params=None):
        response = original_get(path, params=params)
        if path != "/v1/plans/plan-123":
            return response
        plan = response.json()
        plan["plan_steps"] = [
            {
                **plan["plan_steps"][0],
                "id": "task-1",
                "title": "Set up MWAA environment with Terraform",
                "status": "in_progress",
                "order": 1,
            },
            {
                **plan["plan_steps"][1],
                "id": "task-2",
                "title": "Create and validate DAG files",
                "status": "in_progress",
                "order": 2,
            },
            {
                "id": "task-3",
                "order": 3,
                "title": "Test DAGs locally with MWAA Docker",
                "description": "Validate DAG parsing and dependency compatibility",
                "status": "todo",
            },
        ]
        return _FakeResponse(plan)

    monkeypatch.setattr(fake_client, "get", _get)

    cli.main(["continue"])

    out = capsys.readouterr().out
    assert "Task: Set up MWAA environment with Terraform" in out
    assert "Task: Test DAGs locally with MWAA Docker" not in out


def test_continue_prompt_includes_error_guidance(fake_client, monkeypatch, capsys):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    _bypass_auth(monkeypatch)

    cli.main(["continue"])

    out = capsys.readouterr().out
    assert "If a keshro command fails" in out


def test_continue_prompt_does_not_tell_claude_to_refetch(
    fake_client, monkeypatch, capsys
):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    _bypass_auth(monkeypatch)

    cli.main(["continue"])

    out = capsys.readouterr().out
    assert "Do not re-fetch them" in out
    assert "Start by grounding" not in out


def test_continue_exits_when_not_authenticated(fake_client, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: {})
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: {})

    exit_code = cli.main(["continue", "-p", "plan-123"])
    assert exit_code == 1


def test_continue_exits_when_token_expired(fake_client, monkeypatch):
    monkeypatch.setattr(
        "keshro_cli.cli.load_auth",
        lambda: {"token": "expired-token", "default_plan_id": "plan-123"},
    )
    monkeypatch.setattr(
        "keshro_cli.client.load_auth", lambda: {"token": "expired-token"}
    )

    def _fake_make_client(*args, **kwargs):
        class _ExpiredClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, path, **kw):
                resp = httpx.Response(401, request=httpx.Request("GET", path))
                raise httpx.HTTPStatusError(
                    "Unauthorized", request=resp.request, response=resp
                )

        return _ExpiredClient()

    monkeypatch.setattr("keshro_cli.cli.make_client", _fake_make_client)

    exit_code = cli.main(["continue", "-p", "plan-123"])
    assert exit_code == 1


def test_setup_claude_creates_slash_command(monkeypatch, tmp_path, capsys):
    commands_dir = tmp_path / "commands"
    monkeypatch.setattr("keshro_cli.cli.CLAUDE_COMMANDS_DIR", commands_dir)

    cli.main(["setup-claude"])

    out = capsys.readouterr().out
    assert "Installed Claude Code slash command" in out
    target = commands_dir / "keshro.md"
    assert target.exists()
    content = target.read_text()
    assert "keshro continue" in content
    assert "Do NOT use Keshro MCP tools" in content
    assert "keshro create --context-file /tmp/keshro-context.txt" in content


def test_setup_claude_overwrites_existing(monkeypatch, tmp_path, capsys):
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir(parents=True)
    (commands_dir / "keshro.md").write_text("old content")
    monkeypatch.setattr("keshro_cli.cli.CLAUDE_COMMANDS_DIR", commands_dir)

    cli.main(["setup-claude"])

    content = (commands_dir / "keshro.md").read_text()
    assert "old content" not in content
    assert "keshro continue" in content


def test_create_reads_context_from_file(fake_client, monkeypatch, tmp_path, capsys):
    _auth = {**_auth_with_plan(), "token": "ksh_pat_test"}
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: _auth)
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: _auth)
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr("keshro_cli.cli.update_auth", lambda payload: payload)
    monkeypatch.setattr("keshro_cli.cli._inside_coding_agent", lambda: False)
    monkeypatch.setattr("keshro_cli.cli._collect_generic_discovery", lambda _: None)

    context_path = tmp_path / "context.txt"
    context_path.write_text("Scale the Helm chart safely.")

    original_post = fake_client.post

    def _post(path, json=None, timeout=None):
        if path == "/v1/plans/describe/preview":
            return _FakeResponse({"questions": [], "enrichment_context": ""})
        if path == "/v1/plans/generate":
            assert json["description"] == "Scale the Helm chart safely."
            return _FakeResponse(
                {
                    "id": "plan-ctx-1",
                    "title": "Scalability plan",
                    "status": "draft",
                    "plan_steps": [],
                }
            )
        return original_post(path, json=json, timeout=timeout)

    fake_client.post = _post

    code = cli.main(["create", "--context-file", str(context_path)])

    assert code == 0
    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "Scalability plan" in out


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
    assert ("DELETE", "/v1/migrations/migration-123", None) in fake_client.calls


def test_migration_list_json_outputs_machine_readable_rows(fake_client, capsys):
    cli.main(["--json", "migration", "list"])
    out = json.loads(capsys.readouterr().out)
    assert out[0]["id"] == "migration-123"
    assert out[0]["source_type"] == "AWS Batch"


def test_plan_list_is_concise_by_default(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: {})
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: {})
    monkeypatch.setattr(cli, "_format_plan_timestamp", lambda value: "Today 10:30")
    cli.main(["plan", "list"])
    out = capsys.readouterr().out
    assert "ID" in out
    assert "TITLE" in out
    assert "STATUS" in out
    assert "UPDATED" in out
    assert "plan-123" in out
    assert "AWS Batch to Airflow pilot" in out
    assert "AWS Batch -> Airflow" in out
    assert "Today 10:30" in out
    assert "2026-03-11T15:30:00Z" not in out
    assert "Pilot plan for the first DAG migration." not in out
    assert "for org" not in out


def test_plan_list_verbose_includes_summary_and_timestamp(
    fake_client, capsys, monkeypatch
):
    monkeypatch.setattr(
        cli, "_format_verbose_timestamp", lambda value: "2026-03-11 10:30 PDT"
    )
    cli.main(["plan", "list", "--verbose"])
    out = capsys.readouterr().out
    assert "Pilot plan for the first DAG migration." in out
    assert "Updated:" in out
    assert "2026-03-11 10:30 PDT" in out
    assert "2026-03-11T15:30:00Z" not in out


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
            if path == "/v1/plans":
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
    monkeypatch.setattr("typer.confirm", lambda *args, **kwargs: True)
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
    assert out["path"] == "/v1/plans/plan-123"


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
    monkeypatch.setattr("keshro_cli.cli._ensure_authenticated", lambda: None)
    monkeypatch.setattr(
        "keshro_cli.cli._link_current_repo_to_plan", lambda *args, **kwargs: True
    )
    code = cli.main(["config", "set", "-p", "plan-123"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Saved default context: personal" in out
    assert "Saved default plan: AWS Batch to Airflow pilot" in out
    assert "Linked the current repo to this plan in Keshro." in out


def test_require_plan_context_can_resolve_repo_link(monkeypatch):
    monkeypatch.setattr(
        "keshro_cli.cli.load_auth",
        lambda: {
            "api_url": "http://localhost:8000",
            "token": "jwt-123",
        },
    )
    monkeypatch.setattr("keshro_cli.cli.update_auth", lambda payload: payload)

    class _ResolveClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, params=None, headers=None, timeout=None):
            if path == "/v1/plans/repo-link/resolve":
                assert params["repo_root"] == "/tmp/demo"
                return _FakeResponse({"plan_id": "plan-123"})
            if path == "/v1/plans/plan-123":
                return _FakeResponse(
                    {"id": "plan-123", "title": "AWS Batch to Airflow pilot"}
                )
            raise AssertionError(path)

    def _fake_run(cmd, cwd=None, capture_output=None, text=None, check=None):
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="/tmp/demo\n", stderr="")
        if cmd == ["git", "config", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="git@github.com:acme/demo.git\n", stderr=""
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(
        "keshro_cli.cli.make_client", lambda api_url=None, token=None: _ResolveClient()
    )
    monkeypatch.setattr("keshro_cli.cli.subprocess.run", _fake_run)

    assert cli._require_plan_context(None) == "plan-123"


def test_current_plan_id_prefers_repo_resolution_before_cached_default(monkeypatch):
    monkeypatch.setattr(
        "keshro_cli.cli.load_auth",
        lambda: {
            "default_plan_id": "plan-cached",
            "default_plan_title": "Cached plan",
        },
    )
    saved = {}

    monkeypatch.setattr(
        "keshro_cli.cli.update_auth", lambda payload: saved.update(payload) or payload
    )
    monkeypatch.setattr(
        "keshro_cli.cli._resolve_repo_linked_plan",
        lambda *args, **kwargs: ("plan-linked", "Repo linked plan"),
    )

    assert cli._current_plan_id(None) == "plan-linked"
    assert saved["default_plan_id"] == "plan-linked"
    assert saved["default_plan_title"] == "Repo linked plan"


def test_get_plan_or_exit_clears_stale_cached_default_on_404(monkeypatch):
    saved = {}
    monkeypatch.setattr(
        "keshro_cli.cli.load_auth",
        lambda: {
            "default_plan_id": "plan-stale",
            "default_plan_title": "Stale plan",
        },
    )
    monkeypatch.setattr(
        "keshro_cli.cli._resolve_repo_linked_plan",
        lambda *args, **kwargs: (None, None),
    )
    monkeypatch.setattr(
        "keshro_cli.cli.update_auth", lambda payload: saved.update(payload) or payload
    )

    class _404Response:
        status_code = 404

        def __init__(self):
            self.request = httpx.Request(
                "GET", "http://localhost:8000/v1/plans/plan-stale"
            )

        def json(self):
            return {"detail": "Plan not found"}

    class _MissingPlanClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, params=None, headers=None, timeout=None):
            response = _404Response()
            raise httpx.HTTPStatusError(
                "Plan not found", request=response.request, response=response
            )

    monkeypatch.setattr(
        "keshro_cli.cli.make_client",
        lambda api_url=None, token=None: _MissingPlanClient(),
    )

    with pytest.raises(httpx.HTTPStatusError):
        cli._get_plan_or_exit(None)

    assert saved["default_plan_id"] is None
    assert saved["default_plan_title"] is None


def test_install_codex_integration_replaces_existing_managed_block(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("keshro_cli.cli.CODEX_HOME_DIR", tmp_path)
    target = tmp_path / "AGENTS.md"
    old_block = (
        "<!-- keshro-agent-instructions -->\n"
        "# Keshro Integration\n\n"
        "OLD CONTENT\n"
        "<!-- keshro-agent-instructions -->\n"
    )
    target.write_text("Existing intro\n\n" + old_block + "\nExisting footer\n")

    cli._install_codex_integration()

    content = target.read_text()
    assert "OLD CONTENT" not in content
    assert "Existing intro" in content
    assert "Existing footer" in content
    assert content.count("<!-- keshro-agent-instructions -->") == 2


def test_setup_all_reports_already_present_integrations(monkeypatch, capsys):
    monkeypatch.setattr(
        "keshro_cli.cli._install_agent_integrations",
        lambda silent=True: (
            [],
            ["Claude Code: /tmp/keshro.md", "Codex: /tmp/codex.md"],
        ),
    )

    cli.main(["setup"])
    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "All agent integrations already installed." in out


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
    assert out["path"] == "/v1/plans/plan-123/tasks/task-456"
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
    assert out["path"] == "/v1/plans/plan-123/tasks/task-456"
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
    assert out["path"] == "/v1/plans/plan-123/tasks/task-456"
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
    assert out["path"] == "/v1/plans/plan-123/tasks/task-456"


def test_plan_next_returns_first_actionable_task(fake_client, capsys):
    cli.main(["--json", "plan", "next", "plan-123"])
    out = json.loads(capsys.readouterr().out)
    assert out["plan_id"] == "plan-123"
    assert out["task"]["id"] == "review-schedules"


def test_task_start_marks_task_in_progress(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "start",
            "plan-123",
            "task-456",
            "--notes",
            "Starting the pilot implementation",
            "--reason",
            "Top priority after plan review",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/v1/plans/plan-123/tasks/task-456"
    assert out["payload"]["status"] == "in_progress"
    assert "Starting the pilot implementation" in out["payload"]["notes"]
    assert "[20" in out["payload"]["notes"]
    assert out["payload"]["feedback_reason"] == "Top priority after plan review"


def test_task_next_returns_next_actionable_task(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "next",
            "--plan-id",
            "plan-123",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["plan_id"] == "plan-123"
    assert out["task"]["id"] == "review-schedules"


def test_task_done_marks_task_completed(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "done",
            "plan-123",
            "task-456",
            "--notes",
            "Pilot DAG merged and validated",
            "--link",
            "https://github.com/acme/migrations/pull/21",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/v1/plans/plan-123/tasks/task-456"
    assert out["payload"]["status"] == "completed"
    assert "Pilot DAG merged and validated" in out["payload"]["notes"]
    assert "[20" in out["payload"]["notes"]
    assert out["payload"]["artifact_links"] == [
        "https://github.com/acme/migrations/pull/21"
    ]
    assert out["payload"]["blocked_reason"] == ""


def test_task_done_requires_completion_evidence_when_acceptance_criteria_exist(
    fake_client, monkeypatch, capsys
):
    original_get = fake_client.get

    def _get(path, params=None):
        response = original_get(path, params=params)
        if path != "/v1/plans/plan-123":
            return response
        plan = response.json()
        enriched_steps = []
        for step in plan["plan_steps"]:
            if step["id"] == "task-456":
                enriched_steps.append(
                    {
                        **step,
                        "acceptance_criteria": [
                            "DAG syntax validates without errors",
                            "Retry logic implemented",
                        ],
                        "discovery_commands": [
                            "airflow dags check dags/",
                            "python -m py_compile dags/*.py",
                        ],
                    }
                )
            else:
                enriched_steps.append(step)
        return _FakeResponse({**plan, "plan_steps": enriched_steps})

    monkeypatch.setattr(fake_client, "get", _get)

    code = cli.main(
        [
            "--json",
            "task",
            "done",
            "plan-123",
            "task-456",
            "--notes",
            "Pilot DAG merged and validated",
        ]
    )
    assert code != 0
    err = ANSI_RE.sub("", capsys.readouterr().err)
    assert "Acceptance criteria met:" in err
    assert "Verification:" in err


def test_task_done_accepts_completion_evidence_when_acceptance_criteria_exist(
    fake_client, monkeypatch, capsys
):
    original_get = fake_client.get

    def _get(path, params=None):
        response = original_get(path, params=params)
        if path != "/v1/plans/plan-123":
            return response
        plan = response.json()
        enriched_steps = []
        for step in plan["plan_steps"]:
            if step["id"] == "task-456":
                enriched_steps.append(
                    {
                        **step,
                        "acceptance_criteria": [
                            "DAG syntax validates without errors",
                            "Retry logic implemented",
                        ],
                        "discovery_commands": [
                            "airflow dags check dags/",
                            "python -m py_compile dags/*.py",
                        ],
                    }
                )
            else:
                enriched_steps.append(step)
        return _FakeResponse({**plan, "plan_steps": enriched_steps})

    monkeypatch.setattr(fake_client, "get", _get)

    cli.main(
        [
            "--json",
            "task",
            "done",
            "plan-123",
            "task-456",
            "--notes",
            "Files created: dags/daily_sales_pipeline.py | Acceptance criteria met: DAG syntax validates without errors; retry logic implemented | Verification: airflow dags check dags/ passed; python -m py_compile dags/*.py passed",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["payload"]["status"] == "completed"
    assert "Acceptance criteria met:" in out["payload"]["notes"]
    assert "Verification:" in out["payload"]["notes"]


def test_task_block_marks_task_blocked(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "block",
            "plan-123",
            "task-456",
            "--reason",
            "Waiting on Terraform IAM role changes",
            "--feedback-reason",
            "Access blocker discovered during pilot setup",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/v1/plans/plan-123/tasks/task-456"
    assert out["payload"]["status"] == "blocked"
    assert out["payload"]["blocked_reason"] == "Waiting on Terraform IAM role changes"
    assert (
        out["payload"]["feedback_reason"]
        == "Access blocker discovered during pilot setup"
    )


def test_task_unblock_clears_blocked_reason(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "unblock",
            "plan-123",
            "task-456",
            "--notes",
            "Terraform role applied; resuming pilot",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/v1/plans/plan-123/tasks/task-456"
    assert out["payload"]["status"] == "in_progress"
    assert out["payload"]["blocked_reason"] == ""
    assert "Terraform role applied; resuming pilot" in out["payload"]["notes"]
    assert "[20" in out["payload"]["notes"]


def test_task_done_appends_to_existing_notes(fake_client, monkeypatch, capsys):
    original_get_plan = cli._get_plan_or_exit

    def _plan_with_existing_notes(plan_id: str):
        plan = original_get_plan(plan_id)
        plan["plan_steps"][1][
            "notes"
        ] = "[2026-03-15 16:00 UTC] Existing execution note"
        return plan

    monkeypatch.setattr(cli, "_get_plan_or_exit", _plan_with_existing_notes)

    cli.main(
        [
            "--json",
            "task",
            "done",
            "plan-123",
            "task-456",
            "--notes",
            "Pilot DAG merged and validated",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert "Existing execution note" in out["payload"]["notes"]
    assert "Pilot DAG merged and validated" in out["payload"]["notes"]


def test_task_start_human_output_is_compact(fake_client, capsys):
    cli.main(
        [
            "task",
            "start",
            "plan-123",
            "task-456",
            "--notes",
            "Starting the pilot implementation",
        ]
    )
    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert "Updated task" in out
    assert "[in_progress]." in out
    assert "Notes updated" in out
    assert "Plan ID:" not in out
    assert "Task ID:" not in out


def test_task_note_appends_timestamped_note(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "note",
            "plan-123",
            "task-456",
            "--note",
            "Airflow will orchestrate Batch during the pilot",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/v1/plans/plan-123/tasks/task-456"
    assert "Airflow will orchestrate Batch during the pilot" in out["payload"]["notes"]
    assert "[20" in out["payload"]["notes"]


def test_task_artifact_appends_link_without_overwriting(fake_client, capsys):
    cli.main(
        [
            "--json",
            "task",
            "artifact",
            "plan-123",
            "task-456",
            "--link",
            "https://github.com/acme/migrations/pull/99",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/v1/plans/plan-123/tasks/task-456"
    assert out["payload"]["artifact_links"] == [
        "https://github.com/acme/migrations/pull/99"
    ]


def test_plan_replan_notes_appends_summary(fake_client, capsys):
    cli.main(
        [
            "--json",
            "plan",
            "replan-notes",
            "Need to keep Batch as the runtime while Airflow takes over orchestration.",
            "--plan-id",
            "plan-123",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/v1/plans/plan-123"
    assert "Pilot plan for the first DAG migration." in out["payload"]["summary"]
    assert (
        "Need to keep Batch as the runtime while Airflow takes over orchestration."
        in out["payload"]["summary"]
    )


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


def test_task_delete_prompts_before_removing_linked_issue(
    fake_client, capsys, monkeypatch
):
    seen = {}

    def fake_confirm(message, abort=False):
        seen["message"] = message
        seen["abort"] = abort
        return True

    monkeypatch.setattr("typer.confirm", fake_confirm)
    cli.main(["task", "delete", "plan-123", "task-456"])
    out = capsys.readouterr().out
    assert "Deleted task task-456 from plan plan-123." in out
    assert "linked Linear issue KES-42" in seen["message"]
    assert seen["abort"] is True


def test_task_delete_yes_skips_confirmation(fake_client, capsys, monkeypatch):
    def fail_confirm(*args, **kwargs):
        raise AssertionError("confirm should not be called")

    monkeypatch.setattr("typer.confirm", fail_confirm)
    cli.main(["task", "delete", "plan-123", "task-456", "--yes"])
    out = capsys.readouterr().out
    assert "Deleted task task-456 from plan plan-123." in out


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


def test_task_next_uses_saved_plan_context(fake_client, capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", _auth_with_plan)
    monkeypatch.setattr("keshro_cli.client.load_auth", _auth_with_plan)
    cli.main(["task", "next"])
    method, path, _ = fake_client.calls[-1]
    assert method == "GET"
    assert path == "/v1/plans/plan-123"


def test_plan_replan_notes_accepts_plan_id_option(fake_client, capsys):
    cli.main(
        [
            "--json",
            "plan",
            "replan-notes",
            "--plan-id",
            "plan-123",
            "Pilot DAG completed",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == "/v1/plans/plan-123"
    assert "Pilot DAG completed" in out["payload"]["summary"]


def test_task_edit_without_plan_context_fails(capsys, monkeypatch):
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: {})
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: {})
    code = cli.main(["task", "edit", "task-456", "--status", "in_progress"])
    assert code == 1
    assert "Plan ID required" in capsys.readouterr().err


def test_migration_history_uses_plan_audit_trail(fake_client, capsys):
    cli.main(["migration", "history", "migration-123"])
    out = capsys.readouterr().out
    assert "Audit Trail:" in out
    assert "Translate pilot DAG" in out
    assert "Pilot scope changed after DAG review" in out


def test_migration_history_json(fake_client, capsys):
    cli.main(["--json", "migration", "history", "migration-123"])
    out = json.loads(capsys.readouterr().out)
    assert out["migration_id"] == "migration-123"
    assert out["plan_id"] == "plan-123"
    assert out["task_feedback_events"][0]["task_id"] == "task-456"


def test_config_prints_saved_auth_metadata(monkeypatch, capsys):
    class _ConfigClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, params=None):
            if path == "/v1/auth/me":
                return _FakeResponse({"email": "cli@example.com", "id": "user-1"})
            if path == "/v1/orgs":
                return _FakeResponse([])
            raise AssertionError(path)

    monkeypatch.setattr(
        "keshro_cli.cli.load_auth",
        lambda: {
            "api_url": "https://app.keshro.test",
            "token": "jwt-123",
            "user": {"email": "cli@example.com", "name": "CLI User"},
        },
    )
    monkeypatch.setattr(
        "keshro_cli.cli.make_client", lambda api_url=None, token=None: _ConfigClient()
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
            assert path == "/v1/auth/me"
            assert headers == {"Authorization": "Bearer ksh_pat_test"}
            return _FakeResponse({"email": "cli@example.com", "id": "user-1"})

    monkeypatch.setattr("keshro_cli.auth.httpx.Client", lambda **kwargs: _AuthClient())
    monkeypatch.setattr(
        "keshro_cli.auth.save_auth", lambda payload: saved.update(payload)
    )
    monkeypatch.setattr(
        "keshro_cli.cli._install_claude_integration",
        lambda: Path("/tmp/keshro.md"),
    )
    monkeypatch.setattr(
        "keshro_cli.cli._install_codex_integration",
        lambda: Path("/tmp/codex-AGENTS.md"),
    )

    cli.main(["login", "ksh_pat_test"])
    out = capsys.readouterr().out
    assert "Successfully logged in to Keshro as cli@example.com." in out
    assert "Claude Code: /tmp/keshro.md" in out
    assert "Codex: /tmp/codex-AGENTS.md" in out
    assert (
        "Run `keshro setup` inside a repo if you also want Cursor repo instructions there."
        in out
    )
    assert saved["token"] == "ksh_pat_test"


def test_auth_login_with_token_validates_with_auth_me(monkeypatch, capsys):
    saved = {}

    class _AuthClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, headers=None):
            assert path == "/v1/auth/me"
            assert headers == {"Authorization": "Bearer ksh_pat_test"}
            return _FakeResponse({"email": "cli@example.com", "id": "user-1"})

    monkeypatch.setattr("keshro_cli.auth.httpx.Client", lambda **kwargs: _AuthClient())
    monkeypatch.setattr(
        "keshro_cli.auth.save_auth", lambda payload: saved.update(payload)
    )
    monkeypatch.setattr(
        "keshro_cli.cli._install_claude_integration",
        lambda: Path("/tmp/keshro.md"),
    )
    monkeypatch.setattr(
        "keshro_cli.cli._install_codex_integration",
        lambda: Path("/tmp/codex-AGENTS.md"),
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


def test_config_marks_stale_token_as_not_authenticated(monkeypatch, capsys):
    class _UnauthorizedResponse:
        def raise_for_status(self):
            request = httpx.Request("GET", "https://app.keshro.test/api/auth/me")
            response = httpx.Response(
                401, request=request, json={"detail": "Authentication required"}
            )
            raise httpx.HTTPStatusError(
                "Client error '401 Unauthorized' for url",
                request=request,
                response=response,
            )

        def json(self):
            return {"detail": "Authentication required"}

    class _StaleClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, params=None):
            assert path == "/v1/auth/me"
            return _UnauthorizedResponse()

    monkeypatch.setattr(
        "keshro_cli.cli.load_auth",
        lambda: {
            "api_url": "https://app.keshro.test",
            "token": "jwt-stale",
            "user": {"email": "cli@example.com", "name": "CLI User"},
        },
    )
    monkeypatch.setattr(
        "keshro_cli.cli.make_client", lambda api_url=None, token=None: _StaleClient()
    )

    cli.main(["config"])
    out = capsys.readouterr().out
    assert "Authenticated:" in out
    assert "no" in out


def test_auth_login_requires_token_argument(monkeypatch, capsys):
    monkeypatch.setattr("keshro_cli.auth.load_auth", lambda: {})
    code = cli.main(["login"])
    captured = capsys.readouterr()
    assert code == 1
    cleaned = ANSI_RE.sub("", captured.err)
    assert "No saved Keshro session found." in cleaned
    assert "Usage: keshro login <api-token>" in cleaned
    assert "Account -> API" in cleaned


def test_auth_login_without_token_reuses_saved_session(monkeypatch, capsys):
    monkeypatch.setattr(
        "keshro_cli.auth.load_auth",
        lambda: {
            "api_url": "https://app.keshro.test",
            "token": "jwt-123",
            "user": {"email": "cli@example.com"},
        },
    )

    class _AuthClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, headers=None):
            assert path == "/v1/auth/me"
            assert headers == {"Authorization": "Bearer jwt-123"}
            return _FakeResponse({"email": "cli@example.com", "id": "user-1"})

    monkeypatch.setattr("keshro_cli.auth.httpx.Client", lambda **kwargs: _AuthClient())

    cli.main(["login"])
    out = capsys.readouterr().out.strip()
    assert out == "Already logged in to Keshro as cli@example.com."


def test_auth_login_without_token_reports_expired_saved_session(monkeypatch, capsys):
    monkeypatch.setattr(
        "keshro_cli.auth.load_auth",
        lambda: {
            "api_url": "https://app.keshro.test",
            "token": "jwt-stale",
            "user": {"email": "cli@example.com"},
        },
    )

    class _UnauthorizedClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, headers=None):
            request = httpx.Request("GET", "https://app.keshro.test/api/auth/me")
            response = httpx.Response(
                401, request=request, json={"detail": "Authentication required"}
            )
            raise httpx.HTTPStatusError(
                "Client error '401 Unauthorized' for url",
                request=request,
                response=response,
            )

    monkeypatch.setattr(
        "keshro_cli.auth.httpx.Client", lambda **kwargs: _UnauthorizedClient()
    )

    code = cli.main(["login"])
    captured = capsys.readouterr()
    assert code == 1
    cleaned = ANSI_RE.sub("", captured.err)
    assert "Your saved Keshro session has expired." in cleaned
    assert "keshro login <api-token>" in cleaned
    assert "Account -> API" in cleaned


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
    assert (
        ANSI_RE.sub("", captured.err).strip()
        == "Keshro API error (404): Template not found"
    )


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
    assert seen["path"] == "/v1/plans"
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
        "Could not reach Keshro at http://localhost:8000/v1/plans/templates."
        in captured.err
    )


# ---------------------------------------------------------------------------
# plan push / sync-pull / generate enrichment / status cost tests
# ---------------------------------------------------------------------------


def test_plan_push(fake_client, capsys, monkeypatch):
    auth = {**_auth_with_plan(), "token": "ksh_pat_test"}
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: auth)
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: auth)
    code = cli.main(["plan", "push", "-p", "plan-123", "--provider", "linear"])
    captured = capsys.readouterr()
    assert code == 0
    assert "3 issue(s) created" in captured.out
    assert "1 updated" in captured.out


def test_plan_sync_pull(fake_client, capsys, monkeypatch):
    _auth = {**_auth_with_plan(), "token": "ksh_pat_test"}
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: _auth)
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: _auth)
    code = cli.main(["plan", "sync-pull", "-p", "plan-123"])
    captured = capsys.readouterr()
    assert code == 0
    assert "2 task(s) updated" in captured.out
    assert "KES-101" in captured.out


def test_plan_generate_shows_enrichment(fake_client, capsys, monkeypatch):
    _auth = {**_auth_with_plan(), "token": "ksh_pat_test"}
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: _auth)
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: _auth)
    original_post = fake_client.post

    def _post_gen(path, json=None, timeout=None):
        if "/generate" in path:
            return _FakeResponse(
                {
                    "id": "plan-gen-1",
                    "title": "Auth Refactor",
                    "status": "draft",
                    "plan_steps": [
                        {
                            "order": 1,
                            "title": "Update auth",
                            "depends_on": [],
                            "risk_level": "high",
                        },
                    ],
                    "enrichment_sources": [
                        {"name": "Greptile", "description": "Codebase analysis"},
                    ],
                    "decisions": {
                        "confidence_score": 82,
                        "risks": [{"severity": "high", "description": "Auth"}],
                    },
                }
            )
        return original_post(path, json=json, timeout=timeout)

    fake_client.post = _post_gen
    code = cli.main(["plan", "generate", "Refactor auth module"])
    captured = capsys.readouterr()
    out = ANSI_RE.sub("", captured.out)
    assert code == 0
    assert "Greptile" in out
    assert "confidence: 82%" in out
    assert "[high risk]" in out


def test_status_shows_cost_summary(fake_client, capsys, monkeypatch):
    _auth = {**_auth_with_plan(), "token": "ksh_pat_test"}
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: _auth)
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: _auth)
    original_get = fake_client.get

    def _get_cost(path, params=None, timeout=None):
        if path == "/v1/plans/plan-123":
            return _FakeResponse(
                {
                    "id": "plan-123",
                    "title": "Auth Refactor",
                    "status": "in_progress",
                    "plan_steps": [
                        {
                            "id": "t1",
                            "order": 1,
                            "title": "Task 1",
                            "status": "completed",
                        },
                    ],
                    "task_feedback_events": [],
                    "agent_cost": {
                        "total_cost_usd": 4.50,
                        "total_tokens": 250000,
                        "total_duration_seconds": 720,
                        "tasks_tracked": 2,
                        "by_model": {
                            "claude-sonnet-4": {
                                "tasks": 2,
                                "cost_usd": 4.50,
                                "tokens": 250000,
                                "duration_seconds": 720,
                            },
                        },
                    },
                    "enrichment_sources": [{"name": "Greptile", "description": "x"}],
                }
            )
        return original_get(path, params=params)

    fake_client.get = _get_cost
    code = cli.main(["status", "-p", "plan-123"])
    captured = capsys.readouterr()
    out = ANSI_RE.sub("", captured.out)
    assert code == 0
    assert "$4.50" in out
    assert "250,000 tokens" in out
    assert "claude-sonnet-4" in out
    assert "Greptile" in out


def test_status_surfaces_enrichment_analysis_and_ui_review_link(
    fake_client, capsys, monkeypatch
):
    _auth = {
        **_auth_with_plan(),
        "token": "ksh_pat_test",
        "api_url": "http://localhost:8000",
    }
    monkeypatch.setattr("keshro_cli.cli.load_auth", lambda: _auth)
    monkeypatch.setattr("keshro_cli.client.load_auth", lambda: _auth)
    original_get = fake_client.get

    def _get_status(path, params=None, headers=None, timeout=None):
        if path == "/v1/plans/plan-123":
            return _FakeResponse(
                {
                    "id": "plan-123",
                    "title": "Kubetorch Helm Chart Horizontal Scaling Enhancement",
                    "status": "draft",
                    "source_type": "",
                    "target_type": "",
                    "updated_at": "2026-03-11T15:30:00Z",
                    "plan_steps": [
                        {
                            "id": "t1",
                            "order": 1,
                            "title": "Audit current chart structure",
                            "status": "todo",
                        }
                    ],
                    "task_feedback_events": [],
                    "enrichment_sources": [
                        {
                            "name": "Web research",
                            "detail": "Best practices for Helm charts -> https://example.com/helm",
                        }
                    ],
                    "decisions": {
                        "confidence_score": 74,
                        "risks": [
                            {"title": "Autoscaling values may break existing installs"}
                        ],
                        "unknowns": [
                            {"question": "Which clusters need backwards compatibility?"}
                        ],
                    },
                }
            )
        return original_get(path, params=params, headers=headers, timeout=timeout)

    fake_client.get = _get_status
    code = cli.main(["status", "-p", "plan-123"])
    out = ANSI_RE.sub("", capsys.readouterr().out)
    assert code == 0
    assert "Enriched by: Web research" in out
    assert "Analysis: confidence: 74% · 1 risk · 1 open question" in out
    assert "Top risks:" in out
    assert "Open questions:" in out
    assert "Review in UI: http://localhost:3000/plans/plan-123" in out
    assert "Best practices for Helm charts" not in out
    assert "├──" not in out
