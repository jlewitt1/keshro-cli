"""Tests for the Textual TUI dashboard (keshro status --tui)."""

from __future__ import annotations

import pytest

from keshro_cli.tui import (
    ActiveAgents,
    KeshroStatusApp,
    PlanInsights,
    PlanOverview,
    RecentEvents,
    TaskGraph,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_PLAN = {
    "title": "AWS Batch to Airflow pilot",
    "status": "in_progress",
    "plan_steps": [
        {
            "id": "task-1",
            "title": "Capture current context",
            "status": "completed",
            "order": 1,
            "depends_on": [],
        },
        {
            "id": "task-2",
            "title": "Write DAG skeleton",
            "status": "in_progress",
            "order": 2,
            "depends_on": ["task-1"],
            "owner": "agent-1",
        },
        {
            "id": "task-3",
            "title": "Set up Airflow env",
            "status": "in_progress",
            "order": 3,
            "depends_on": ["task-1"],
        },
        {
            "id": "task-4",
            "title": "Integration tests",
            "status": "blocked",
            "order": 4,
            "depends_on": ["task-2", "task-3"],
        },
        {
            "id": "task-5",
            "title": "Deploy to staging",
            "status": "todo",
            "order": 5,
            "depends_on": ["task-4"],
        },
    ],
    "task_feedback_events": [
        {
            "event_type": "task_start",
            "task_title": "Capture current context",
            "created_at": "2026-03-25T10:00:00Z",
        },
        {
            "event_type": "task_done",
            "task_title": "Capture current context",
            "created_at": "2026-03-25T10:05:00Z",
        },
        {
            "event_type": "task_start",
            "task_title": "Write DAG skeleton",
            "created_at": "2026-03-25T10:06:00Z",
        },
        {
            "event_type": "task_start",
            "task_title": "Set up Airflow env",
            "created_at": "2026-03-25T10:06:30Z",
        },
        {
            "event_type": "task_block",
            "task_title": "Integration tests",
            "created_at": "2026-03-25T10:07:00Z",
        },
    ],
    "agent_cost": {
        "total_cost_usd": 1.23,
        "total_tokens": 45000,
    },
    "enrichment_sources": [
        {
            "name": "Web research",
            "detail": "Best practices for Amazon Managed Workflows for Apache Airflow -> https://docs.aws.amazon.com/mwaa/latest/userguide/best-practices.html\nBest practices for AWS Batch -> https://docs.aws.amazon.com/batch/latest/userguide/best-practices.html",
        }
    ],
    "decisions": {
        "confidence_score": 82,
        "risks": [
            {
                "title": "IAM permissions may be too narrow for Airflow workers to reach Batch and Glue.",
            },
            {
                "description": "Terraform assumptions may not match the existing VPC layout.",
            },
        ],
        "unknowns": [
            {
                "question": "Which environment owns the production DAG bucket?",
            },
        ],
    },
    "id": "plan-abc",
}

EMPTY_PLAN = {
    "title": "Empty plan",
    "status": "draft",
    "id": "plan-empty",
    "plan_steps": [],
    "task_feedback_events": [],
}


# ---------------------------------------------------------------------------
# PlanInsights
# ---------------------------------------------------------------------------


class TestPlanInsights:
    def test_no_data(self):
        w = PlanInsights()
        assert "No plan insights" in w.render()

    def test_no_insights(self):
        w = PlanInsights()
        w.plan_data = EMPTY_PLAN
        assert "No plan insights" in w.render()

    def test_renders_enrichment_and_analysis(self):
        w = PlanInsights()
        w.plan_data = SAMPLE_PLAN
        w.app_url = "http://localhost:3000"
        text = w.render()
        assert "Enriched by:" in text
        assert "Web research" in text
        assert "confidence 82%" in text
        assert "2 risks" in text
        assert "1 open questions" in text
        assert "Top risks:" in text
        assert "Open questions:" in text
        assert "Review in UI:" in text
        assert "http://localhost:3000/plans/plan-abc" in text
        assert '[link="http://localhost:3000/plans/plan-abc"]' in text

    def test_truncates_long_risks(self):
        plan = {
            **SAMPLE_PLAN,
            "decisions": {
                "risks": [{"description": "A" * 200}],
            },
        }
        w = PlanInsights()
        w.plan_data = plan
        text = w.render()
        assert "A" * 120 not in text
        assert "…" in text


# ---------------------------------------------------------------------------
# PlanOverview
# ---------------------------------------------------------------------------


class TestPlanOverview:
    def test_loading_state(self):
        w = PlanOverview()
        assert "Loading" in w.render()

    def test_error_state(self):
        w = PlanOverview()
        w._error = "Connection refused"
        text = w.render()
        assert "Connection error" in text
        assert "Connection refused" in text

    def test_renders_title_and_status(self):
        w = PlanOverview()
        w.plan_data = SAMPLE_PLAN
        text = w.render()
        assert "AWS Batch to Airflow pilot" in text
        assert "in_progress" in text

    def test_renders_progress_counts(self):
        w = PlanOverview()
        w.plan_data = SAMPLE_PLAN
        text = w.render()
        assert "2 active" in text
        assert "1 blocked" in text
        assert "1/5 done" in text

    def test_renders_cost(self):
        w = PlanOverview()
        w.plan_data = SAMPLE_PLAN
        text = w.render()
        assert "$1.23" in text
        assert "45,000" in text

    def test_no_cost_when_absent(self):
        plan = {**SAMPLE_PLAN, "agent_cost": {}}
        w = PlanOverview()
        w.plan_data = plan
        text = w.render()
        assert "Cost" not in text

    def test_live_indicator(self):
        w = PlanOverview()
        w.plan_data = SAMPLE_PLAN
        w._live = True
        assert "live" in w.render()

    def test_polling_indicator(self):
        w = PlanOverview()
        w.plan_data = SAMPLE_PLAN
        w._live = False
        assert "polling" in w.render()

    def test_empty_plan(self):
        w = PlanOverview()
        w.plan_data = EMPTY_PLAN
        text = w.render()
        assert "0/0 done" in text


# ---------------------------------------------------------------------------
# ActiveAgents
# ---------------------------------------------------------------------------


class TestActiveAgents:
    def test_no_data(self):
        w = ActiveAgents()
        assert "No active agents" in w.render()

    def test_shows_active_tasks(self):
        w = ActiveAgents()
        w.plan_data = SAMPLE_PLAN
        text = w.render()
        assert "Write DAG skeleton" in text
        assert "Set up Airflow env" in text

    def test_shows_owner_when_present(self):
        w = ActiveAgents()
        w.plan_data = SAMPLE_PLAN
        text = w.render()
        assert "agent-1" in text

    def test_excludes_non_active(self):
        w = ActiveAgents()
        w.plan_data = SAMPLE_PLAN
        text = w.render()
        assert "Integration tests" not in text
        assert "Deploy to staging" not in text

    def test_empty_plan_shows_no_agents(self):
        w = ActiveAgents()
        w.plan_data = EMPTY_PLAN
        assert "No active agents" in w.render()


# ---------------------------------------------------------------------------
# TaskGraph
# ---------------------------------------------------------------------------


class TestTaskGraph:
    def test_no_data(self):
        w = TaskGraph()
        assert "No tasks" in w.render()

    def test_empty_steps(self):
        w = TaskGraph()
        w.plan_data = EMPTY_PLAN
        assert "No tasks in plan" in w.render()

    def test_renders_all_tasks(self):
        w = TaskGraph()
        w.plan_data = SAMPLE_PLAN
        text = w.render()
        assert "task-1" in text
        assert "task-2" in text
        assert "task-3" in text
        assert "task-4" in text
        assert "task-5" in text

    def test_status_icons(self):
        w = TaskGraph()
        w.plan_data = SAMPLE_PLAN
        text = w.render()
        assert "✓" in text  # completed
        assert "▶" in text  # in_progress
        assert "✗" in text  # blocked
        assert "○" in text  # todo

    def test_shows_dependencies(self):
        w = TaskGraph()
        w.plan_data = SAMPLE_PLAN
        text = w.render()
        assert "task-1" in text  # dependency reference for task-2
        assert "task-2, task-3" in text  # dependencies for task-4

    def test_sorted_by_order(self):
        w = TaskGraph()
        w.plan_data = SAMPLE_PLAN
        text = w.render()
        lines = text.splitlines()
        task_lines = [line for line in lines if "task-" in line]
        ids = []
        for line in task_lines:
            for token in line.split():
                if token.startswith("task-"):
                    ids.append(token)
                    break
        assert ids == ["task-1", "task-2", "task-3", "task-4", "task-5"]

    def test_truncates_long_titles(self):
        plan = {
            "title": "Test",
            "status": "draft",
            "plan_steps": [
                {
                    "id": "task-long",
                    "title": "A" * 80,
                    "status": "todo",
                    "order": 1,
                    "depends_on": [],
                }
            ],
        }
        w = TaskGraph()
        w.plan_data = plan
        text = w.render()
        # Title should be truncated to 50 chars
        assert "A" * 80 not in text
        assert "A" * 50 in text


# ---------------------------------------------------------------------------
# RecentEvents
# ---------------------------------------------------------------------------


class TestRecentEvents:
    def test_no_data(self):
        w = RecentEvents()
        assert "No events" in w.render()

    def test_empty_events(self):
        w = RecentEvents()
        w.plan_data = EMPTY_PLAN
        assert "No recent events" in w.render()

    def test_renders_events(self):
        w = RecentEvents()
        w.plan_data = SAMPLE_PLAN
        text = w.render()
        assert "task_start" in text
        assert "task_done" in text
        assert "task_block" in text

    def test_renders_task_titles(self):
        w = RecentEvents()
        w.plan_data = SAMPLE_PLAN
        text = w.render()
        assert "Capture current context" in text
        assert "Write DAG skeleton" in text
        assert "Integration tests" in text

    def test_renders_timestamps(self):
        w = RecentEvents()
        w.plan_data = SAMPLE_PLAN
        text = w.render()
        assert "2026-03-25 10:00:00" in text

    def test_events_in_reverse_chronological_order(self):
        w = RecentEvents()
        w.plan_data = SAMPLE_PLAN
        text = w.render()
        lines = [line for line in text.splitlines() if "task_" in line]
        # Most recent event first
        assert "task_block" in lines[0]

    def test_limits_to_10_events(self):
        events = [
            {
                "event_type": "task_note",
                "task_title": f"Event {i}",
                "created_at": f"2026-03-25T10:{i:02d}:00Z",
            }
            for i in range(15)
        ]
        plan = {**SAMPLE_PLAN, "task_feedback_events": events}
        w = RecentEvents()
        w.plan_data = plan
        text = w.render()
        event_lines = [el for el in text.splitlines() if "task_note" in el]
        assert len(event_lines) == 10
        # Should show the last 10 (indices 5-14)
        assert "Event 5" in text
        assert "Event 14" in text
        assert "Event 4" not in text


# ---------------------------------------------------------------------------
# KeshroStatusApp — async Textual pilot tests
# ---------------------------------------------------------------------------


class TestKeshroStatusApp:
    def test_init_stores_config(self):
        app = KeshroStatusApp(
            api_url="http://localhost:8000",
            token="tok-123",
            plan_id="plan-abc",
        )
        assert app.api_url == "http://localhost:8000"
        assert app.token == "tok-123"
        assert app.plan_id == "plan-abc"

    @pytest.mark.asyncio
    async def test_app_composes_all_widgets(self, monkeypatch):
        """Verify the app mounts all expected panels."""
        import httpx

        # Mock httpx.Client to avoid real HTTP calls during mount
        class _MockClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, path):
                return _MockResponse(SAMPLE_PLAN)

        class _MockResponse:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        monkeypatch.setattr(httpx, "Client", lambda **kw: _MockClient())

        app = KeshroStatusApp(
            api_url="http://localhost:8000",
            token="tok-123",
            plan_id="plan-abc",
        )
        async with app.run_test():
            assert app.query_one("#overview", PlanOverview)
            assert app.query_one("#insights", PlanInsights)
            assert app.query_one("#agents", ActiveAgents)
            assert app.query_one("#graph", TaskGraph)
            assert app.query_one("#events", RecentEvents)

    @pytest.mark.asyncio
    async def test_fetch_populates_widgets(self, monkeypatch):
        """Verify that a successful fetch updates all widget data."""
        import httpx

        class _MockClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, path):
                return _MockResponse(SAMPLE_PLAN)

        class _MockResponse:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        monkeypatch.setattr(httpx, "Client", lambda **kw: _MockClient())

        app = KeshroStatusApp(
            api_url="http://localhost:8000",
            token="tok-123",
            plan_id="plan-abc",
        )
        async with app.run_test():
            overview = app.query_one("#overview", PlanOverview)
            assert overview.plan_data is not None
            assert overview.plan_data["title"] == "AWS Batch to Airflow pilot"

            insights = app.query_one("#insights", PlanInsights)
            assert insights.plan_data is not None
            assert insights.app_url == "http://localhost:3000"

            agents = app.query_one("#agents", ActiveAgents)
            assert agents.plan_data is not None

    @pytest.mark.asyncio
    async def test_fetch_error_sets_error_state(self, monkeypatch):
        """Verify that a failed fetch shows an error in PlanOverview."""
        import httpx

        class _FailClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, path):
                raise httpx.ConnectError("Connection refused")

        monkeypatch.setattr(httpx, "Client", lambda **kw: _FailClient())

        app = KeshroStatusApp(
            api_url="http://localhost:8000",
            token="tok-123",
            plan_id="plan-abc",
        )
        async with app.run_test():
            overview = app.query_one("#overview", PlanOverview)
            assert overview._error
            assert "Connection refused" in overview._error

    @pytest.mark.asyncio
    async def test_app_title_and_subtitle(self, monkeypatch):
        import httpx

        class _MockClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, path):
                return _MockResponse(SAMPLE_PLAN)

        class _MockResponse:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        monkeypatch.setattr(httpx, "Client", lambda **kw: _MockClient())

        app = KeshroStatusApp(
            api_url="http://localhost:8000",
            token="tok-123",
            plan_id="plan-abcdef123456",
        )
        async with app.run_test():
            assert app.title == "KESHRO STATUS"
            assert "plan-abcdef12345" in app.sub_title

    @pytest.mark.asyncio
    async def test_quit_binding(self, monkeypatch):
        import httpx

        class _MockClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, path):
                return _MockResponse(SAMPLE_PLAN)

        class _MockResponse:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        monkeypatch.setattr(httpx, "Client", lambda **kw: _MockClient())

        app = KeshroStatusApp(
            api_url="http://localhost:8000",
            token="tok-123",
            plan_id="plan-abc",
        )
        async with app.run_test() as pilot:
            await pilot.press("q")
            # App should exit — if we get here, it didn't crash
