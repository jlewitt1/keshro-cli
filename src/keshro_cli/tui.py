"""Textual TUI dashboard for keshro status.

Shows real-time agent progress, dependency graph, and recent events.
Polls the API every 2 seconds.
"""

from __future__ import annotations

import httpx
from textual.app import App, ComposeResult
from textual.reactive import reactive
from textual.widgets import Footer, Header, Static

from .graph import render_progress_bar

STATUS_ICONS = {
    "completed": "✓",
    "in_progress": "▶",
    "blocked": "✗",
    "todo": "○",
}


class PlanOverview(Static):
    """Shows plan title, progress bar, and summary stats."""

    plan_data: reactive[dict | None] = reactive(None)
    _error: str = ""

    def render(self) -> str:
        if self._error:
            return f"[red]Connection error:[/red] {self._error}\n[dim]Check that the backend is running and you're logged in.[/dim]"
        if not self.plan_data:
            return "[dim]Loading plan...[/dim]"
        plan = self.plan_data
        steps = plan.get("plan_steps", [])
        title = plan.get("title", "Untitled")
        status = plan.get("status", "unknown")
        total = len(steps)
        completed = sum(1 for s in steps if s.get("status") == "completed")
        blocked = sum(1 for s in steps if s.get("status") == "blocked")
        in_progress = sum(1 for s in steps if s.get("status") == "in_progress")

        bar = render_progress_bar(completed, total, width=40)

        lines = [
            f"[bold cyan]{title}[/bold cyan]  [dim]({status})[/dim]",
            "",
            f"  {bar}",
            "",
        ]

        parts = []
        if in_progress:
            parts.append(f"[yellow]{in_progress} active[/yellow]")
        if blocked:
            parts.append(f"[red]{blocked} blocked[/red]")
        parts.append(f"{completed}/{total} done")
        lines.append(f"  {'  │  '.join(parts)}")

        cost = plan.get("agent_cost", {})
        if cost.get("total_cost_usd"):
            lines.append(
                f"  [dim]Cost: ${cost['total_cost_usd']:.2f} ({cost.get('total_tokens', 0):,} tokens)[/dim]"
            )

        return "\n".join(lines)


class ActiveAgents(Static):
    """Shows currently active tasks (in_progress status)."""

    plan_data: reactive[dict | None] = reactive(None)

    def render(self) -> str:
        if not self.plan_data:
            return "[dim]No active agents[/dim]"
        steps = self.plan_data.get("plan_steps", [])
        active = [s for s in steps if s.get("status") == "in_progress"]

        if not active:
            return "[dim]No active agents[/dim]"

        lines = ["[bold]ACTIVE TASKS[/bold]", ""]
        for step in active:
            title = step.get("title", "Untitled")
            owner = step.get("owner", "")
            owner_label = f" [dim]({owner})[/dim]" if owner else ""
            lines.append(f"  [yellow]▶[/yellow] {title}{owner_label}")

        return "\n".join(lines)


class TaskGraph(Static):
    """Shows the dependency graph with status indicators."""

    plan_data: reactive[dict | None] = reactive(None)

    def render(self) -> str:
        if not self.plan_data:
            return "[dim]No tasks[/dim]"
        steps = self.plan_data.get("plan_steps", [])
        if not steps:
            return "[dim]No tasks in plan[/dim]"

        lines = ["[bold]TASK GRAPH[/bold]", ""]
        for step in sorted(steps, key=lambda s: s.get("order", 0)):
            status = step.get("status", "todo")
            icon = STATUS_ICONS.get(status, "?")
            title = step.get("title", "Untitled")[:50]
            deps = step.get("depends_on", [])

            style_map = {
                "completed": "green",
                "in_progress": "yellow",
                "blocked": "red",
                "todo": "dim",
            }
            style = style_map.get(status, "dim")
            dep_str = ""
            if deps:
                dep_str = f" [dim]← {', '.join(deps)}[/dim]"

            lines.append(
                f"  [{style}]{icon}[/{style}] {step.get('id', '?'):20s} {title}{dep_str}"
            )

        return "\n".join(lines)


class RecentEvents(Static):
    """Shows recent task feedback events."""

    plan_data: reactive[dict | None] = reactive(None)

    def render(self) -> str:
        if not self.plan_data:
            return "[dim]No events[/dim]"
        events = self.plan_data.get("task_feedback_events", [])
        if not events:
            return "[dim]No recent events[/dim]"

        lines = ["[bold]RECENT EVENTS[/bold]", ""]
        for event in reversed(events[-10:]):
            event_type = event.get("event_type", "unknown")
            task_title = event.get("task_title", "?")
            ts = event.get("created_at", "")[:19].replace("T", " ")

            style_map = {
                "task_start": "yellow",
                "task_done": "green",
                "task_block": "red",
                "task_note": "dim",
            }
            style = style_map.get(event_type, "dim")
            lines.append(
                f"  [dim]{ts}[/dim]  [{style}]{event_type:12s}[/{style}]  {task_title}"
            )

        return "\n".join(lines)


class KeshroStatusApp(App):
    """Keshro TUI dashboard — real-time plan monitoring."""

    CSS = """
    Screen {
        layout: grid;
        grid-size: 2 2;
        grid-gutter: 1;
    }
    PlanOverview {
        column-span: 2;
        height: auto;
        min-height: 8;
        border: solid $primary;
        padding: 1;
    }
    ActiveAgents {
        height: auto;
        min-height: 6;
        border: solid $secondary;
        padding: 1;
    }
    TaskGraph {
        height: auto;
        min-height: 10;
        border: solid $secondary;
        padding: 1;
    }
    RecentEvents {
        column-span: 2;
        height: auto;
        min-height: 8;
        border: solid $accent;
        padding: 1;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
    ]

    def __init__(self, api_url: str, token: str, plan_id: str):
        super().__init__()
        self.api_url = api_url
        self.token = token
        self.plan_id = plan_id
        self._last_data: dict | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield PlanOverview(id="overview")
        yield ActiveAgents(id="agents")
        yield TaskGraph(id="graph")
        yield RecentEvents(id="events")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "KESHRO STATUS"
        self.sub_title = f"Plan: {self.plan_id[:20]}"
        self._fetch_and_update()
        self.set_interval(2.0, self._fetch_and_update)

    def _fetch_and_update(self) -> None:
        try:
            with httpx.Client(
                base_url=self.api_url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                timeout=5,
            ) as client:
                resp = client.get(f"/api/v1/plans/{self.plan_id}")
                resp.raise_for_status()
                data = resp.json()
            self._last_data = data

            self.query_one("#overview", PlanOverview).plan_data = data
            self.query_one("#agents", ActiveAgents).plan_data = data
            self.query_one("#graph", TaskGraph).plan_data = data
            self.query_one("#events", RecentEvents).plan_data = data
        except Exception as exc:
            # Show error in overview panel instead of silently failing
            overview = self.query_one("#overview", PlanOverview)
            overview.plan_data = None
            overview._error = str(exc)

    def action_refresh(self) -> None:
        self._fetch_and_update()


def run_tui(api_url: str, token: str, plan_id: str) -> None:
    """Launch the Textual TUI dashboard."""
    app = KeshroStatusApp(api_url=api_url, token=token, plan_id=plan_id)
    app.run()
