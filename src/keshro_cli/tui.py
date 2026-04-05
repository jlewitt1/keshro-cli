"""Textual TUI dashboard for keshro status.

Connects via SSE for real-time updates, falls back to polling.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
from textual.app import App, ComposeResult
from textual.reactive import reactive
from textual.widgets import Footer, Header, Static
from textual.worker import Worker, WorkerState

from .graph import render_progress_bar

STATUS_ICONS = {
    "completed": "✓",
    "in_progress": "▶",
    "blocked": "✗",
    "todo": "○",
}


def _clean(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _plan_analysis(plan: dict) -> dict:
    decisions = plan.get("decisions") or {}
    return decisions if isinstance(decisions, dict) else {}


def _extract_source_titles(source: dict, limit: int = 3) -> list[str]:
    titles: list[str] = []
    for item in source.get("sources") or []:
        title = _clean(item.get("title") or item.get("label") or item.get("url"))
        if title and title not in titles:
            titles.append(title)
        if len(titles) >= limit:
            return titles
    detail = _clean(source.get("detail"))
    if not detail:
        return titles
    for raw_line in detail.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for separator in (" -> ", " — ", " - https://", " - http://"):
            if separator in line:
                line = line.split(separator, 1)[0].strip()
                break
        if line.startswith("http://") or line.startswith("https://"):
            continue
        if line and line not in titles:
            titles.append(line)
        if len(titles) >= limit:
            break
    return titles


def _truncate_text(value: str, limit: int = 110) -> str:
    text = _clean(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _analysis_item_text(item: object, *, fallback: str) -> str:
    if isinstance(item, dict):
        for key in ("title", "description", "question", "summary"):
            value = _clean(item.get(key))
            if value:
                return value
        return fallback
    value = _clean(item)
    return value or fallback


def _confidence_percent(value: object) -> int | None:
    if not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and 0 <= value <= 1:
        return round(value * 100)
    return round(value)


def _app_url_from_api_url(api_url: str) -> str:
    clean = _clean(api_url).rstrip("/")
    if not clean:
        return "https://keshro.com"
    if "localhost" in clean or "127.0.0.1" in clean:
        return clean.replace("://api.", "://").replace(":8000", ":3000")
    if "api." in clean:
        return clean.replace("://api.", "://", 1)
    if clean.endswith("/api"):
        return clean[:-4]
    return clean


def _execution_dashboard_url(plan: dict, app_url: str) -> str:
    migration_id = _clean(plan.get("migration_id"))
    if migration_id:
        return f"{app_url}/migrations/{migration_id}"
    plan_id = _clean(plan.get("id"))
    return f"{app_url}/plans/{plan_id}" if plan_id else ""


class PlanOverview(Static):
    """Shows plan title, progress bar, and summary stats."""

    plan_data: reactive[dict | None] = reactive(None)
    _error: str = ""
    _live: bool = False

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

        mode = "[green]● live[/green]" if self._live else "[yellow]● polling[/yellow]"

        lines = [
            f"[bold cyan]{title}[/bold cyan]  [dim]({status})[/dim]  {mode}",
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


class PlanInsights(Static):
    """Shows enrichment context, risks, open questions, and UI review link."""

    plan_data: reactive[dict | None] = reactive(None)
    app_url: reactive[str] = reactive("")

    def render(self) -> str:
        if not self.plan_data:
            return "[dim]No plan insights yet[/dim]"
        plan = self.plan_data
        analysis = _plan_analysis(plan)
        sources = plan.get("enrichment_sources") or []
        if not sources and not analysis:
            return "[dim]No plan insights yet[/dim]"

        lines = ["[bold]PLAN INSIGHTS[/bold]", ""]

        if sources:
            names = [
                _clean(source.get("name"))
                for source in sources
                if _clean(source.get("name"))
            ]
            if names:
                lines.append(f"[cyan]Enriched by:[/cyan] {', '.join(names)}")
            highlights: list[str] = []
            for source in sources[:2]:
                for title in _extract_source_titles(source, limit=2):
                    if title not in highlights:
                        highlights.append(title)
                    if len(highlights) >= 3:
                        break
                if len(highlights) >= 3:
                    break
            for title in highlights:
                lines.append(f"  [dim]• {_truncate_text(title, 90)}[/dim]")
            if highlights:
                lines.append("")

        confidence = analysis.get("confidence_score")
        confidence_basis = analysis.get("confidence_basis")
        risks = analysis.get("risks") or []
        unknowns = analysis.get("unknowns") or []
        summary_bits: list[str] = []
        confidence_percent = _confidence_percent(confidence)
        if confidence_percent is not None:
            tag = " ✓" if confidence_basis == "outcome_based" else ""
            summary_bits.append(f"confidence {confidence_percent}%{tag}")
        if risks:
            summary_bits.append(f"{len(risks)} risks")
        if unknowns:
            summary_bits.append(f"{len(unknowns)} open questions")
        if summary_bits:
            lines.append(f"[dim]{'  │  '.join(summary_bits)}[/dim]")

        if risks:
            lines.append("[red]Top risks:[/red]")
            for risk in risks[:3]:
                lines.append(
                    f"  • {_truncate_text(_analysis_item_text(risk, fallback='Unspecified risk'), 100)}"
                )
        if unknowns:
            if risks:
                lines.append("")
            lines.append("[yellow]Open questions:[/yellow]")
            for unknown in unknowns[:3]:
                lines.append(
                    f"  • {_truncate_text(_analysis_item_text(unknown, fallback='Unspecified question'), 100)}"
                )

        if self.app_url:
            plan_url = _execution_dashboard_url(plan, self.app_url)
            lines.extend(
                [
                    "",
                    f'[dim]Review in UI:[/dim] [link="{plan_url}"]{plan_url}[/link]',
                ]
            )

        return "\n".join(lines)


class ActiveAgents(Static):
    """Shows currently active tasks (in_progress status)."""

    plan_data: reactive[dict | None] = reactive(None)
    show_full: bool = False

    def render(self) -> str:
        if not self.plan_data:
            return "[dim]No active agents[/dim]"
        steps = self.plan_data.get("plan_steps", [])
        active = [s for s in steps if s.get("status") == "in_progress"]

        if not active:
            return "[dim]No active agents[/dim]"

        lines = ["[bold]ACTIVE TASKS[/bold]", ""]
        for step in sorted(active, key=lambda s: s.get("order", 0)):
            title = step.get("title", "Untitled")
            owner = _clean(step.get("owner"))
            session = step.get("agent_session_id", "")
            badges = []
            if owner:
                badges.append(owner)
            if session:
                badges.append(session)
            session_label = f" [dim]{' · '.join(badges)}[/dim]" if badges else ""
            elapsed = ""
            updated = step.get("last_updated_at", "")
            if updated:
                try:
                    t = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    delta = datetime.now(timezone.utc) - t
                    mins = int(delta.total_seconds() // 60)
                    secs = int(delta.total_seconds() % 60)
                    elapsed = (
                        f" [dim]{mins}m {secs}s[/dim]"
                        if mins > 0
                        else f" [dim]{secs}s[/dim]"
                    )
                except Exception:
                    pass
            lines.append(f"  [yellow]▶[/yellow] {title}{session_label}{elapsed}")

            # Show latest notes for this task (what the agent is doing)
            notes = (step.get("notes") or "").strip()
            if notes:
                note_lines = [
                    note_line.strip()
                    for note_line in notes.split("\n")
                    if note_line.strip()
                ]
                if self.show_full:
                    for nl in note_lines:
                        lines.append(f"    [dim]{nl}[/dim]")
                else:
                    for nl in note_lines[-3:]:
                        display = nl[:80] + "..." if len(nl) > 80 else nl
                        lines.append(f"    [dim]{display}[/dim]")
                    if len(note_lines) > 3:
                        lines.append(
                            f"    [dim]... {len(note_lines) - 3} more — press L for full logs[/dim]"
                        )

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
    """Keshro TUI dashboard — real-time plan monitoring via SSE."""

    CSS = """
    Screen {
        layout: vertical;
        overflow-y: auto;
        padding: 0 1;
    }
    PlanOverview {
        height: auto;
        min-height: 6;
        border: solid $primary;
        padding: 1;
        margin: 0 0 1 0;
    }
    PlanInsights {
        height: auto;
        min-height: 6;
        border: solid $warning;
        padding: 1;
        margin: 0 0 1 0;
    }
    ActiveAgents {
        height: auto;
        min-height: 5;
        border: solid $secondary;
        padding: 1;
        margin: 0 0 1 0;
    }
    TaskGraph {
        height: auto;
        min-height: 6;
        border: solid $secondary;
        padding: 1;
        margin: 0 0 1 0;
    }
    RecentEvents {
        height: auto;
        min-height: 5;
        border: solid $accent;
        padding: 1;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("l", "toggle_logs", "Logs"),
    ]

    _show_full_logs: bool = False

    def __init__(self, api_url: str, token: str, plan_id: str):
        super().__init__()
        self.api_url = api_url
        self.token = token
        self.plan_id = plan_id
        self._last_data: dict | None = None
        self._sse_connected: bool = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield PlanOverview(id="overview")
        yield PlanInsights(id="insights")
        yield ActiveAgents(id="agents")
        yield TaskGraph(id="graph")
        yield RecentEvents(id="events")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "KESHRO STATUS"
        self.sub_title = f"Context: {self.plan_id[:20]}"
        self._fetch_and_update()
        self._start_sse()

    def _start_sse(self) -> None:
        """Start SSE listener as a Textual worker."""
        self._sse_worker = self.run_worker(
            self._sse_listen, exclusive=True, group="sse"
        )

    async def _sse_listen(self) -> None:
        """Connect to SSE stream; on any event, re-fetch plan data."""
        try:
            from httpx_sse import aconnect_sse
        except ImportError:
            self._sse_connected = False
            self._update_live_indicator(False)
            self._start_polling_fallback()
            self.notify(
                "Live updates unavailable — install httpx-sse for real-time: pip install httpx-sse",
                severity="warning",
                timeout=8,
            )
            return

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "text/event-stream",
        }
        try:
            async with httpx.AsyncClient(
                base_url=self.api_url, headers=headers, timeout=None
            ) as client:
                async with aconnect_sse(
                    client, "GET", f"/v1/plans/{self.plan_id}/stream"
                ) as sse:
                    self._sse_connected = True
                    self._update_live_indicator(True)
                    async for event in sse.aiter_sse():
                        # Any event (task_updated, plan_updated, etc.) → re-fetch
                        if event.event and event.event != "comment":
                            self.call_later(self._fetch_and_update)
                    # Stream closed cleanly — fall back to polling
                    self._sse_connected = False
                    self._update_live_indicator(False)
                    self._start_polling_fallback()
        except Exception:
            self._sse_connected = False
            self._update_live_indicator(False)
            # Fall back to polling
            self._start_polling_fallback()

    def _start_polling_fallback(self) -> None:
        """Fall back to 10s polling if SSE is unavailable."""
        self._poll_timer = self.set_interval(3.0, self._fetch_and_update)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """If SSE worker dies, start polling fallback."""
        if event.worker.group == "sse" and event.state == WorkerState.ERROR:
            self._sse_connected = False
            self._update_live_indicator(False)
            self._start_polling_fallback()

    def _update_live_indicator(self, live: bool) -> None:
        """Update the overview panel's live/polling indicator."""
        try:
            overview = self.query_one("#overview", PlanOverview)
            overview._live = live
            overview.refresh()
        except Exception:
            pass

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
                resp = client.get(f"/v1/plans/{self.plan_id}")
                resp.raise_for_status()
                data = resp.json()
            self._last_data = data

            self.query_one("#overview", PlanOverview).plan_data = data
            insights = self.query_one("#insights", PlanInsights)
            insights.plan_data = data
            insights.app_url = _app_url_from_api_url(self.api_url)
            self.query_one("#agents", ActiveAgents).plan_data = data
            self.query_one("#graph", TaskGraph).plan_data = data
            self.query_one("#events", RecentEvents).plan_data = data
            migration_id = _clean(data.get("migration_id"))
            if migration_id:
                self.sub_title = f"Migration: {migration_id[:20]}"
            else:
                self.sub_title = f"Plan: {self.plan_id[:20]}"
        except Exception as exc:
            overview = self.query_one("#overview", PlanOverview)
            overview.plan_data = None
            overview._error = str(exc)

    def action_refresh(self) -> None:
        self._fetch_and_update()

    def action_toggle_logs(self) -> None:
        self._show_full_logs = not self._show_full_logs
        agents_widget = self.query_one("#agents", ActiveAgents)
        agents_widget.show_full = self._show_full_logs
        agents_widget.refresh()  # Force re-render


def run_tui(api_url: str, token: str, plan_id: str) -> None:
    """Launch the Textual TUI dashboard."""
    app = KeshroStatusApp(api_url=api_url, token=token, plan_id=plan_id)
    app.run()
