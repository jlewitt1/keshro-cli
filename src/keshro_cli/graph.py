"""Dependency graph ASCII visualization for plans.

Renders task dependency graphs with status-colored nodes:
  ✓ completed  ▶ in_progress  ✗ blocked  ○ todo
"""

from __future__ import annotations

STATUS_ICONS = {
    "completed": "✓",
    "in_progress": "▶",
    "blocked": "✗",
    "todo": "○",
}

# ANSI colors for terminal
RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
CYAN = "\033[36m"

STATUS_COLORS = {
    "completed": GREEN,
    "in_progress": YELLOW,
    "blocked": RED,
    "todo": DIM,
}


def _render_node(task_id: str, status: str, use_color: bool = True) -> str:
    icon = STATUS_ICONS.get(status, "?")
    if use_color:
        color = STATUS_COLORS.get(status, "")
        return f"{color}{icon}{RESET}"
    return icon


def render_graph(steps: list[dict], use_color: bool = True) -> str:
    """Render a dependency graph as ASCII art.

    Returns a multi-line string showing tasks and their dependencies.
    """
    if not steps:
        return "  (no tasks)"

    id_to_step = {s.get("id", f"task-{i}"): s for i, s in enumerate(steps)}
    id_to_order = {
        s.get("id", f"task-{i}"): s.get("order", i) for i, s in enumerate(steps)
    }

    # Find root tasks (no dependencies)
    all_ids = set(id_to_step.keys())
    dependent_ids = set()
    for step in steps:
        for dep in step.get("depends_on", []):
            dependent_ids.add(step.get("id"))

    root_ids = [
        s.get("id")
        for s in sorted(steps, key=lambda s: s.get("order", 0))
        if not s.get("depends_on")
    ]

    # Build adjacency: parent -> children
    children: dict[str, list[str]] = {sid: [] for sid in all_ids}
    for step in steps:
        for dep_id in step.get("depends_on", []):
            if dep_id in children:
                children[dep_id].append(step.get("id", ""))

    lines = []
    visited = set()

    def _render_chain(task_id: str, prefix: str = "  ", is_last: bool = True):
        if task_id in visited or task_id not in id_to_step:
            return
        visited.add(task_id)

        step = id_to_step[task_id]
        status = step.get("status", "todo")
        node = _render_node(task_id, status, use_color)
        title = step.get("title", task_id)

        # Truncate long titles
        max_title = 40
        if len(title) > max_title:
            title = title[: max_title - 1] + "…"

        line = f"{prefix}{node} {task_id}"
        if use_color:
            color = STATUS_COLORS.get(status, "")
            line = f"{prefix}{node} {color}{title}{RESET}"
        else:
            line = f"{prefix}{node} {title}"

        lines.append(line)

        kids = children.get(task_id, [])
        kids_sorted = sorted(kids, key=lambda k: id_to_order.get(k, 0))

        for i, child_id in enumerate(kids_sorted):
            is_child_last = i == len(kids_sorted) - 1
            connector = "└── " if is_child_last else "├── "
            child_prefix = "    " if is_child_last else "│   "
            lines.append(f"{prefix}{connector[:-1]}")
            _render_chain(child_id, prefix + child_prefix, is_child_last)

    # Render from each root
    for root_id in root_ids:
        _render_chain(root_id)

    # Render any orphaned tasks not yet visited
    for step in sorted(steps, key=lambda s: s.get("order", 0)):
        sid = step.get("id")
        if sid not in visited:
            _render_chain(sid)

    return "\n".join(lines) if lines else "  (no tasks)"


def render_progress_bar(completed: int, total: int, width: int = 30) -> str:
    """Render a progress bar like: ████████░░░░░░░░ 5/12 42%"""
    if total == 0:
        return "░" * width + " 0/0 0%"
    pct = completed / total
    filled = int(width * pct)
    bar = "█" * filled + "░" * (width - filled)
    return f"{bar} {completed}/{total} {int(pct * 100)}%"


def render_plan_summary(plan: dict, use_color: bool = True) -> str:
    """Render a compact plan summary with progress and graph."""
    steps = plan.get("plan_steps", [])
    title = plan.get("title", "Untitled")
    status = plan.get("status", "unknown")

    completed = sum(1 for s in steps if s.get("status") == "completed")
    blocked = sum(1 for s in steps if s.get("status") == "blocked")
    in_progress = sum(1 for s in steps if s.get("status") == "in_progress")
    total = len(steps)

    lines = []
    if use_color:
        lines.append(f"  {CYAN}{title}{RESET}  {DIM}({status}){RESET}")
    else:
        lines.append(f"  {title}  ({status})")

    lines.append(f"  {render_progress_bar(completed, total)}")

    parts = []
    if in_progress:
        parts.append(f"{in_progress} active")
    if blocked:
        parts.append(f"{blocked} blocked")
    parts.append(f"{completed}/{total} done")
    lines.append(f"  {' │ '.join(parts)}")
    lines.append("")
    lines.append(render_graph(steps, use_color))

    return "\n".join(lines)
