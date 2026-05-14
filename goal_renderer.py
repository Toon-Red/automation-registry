"""Goal renderer + setter for claude_loop_continuous (AR-S3j).

Per claude-code-guide investigation: ``/goal`` is built-in to Claude
Code, Stop-hook-evaluated (Haiku judges completion per turn), session-
scoped, callable non-interactively via ``claude -p "/goal <text>"``.

This module provides:

  * ``render_goal(entry, task)`` -- substitutes placeholders in the
    entry's ``goal_template`` with fields from the current work item.
  * ``build_goal_invocation(rendered)`` -- argv list for subprocess;
    tests inspect this without spawning a real ``claude`` process.
  * ``set_goal(rendered, *, runner)`` -- invokes the CLI; default
    runner is ``subprocess.run`` but tests inject a fake.

Substitution is intentionally narrow: dotted-path placeholders resolve
against a whitelist of fields. Anything else raises ``GoalRenderError``.
No Python eval, no ``str.format`` exposure (which would let ``{x.__class__}``
escape).

Supported placeholders (case-sensitive):

    {task.id}             {task.title}        {task.project}
    {task.project_id}     {task.priority}     {task.category}
    {task.complexity}     {task.description}  {task.status}
    {entry.name}          {entry.description} {entry.owner_project}
    {entry.mechanism}
    {now.iso}             {now.date}          {now.hour}

Missing optional fields render as the empty string; unknown
placeholders raise.
"""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping


log = logging.getLogger("automation-registry.goal")

DEFAULT_CLAUDE_BIN = "claude"

# Whitelist: placeholder name -> ("namespace", "key").
# A None key means the lookup uses the namespace dict itself.
_ALLOWED: dict[str, tuple[str, str]] = {
    "task.id":          ("task", "id"),
    "task.title":       ("task", "title"),
    "task.project":     ("task", "project_id"),  # alias for project_id
    "task.project_id":  ("task", "project_id"),
    "task.priority":    ("task", "priority"),
    "task.category":    ("task", "category"),
    "task.complexity":  ("task", "complexity"),
    "task.description": ("task", "description"),
    "task.status":      ("task", "status"),
    "entry.name":          ("entry", "name"),
    "entry.description":   ("entry", "description"),
    "entry.owner_project": ("entry", "owner_project"),
    "entry.mechanism":     ("entry", "mechanism"),
    "now.iso":   ("now", "iso"),
    "now.date":  ("now", "date"),
    "now.hour":  ("now", "hour"),
}

# Matches {namespace.key} -- two segments, each lowercase ASCII + _ .
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+(?:\.[a-z_]+)?)\}")


class GoalRenderError(ValueError):
    """Raised when a template placeholder isn't on the whitelist OR a
    required field is absent from the supplied task/entry."""


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _RenderContext:
    task: Mapping[str, Any]
    entry: Mapping[str, Any]
    now: Mapping[str, Any]


def _now_context(now: datetime | None) -> dict[str, Any]:
    now = now or datetime.now()
    return {
        "iso":  now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "hour": now.hour,
    }


def _entry_dict(entry: Any) -> Mapping[str, Any]:
    """Coerce an Automation-like object or dict to a mapping."""
    if isinstance(entry, Mapping):
        return entry
    return {
        "name": getattr(entry, "name", ""),
        "description": getattr(entry, "description", ""),
        "owner_project": getattr(entry, "owner_project", ""),
        "mechanism": getattr(entry, "mechanism", ""),
    }


def render_goal(entry: Any, task: Mapping[str, Any],
                 *, now: datetime | None = None) -> str:
    """Render the entry's ``goal_template`` against the given work
    item. Returns the substituted string.

    Raises ``GoalRenderError`` on unknown placeholder or non-string
    template. Missing-but-allowed fields render as the empty string.
    """
    entry_map = _entry_dict(entry)
    template = (entry.goal_template if hasattr(entry, "goal_template")
                else entry_map.get("goal_template"))
    if not isinstance(template, str) or not template:
        raise GoalRenderError(
            "entry has no goal_template (required for "
            "claude_loop_continuous /goal rendering)"
        )

    ctx = _RenderContext(
        task=task,
        entry=entry_map,
        now=_now_context(now),
    )

    def _replace(m: re.Match) -> str:
        key = m.group(1)
        if key not in _ALLOWED:
            raise GoalRenderError(
                f"unknown placeholder {{{key}}} -- whitelist: "
                f"{sorted(_ALLOWED)}"
            )
        namespace, field_name = _ALLOWED[key]
        source = getattr(ctx, namespace)
        # task and entry mappings + now dict all use .get / subscript.
        if isinstance(source, Mapping):
            value = source.get(field_name, "")
        else:
            value = getattr(source, field_name, "")
        return str(value) if value is not None else ""

    return _PLACEHOLDER_RE.sub(_replace, template)


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------

def build_goal_invocation(rendered_goal: str,
                            *, claude_bin: str = DEFAULT_CLAUDE_BIN
                            ) -> list[str]:
    """Construct the argv for invoking Claude Code non-interactively
    to set the goal. The CLI accepts ``claude -p "/goal <text>"`` per
    the docs.

    The returned argv is the LIST form -- subprocess args, NOT a shell
    string. Quoting is the caller's job only if they shell-out.
    """
    if not rendered_goal or not isinstance(rendered_goal, str):
        raise GoalRenderError(
            "rendered_goal must be a non-empty string"
        )
    return [claude_bin, "-p", f"/goal {rendered_goal}"]


def set_goal(rendered_goal: str,
              *, claude_bin: str = DEFAULT_CLAUDE_BIN,
              runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
              timeout: int = 30) -> subprocess.CompletedProcess:
    """Invoke the CLI to set the goal for the next iteration. Returns
    the CompletedProcess; callers inspect ``returncode`` to detect
    failure. Tests pass a fake ``runner``."""
    argv = build_goal_invocation(rendered_goal, claude_bin=claude_bin)
    return runner(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Convenience: render + set, for the AR-S3h loop runtime to call once
# ---------------------------------------------------------------------------

def render_and_set(entry: Any, task: Mapping[str, Any],
                    *, now: datetime | None = None,
                    claude_bin: str = DEFAULT_CLAUDE_BIN,
                    runner: Callable = subprocess.run,
                    ) -> tuple[str, subprocess.CompletedProcess]:
    """One-call form for the loop runtime: render the goal, fire it,
    return both the rendered text + the subprocess result."""
    rendered = render_goal(entry, task, now=now)
    log.info("set_goal(entry=%s, task=%s, rendered=%r)",
              getattr(entry, "name", "?"),
              task.get("id", "?"),
              rendered)
    proc = set_goal(rendered, claude_bin=claude_bin, runner=runner)
    return rendered, proc
