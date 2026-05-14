"""Desktop scheduled mechanism backend (AR-S3g).

Two trigger flavours from schema v2:

  (1) time-of-day -- emits a plan of operations the operator applies
      via the mcp__scheduled-tasks__* MCP tools. The registry app is a
      separate process; it cannot invoke MCP tools directly. The
      handler computes the diff between yaml-desired and sqlite-known
      state and returns a list of ``DesktopTaskOp`` actions
      (create / update / disable). An operator (or an L8 PM agent
      per AC-S16) materialises them via the MCP and POSTs an ack.

  (2) state-aware -- the registry writes a SessionStart hook into the
      automation-registry's own .claude/settings.json (or, optionally,
      a target repo's). The hook is a small Python script that reads
      workflow_state.json, evaluates the entry's fire_when DSL, and
      invokes the runner if the predicate is true. This is direct
      filesystem write -- no MCP involved.

Reconcile is idempotent and mirrors cron_handler's shape.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import schema as _schema
import state as _state

log = logging.getLogger("automation-registry.desktop_scheduled")

ROOT = Path(__file__).resolve().parent
DEFAULT_STATE_FILE = ROOT / "data" / "workflow_state.json"
DEFAULT_HOOKS_DIR = ROOT / ".claude" / "hooks"


# ---------------------------------------------------------------------------
# Schema for sqlite tracking
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS desktop_scheduled_entries (
    name                TEXT PRIMARY KEY,
    flavour             TEXT NOT NULL CHECK (flavour IN ('time_of_day','state_aware')),
    schedule            TEXT,                -- cron expr for time_of_day
    trigger_json        TEXT,                -- serialised trigger block for state_aware
    target              TEXT NOT NULL,
    target_kind         TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    enabled             INTEGER NOT NULL DEFAULT 1,
    last_synced_ts      TEXT NOT NULL,
    last_applied_ts     TEXT,                -- set when the operator confirms MCP apply
    hook_path           TEXT,                -- filesystem path for state_aware entries
    fingerprint         TEXT NOT NULL
);
"""


def _ensure_schema(db_path: Path) -> None:
    _state.ensure_schema(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(_SCHEMA_SQL)
        conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Plan-side dataclasses
# ---------------------------------------------------------------------------

@dataclass
class DesktopTaskOp:
    """An operation an operator needs to perform via mcp__scheduled-tasks__*
    to bring the Claude Desktop side in sync with the registry."""
    action: str       # 'create' | 'update' | 'disable'
    task_id: str
    cron_expression: str | None = None
    prompt: str | None = None
    description: str | None = None
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "task_id": self.task_id,
            "cron_expression": self.cron_expression,
            "prompt": self.prompt,
            "description": self.description,
            "enabled": self.enabled,
        }


@dataclass
class StateAwareInstall:
    """Result of installing a state-aware entry on the filesystem side."""
    name: str
    hook_path: str
    state_file: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "hook_path": self.hook_path,
            "state_file": self.state_file,
        }


@dataclass
class ReconcileResult:
    pending_ops: list[DesktopTaskOp] = field(default_factory=list)
    state_aware_installs: list[StateAwareInstall] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pending_ops": [op.to_dict() for op in self.pending_ops],
            "state_aware_installs": [s.to_dict() for s in self.state_aware_installs],
            "unchanged": list(self.unchanged),
            "skipped": list(self.skipped),
            "disabled": list(self.disabled),
        }


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

_RUNNER_INVOCATION = (
    "Run the automation-registry runner for this entry:\n\n"
    "1. Open a terminal at {registry_root}\n"
    "2. Execute: python runner.py --entry {entry_name}\n"
    "3. The runner records the result to sqlite (cron_runs) and "
    "exits with the target's exit code.\n\n"
    "Do not perform any additional work beyond invoking the runner."
)


def _build_time_of_day_prompt(entry: _schema.Automation,
                                registry_root: Path) -> str:
    """The PROMPT body of the Claude Desktop scheduled task. Each fire
    spawns a fresh Claude session with this prompt; the session
    invokes the registry runner."""
    return _RUNNER_INVOCATION.format(
        registry_root=str(registry_root),
        entry_name=entry.name,
    )


# ---------------------------------------------------------------------------
# State-aware hook generator
# ---------------------------------------------------------------------------

