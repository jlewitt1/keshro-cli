"""Agent integration setup for Claude Code, Codex, and Cursor."""

import os
import re
import sys
from pathlib import Path

from . import __version__
from ._state import GREEN, RESET, YELLOW, _clean


CLAUDE_COMMANDS_DIR = Path.home() / ".claude" / "commands"
CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"
CODEX_HOME_DIR = Path.home() / ".codex"

_SKILL_FILE = Path(__file__).parent / "data" / "SKILL.md"
try:
    KESHRO_SLASH_COMMAND = _SKILL_FILE.read_text()
except FileNotFoundError:
    KESHRO_SLASH_COMMAND = (
        "Keshro — the intelligent execution layer for AI agents. Plans and executes complex engineering tasks.\n"
        "TRIGGER when: user asks to migrate, refactor, convert, upgrade, move, replace, or plan any multi-step engineering task.\n"
        "Run all keshro commands via Bash.\n"
    )


_CODEX_MARKER_BASE = "<!-- keshro-agent-instructions"


_AGENT_REFRESH_COMMANDS = {
    "create",
    "continue",
    "status",
    "task",
    "plan",
    "explain",
    "rollback",
    "migration",
}


def _paths_match(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return False


def _codex_versioned_marker() -> str:
    return f"{_CODEX_MARKER_BASE} v{__version__} -->"


def _install_claude_integration(*, silent: bool = False) -> Path:
    # Install as a skill (auto-triggered) in ~/.claude/skills/keshro/
    skill_dir = CLAUDE_SKILLS_DIR / "keshro"
    skill_dir.mkdir(parents=True, exist_ok=True)
    target = skill_dir / "SKILL.md"
    was_regular_file = target.exists() and not target.is_symlink()
    was_stale_symlink = target.is_symlink() and not _paths_match(target, _SKILL_FILE)
    if target.is_symlink() or target.exists():
        target.unlink()
    try:
        target.symlink_to(_SKILL_FILE)
    except (OSError, NotImplementedError):
        import shutil

        shutil.copy2(_SKILL_FILE, target)
    # Clean up legacy ~/.claude/commands/keshro.md
    legacy = CLAUDE_COMMANDS_DIR / "keshro.md"
    if legacy.is_symlink() or legacy.exists():
        legacy.unlink()
    if (was_regular_file or was_stale_symlink) and not silent:
        print(f"  Updated agent skill to v{__version__}", file=sys.stderr)
    return target


def _install_codex_integration() -> Path:
    CODEX_HOME_DIR.mkdir(parents=True, exist_ok=True)
    target = CODEX_HOME_DIR / "AGENTS.md"
    marker = _codex_versioned_marker()
    keshro_block = f"{marker}\n# Keshro Integration\n\n{KESHRO_SLASH_COMMAND}\n{marker}"
    # Match any version of the marker for replacement
    marker_re = re.compile(
        rf"{re.escape(_CODEX_MARKER_BASE)}[^>]*-->[\s\S]*?{re.escape(_CODEX_MARKER_BASE)}[^>]*-->\n?",
        re.MULTILINE,
    )
    if target.exists():
        content = target.read_text()
        if _CODEX_MARKER_BASE in content:
            next_content = marker_re.sub(keshro_block + "\n", content, count=1).rstrip()
            target.write_text(next_content + "\n")
        else:
            target.write_text(content.rstrip() + "\n\n" + keshro_block + "\n")
    else:
        target.write_text(keshro_block + "\n")
    return target


def _maybe_refresh_codex() -> None:
    """Refresh Codex AGENTS.md if the embedded keshro version is stale."""
    if _clean(os.environ.get("KESHRO_SUPPRESS_AGENT_SKILL_BANNER")):
        return
    target = CODEX_HOME_DIR / "AGENTS.md"
    if not target.exists():
        return
    try:
        content = target.read_text(errors="replace")
        if _CODEX_MARKER_BASE not in content:
            return
        if _codex_versioned_marker() not in content:
            _install_codex_integration()
    except OSError:
        pass


def _maybe_refresh_claude() -> None:
    """Upgrade Claude Code skill if stale, wrong location, or wrong symlink."""
    if _clean(os.environ.get("KESHRO_SUPPRESS_AGENT_SKILL_BANNER")):
        return
    try:
        skill_target = CLAUDE_SKILLS_DIR / "keshro" / "SKILL.md"
        legacy_target = CLAUDE_COMMANDS_DIR / "keshro.md"
        needs_update = False
        if legacy_target.exists() or legacy_target.is_symlink():
            # Old commands/ install -- migrate to skills/
            needs_update = True
        elif not skill_target.exists() and not skill_target.is_symlink():
            return
        elif (
            skill_target.is_symlink()
            and not _paths_match(skill_target, _SKILL_FILE)
        ):
            needs_update = True
        elif not skill_target.is_symlink():
            # Regular file (e.g. Windows copy fallback) -- only update if content differs
            if skill_target.read_text(errors="replace") != KESHRO_SLASH_COMMAND:
                needs_update = True
        if needs_update:
            _install_claude_integration(silent=True)
    except OSError:
        pass


def _install_agent_integrations(silent: bool = False) -> tuple[list[str], list[str]]:
    """Install keshro instructions for all supported agents.

    Returns ``(installed_targets, already_present_targets)``.
    """
    installed: list[str] = []
    already_present: list[str] = []

    # Claude Code
    try:
        target_path = CLAUDE_SKILLS_DIR / "keshro" / "SKILL.md"
        existing = target_path.read_text() if target_path.exists() else None
        target = _install_claude_integration()
        label = f"Claude Code: {target}"
        current = target.read_text() if target.exists() else None
        if existing == current:
            already_present.append(label)
        else:
            installed.append(label)
    except Exception:
        if not silent:
            raise

    # Codex
    try:
        target_path = CODEX_HOME_DIR / "AGENTS.md"
        existing = target_path.read_text() if target_path.exists() else None
        target = _install_codex_integration()
        label = f"Codex: {target}"
        current = target.read_text() if target.exists() else None
        if existing == current:
            already_present.append(label)
        else:
            installed.append(label)
    except Exception:
        if not silent:
            raise

    # Cursor
    try:
        cwd = Path.cwd()
        cursor_file = cwd / ".cursorrules"
        marker = "# keshro-agent-instructions"
        keshro_block = f"{marker}\n{KESHRO_SLASH_COMMAND}\n# end-keshro"
        if cursor_file.exists():
            content = cursor_file.read_text()
            if marker not in content:
                cursor_file.write_text(content.rstrip() + "\n\n" + keshro_block + "\n")
                installed.append(f"Cursor: {cursor_file}")
            else:
                already_present.append(f"Cursor: {cursor_file}")
        else:
            cursor_file.write_text(keshro_block + "\n")
            installed.append(f"Cursor: {cursor_file}")
    except Exception:
        if not silent:
            raise

    return installed, already_present


def _first_cli_command(argv: list[str]) -> str | None:
    skip_next = False
    options_with_values = {"--api-url", "--token"}
    for arg in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg in options_with_values:
            skip_next = True
            continue
        if not arg.startswith("-"):
            return arg.strip()
    return None


def _should_refresh_agent_integrations(argv: list[str]) -> bool:
    command = _first_cli_command(argv)
    if not command:
        return False
    return command in _AGENT_REFRESH_COMMANDS