_HOOK_TEMPLATE = '''#!/usr/bin/env python3
"""SessionStart hook generated by automation-registry for state-aware
entry {entry_name!r}.

Reads {state_file}, evaluates the fire_when predicate, and invokes
the registry runner if the predicate is true. Cross-platform stdlib
only. Idempotent: if the predicate is false, exits silently with 0.

DO NOT EDIT BY HAND -- regenerated by the registry's reconcile.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Inputs frozen at generation time.
ENTRY_NAME = {entry_name!r}
STATE_FILE = Path({state_file!r})
FIRE_WHEN = {fire_when!r}
ON_FIRE_UPDATE = {on_fire_update!r}
REGISTRY_ROOT = Path({registry_root!r})


def _load_state():
    if not STATE_FILE.is_file():
        return {{}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {{}}


def _save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def main():
    # Late import so a missing registry tree doesn't break Claude
    # session startup -- exit 0 silently instead.
    sys.path.insert(0, str(REGISTRY_ROOT))
    try:
        import state_dsl
    except ImportError:
        return 0

    state = _load_state()
    try:
        should_fire = state_dsl.evaluate(FIRE_WHEN, state)
    except Exception as exc:
        print(f"[registry hook {{ENTRY_NAME!s}}] DSL error: {{exc!s}}",
              file=sys.stderr)
        return 0  # never gate a session on a bad predicate

    if not should_fire:
        return 0

    # Predicate true -> invoke runner.
    proc = subprocess.run(
        [sys.executable, str(REGISTRY_ROOT / "runner.py"),
         "--entry", ENTRY_NAME],
        cwd=str(REGISTRY_ROOT),
    )
    if proc.returncode == 0:
        from datetime import datetime
        state[ON_FIRE_UPDATE] = datetime.now().strftime("%Y-%m-%d")
        _save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def _generate_hook_source(entry: _schema.Automation,
                           registry_root: Path,
                           state_file: Path) -> str:
    trigger = entry.trigger or {}
    return _HOOK_TEMPLATE.format(
        entry_name=entry.name,
        state_file=str(state_file),
        fire_when=trigger.get("fire_when", ""),
        on_fire_update=trigger.get("on_fire_update", ""),
        registry_root=str(registry_root),
    )


def _hook_filename(name: str) -> str:
    return f"registry_state_check_{name.replace('-', '_')}.py"


def _install_state_aware(entry: _schema.Automation, *,
                          hooks_dir: Path,
                          registry_root: Path,
                          state_file: Path) -> StateAwareInstall:
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / _hook_filename(entry.name)
    hook_path.write_text(
        _generate_hook_source(entry, registry_root, state_file),
        encoding="utf-8",
    )
    return StateAwareInstall(
        name=entry.name,
        hook_path=str(hook_path),
        state_file=str(state_file),
    )


def _uninstall_state_aware(name: str, *, hooks_dir: Path) -> bool:
    hook_path = hooks_dir / _hook_filename(name)
    if hook_path.exists():
        hook_path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------

def _fingerprint(entry: _schema.Automation) -> str:
    parts = [
        entry.mechanism,
        entry.schedule or "",
        json.dumps(entry.trigger or {}, sort_keys=True),
        entry.target,
        entry.target_kind,
        entry.description,
        str(entry.enabled),
    ]
    return "|".join(parts)


def reconcile(
    yaml_path: Path | str,
    db_path: Path | str,
    *,
    hooks_dir: Path | None = None,
    registry_root: Path | None = None,
) -> ReconcileResult:
    """Make filesystem-side state match the YAML for Desktop scheduled
    entries. Returns a list of pending MCP ops the operator must apply
    PLUS the state-aware hook files written directly.
    """
    db_path = Path(db_path)
    _ensure_schema(db_path)
    hooks_dir = hooks_dir or DEFAULT_HOOKS_DIR
    registry_root = registry_root or ROOT
    automations = _schema.load_automations(yaml_path)
    relevant = [a for a in automations
                if a.mechanism == "claude_desktop_scheduled"]

    desired_names = {a.name for a in relevant}
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        existing = {
            r["name"]: dict(r) for r in conn.execute(
                "SELECT * FROM desktop_scheduled_entries"
            )
        }

    result = ReconcileResult()

    for auto in relevant:
        flavour = "state_aware" if (auto.trigger and
                                      auto.trigger.get("kind") == "state") \
            else "time_of_day"
        fp = _fingerprint(auto)
        prior = existing.get(auto.name)

        if not auto.enabled:
            if prior:
                if flavour == "time_of_day":
                    result.pending_ops.append(DesktopTaskOp(
                        action="disable",
                        task_id=auto.name,
                    ))
                else:
                    _uninstall_state_aware(auto.name, hooks_dir=hooks_dir)
                _delete_row(db_path, auto.name)
                result.disabled.append(auto.name)
            else:
                result.skipped.append(auto.name)
            continue

        if prior and prior.get("fingerprint") == fp:
            result.unchanged.append(auto.name)
            continue

        if flavour == "time_of_day":
            action = "update" if prior else "create"
            result.pending_ops.append(DesktopTaskOp(
                action=action,
                task_id=auto.name,
                cron_expression=auto.schedule,
                prompt=_build_time_of_day_prompt(auto, registry_root),
                description=auto.description,
                enabled=True,
            ))
        else:
            state_file_path = Path(
                (auto.trigger or {}).get("state_file")
                or str(DEFAULT_STATE_FILE)
            )
            if not state_file_path.is_absolute():
                state_file_path = registry_root / state_file_path
            install = _install_state_aware(
                auto,
                hooks_dir=hooks_dir,
                registry_root=registry_root,
                state_file=state_file_path,
            )
            result.state_aware_installs.append(install)

        _upsert_row(db_path,
                     name=auto.name,
                     flavour=flavour,
                     schedule=auto.schedule,
                     trigger=auto.trigger,
                     target=auto.target,
                     target_kind=auto.target_kind,
                     description=auto.description,
                     enabled=True,
                     hook_path=(install.hook_path
                                 if flavour == "state_aware" else None),
                     fingerprint=fp)

    # Prune entries no longer in YAML.
    for name in list(existing.keys()):
        if name not in desired_names:
            row = existing[name]
            if row["flavour"] == "time_of_day":
                result.pending_ops.append(DesktopTaskOp(
                    action="disable",
                    task_id=name,
                ))
            else:
                _uninstall_state_aware(name, hooks_dir=hooks_dir)
            _delete_row(db_path, name)
            result.disabled.append(name)

    return result


def list_installed(db_path: Path | str) -> list[dict[str, Any]]:
    _ensure_schema(Path(db_path))
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT name, flavour, schedule, target, target_kind, "
            "description, enabled, last_synced_ts, last_applied_ts, "
            "hook_path FROM desktop_scheduled_entries ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def ack_applied(db_path: Path | str, task_ids: list[str]) -> int:
    """Mark a set of time-of-day entries as applied (operator
    confirmed the MCP create/update succeeded). Returns count updated.
    """
    _ensure_schema(Path(db_path))
    now = _now()
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.executemany(
            "UPDATE desktop_scheduled_entries SET last_applied_ts=? "
            "WHERE name=?",
            [(now, name) for name in task_ids],
        )
        conn.commit()
        return cur.rowcount


# ---------------------------------------------------------------------------
# sqlite row helpers
# ---------------------------------------------------------------------------

def _upsert_row(db_path: Path, *, name: str, flavour: str,
                 schedule: str | None, trigger: dict | None,
                 target: str, target_kind: str, description: str,
                 enabled: bool, hook_path: str | None,
                 fingerprint: str) -> None:
    now = _now()
    with sqlite3.connect(str(db_path)) as conn:
        existing = conn.execute(
            "SELECT 1 FROM desktop_scheduled_entries WHERE name=?",
            (name,),
        ).fetchone()
        trigger_json = json.dumps(trigger) if trigger else None
        if existing:
            conn.execute(
                """UPDATE desktop_scheduled_entries
                   SET flavour=?, schedule=?, trigger_json=?, target=?,
                       target_kind=?, description=?, enabled=?,
                       last_synced_ts=?, hook_path=?, fingerprint=?
                   WHERE name=?""",
                (flavour, schedule, trigger_json, target, target_kind,
                 description, int(enabled), now, hook_path, fingerprint,
                 name),
            )
        else:
            conn.execute(
                """INSERT INTO desktop_scheduled_entries
                   (name, flavour, schedule, trigger_json, target,
                    target_kind, description, enabled, last_synced_ts,
                    hook_path, fingerprint)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, flavour, schedule, trigger_json, target, target_kind,
                 description, int(enabled), now, hook_path, fingerprint),
            )
        conn.commit()


def _delete_row(db_path: Path, name: str) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "DELETE FROM desktop_scheduled_entries WHERE name=?", (name,),
        )
        conn.commit()
