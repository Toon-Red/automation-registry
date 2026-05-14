"""claude_loop_continuous mechanism backend (AR-S3h).

> This is NOT "a Claude instance in a loop." It runs the L8-L4
> hierarchy (per AC research c1779970 2026-05-13 amendment +
> correction) as a continuously-operating stack as long as there is
> calendar work AND subscription credit.

Hierarchy (Preston verbatim, corrected 2026-05-13):

  - L4 = gruntwork: coder, qa, playtester, AND grader. Grader is a
        HIGH-COMPUTE L4 (uses Claude or similar); standard L4s can run
        on local Ollama. Engine choice is PER-ROLE within the layer.
  - L5 = managers + guides for L4s.
  - L6 = queen (ruflow IS L6).
  - L7 = Dispatch (Preston's operational interface).
  - L8 = Project Manager (Preston's oversight interface, AC-S16).

This module is the catalog-side backend. Mirrors the shape of
``cron_handler`` and ``desktop_scheduled_handler``:

  * ``reconcile(yaml, db)`` -- install / update / disable entries.
  * ``list_installed(db)`` -- sqlite-backed view.
  * ``pause_entry`` / ``resume_entry`` -- runtime control surface used
    by the loop's own internals AND by the watchdog/operator.
  * ``write_heartbeat`` / ``read_heartbeat`` -- the Q-B idle-check
    primitive.
  * ``watchdog_check`` -- the Tier-1-supervises-Tier-1 hook the paired
    Desktop scheduled task invokes (Q-F crash recovery).
  * ``dispatch_iteration`` -- one tick of the L8-L4 stack: probe quota,
    pick work item, render /goal, invoke the per-role engine plan,
    record result. The actual engine driver lands in AC-S10; this
    module ships with an injectable ``stack_runner`` so the surface is
    testable today and pluggable when AC-S10 arrives.

Subscription timer gap (Preston 2026-05-14 (a)/(b)/(c) question):
``quota_probe.probe_quota`` already supports all three -- the
operator-configured flat file (a/b) AND the API-header probe (c). The
loop runtime picks the earliest credible boundary. AR-S3h ships
working under any of Preston's three calls; the differing answer just
changes which source dominates.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import schema as _schema
import state as _state
import goal_renderer
import quota_probe

log = logging.getLogger("automation-registry.loop_continuous")

ROOT = Path(__file__).resolve().parent
DEFAULT_HEARTBEAT_DIR = ROOT / "data" / "heartbeats"
DEFAULT_QUOTA_STATE_PATH = ROOT / "data" / "quota_state.json"

# Idle threshold: heartbeats older than this are considered stale
# (Q-B). 2 minutes is comfortably longer than a normal iteration's
# heartbeat cadence (every ~10s) but short enough that a hung loop is
# noticed within one watchdog tick.
DEFAULT_IDLE_AFTER_SECONDS = 120

STATUS_INSTALLED = "installed"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_DISABLED = "disabled"
STATUS_CRASHED = "crashed"


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS loop_continuous_entries (
    name              TEXT PRIMARY KEY,
    engines_json      TEXT NOT NULL,
    goal_template     TEXT,
    limit_aware_json  TEXT,
    target            TEXT NOT NULL,
    target_kind       TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    enabled           INTEGER NOT NULL DEFAULT 1,
    status            TEXT NOT NULL DEFAULT 'installed',
    pid               INTEGER,
    started_at        TEXT,
    paused_at         TEXT,
    paused_reason     TEXT,
    next_reset_ts     TEXT,
    active_work_item  TEXT,
    last_heartbeat_ts TEXT,
    last_iteration_ts TEXT,
    fingerprint       TEXT NOT NULL,
    last_synced_ts    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS loop_continuous_iterations (
    iter_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_name     TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    ended_at       TEXT,
    work_item_id   TEXT,
    rendered_goal  TEXT,
    outcome        TEXT,   -- 'succeeded' | 'failed' | 'rate_limited' | 'no_work'
    detail         TEXT
);

CREATE INDEX IF NOT EXISTS idx_loop_iters_entry_started
    ON loop_continuous_iterations (entry_name, started_at DESC);
"""


def _ensure_schema(db_path: Path) -> None:
    _state.ensure_schema(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(_SCHEMA_SQL)
        conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------

@dataclass
class ReconcileResult:
    installed: list[str] = field(default_factory=list)
    reinstalled: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "installed": list(self.installed),
            "reinstalled": list(self.reinstalled),
            "unchanged": list(self.unchanged),
            "disabled": list(self.disabled),
            "skipped": list(self.skipped),
        }


def _fingerprint(entry: _schema.Automation) -> str:
    parts = [
        entry.mechanism,
        json.dumps(entry.engines or {}, sort_keys=True),
        entry.goal_template or "",
        json.dumps(entry.limit_aware or {}, sort_keys=True),
        entry.target,
        entry.target_kind,
        entry.description,
        str(entry.enabled),
    ]
    return "|".join(parts)


def reconcile(yaml_path: Path | str,
              db_path: Path | str) -> ReconcileResult:
    """Make sqlite state match the YAML for claude_loop_continuous
    entries.

    The Tier-1 'install' is a catalog-side commitment: the loop process
    is NOT spawned here -- the runtime spawn happens out-of-band (the
    paired Desktop scheduled task per Q-F, OR an operator invoking
    ``dispatch_iteration`` via the runner). Reconcile just keeps the
    sqlite row + fingerprint in step with the YAML and disables removed
    entries.
    """
    db_path = Path(db_path)
    _ensure_schema(db_path)
    automations = _schema.load_automations(yaml_path)
    relevant = [a for a in automations
                if a.mechanism == "claude_loop_continuous"]

    desired_names = {a.name for a in relevant}
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        existing = {
            r["name"]: dict(r) for r in conn.execute(
                "SELECT * FROM loop_continuous_entries"
            )
        }

    result = ReconcileResult()
    for auto in relevant:
        fp = _fingerprint(auto)
        prior = existing.get(auto.name)

        if not auto.enabled:
            if prior:
                _set_status(db_path, auto.name, STATUS_DISABLED)
                result.disabled.append(auto.name)
            else:
                result.skipped.append(auto.name)
            continue

        if prior and prior.get("fingerprint") == fp:
            result.unchanged.append(auto.name)
            continue

        _upsert_entry(db_path, auto, fingerprint=fp)
        if prior:
            result.reinstalled.append(auto.name)
        else:
            result.installed.append(auto.name)

    # Prune entries the YAML no longer mentions.
    for name in list(existing.keys()):
        if name not in desired_names:
            _delete_entry(db_path, name)
            result.disabled.append(name)

    return result


def _upsert_entry(db_path: Path, auto: _schema.Automation, *,
                    fingerprint: str) -> None:
    now = _now()
    engines_json = json.dumps(auto.engines or {}, sort_keys=True)
    limit_aware_json = json.dumps(auto.limit_aware or {}, sort_keys=True)
    with sqlite3.connect(str(db_path)) as conn:
        existing = conn.execute(
            "SELECT 1 FROM loop_continuous_entries WHERE name=?",
            (auto.name,),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE loop_continuous_entries
                   SET engines_json=?, goal_template=?, limit_aware_json=?,
                       target=?, target_kind=?, description=?, enabled=?,
                       fingerprint=?, last_synced_ts=?,
                       status=CASE WHEN status='disabled' THEN 'installed'
                                   ELSE status END
                   WHERE name=?""",
                (engines_json, auto.goal_template, limit_aware_json,
                 auto.target, auto.target_kind, auto.description,
                 int(auto.enabled), fingerprint, now, auto.name),
            )
        else:
            conn.execute(
                """INSERT INTO loop_continuous_entries
                   (name, engines_json, goal_template, limit_aware_json,
                    target, target_kind, description, enabled, status,
                    fingerprint, last_synced_ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'installed', ?, ?)""",
                (auto.name, engines_json, auto.goal_template,
                 limit_aware_json, auto.target, auto.target_kind,
                 auto.description, int(auto.enabled), fingerprint, now),
            )
        conn.commit()


def _delete_entry(db_path: Path, name: str) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "DELETE FROM loop_continuous_entries WHERE name=?", (name,),
        )
        conn.commit()


def _set_status(db_path: Path, name: str, status: str, **fields: Any) -> None:
    fields = {"status": status, **fields, "last_synced_ts": _now()}
    cols = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [name]
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            f"UPDATE loop_continuous_entries SET {cols} WHERE name=?",
            values,
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Public reads
# ---------------------------------------------------------------------------

def list_installed(db_path: Path | str) -> list[dict[str, Any]]:
    db_path = Path(db_path)
    _ensure_schema(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT name, status, target, target_kind, description,
                      enabled, pid, started_at, paused_at, paused_reason,
                      next_reset_ts, active_work_item, last_heartbeat_ts,
                      last_iteration_ts, last_synced_ts, engines_json,
                      goal_template, limit_aware_json
               FROM loop_continuous_entries ORDER BY name"""
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["engines"] = json.loads(d.pop("engines_json") or "{}")
        d["limit_aware"] = json.loads(d.pop("limit_aware_json") or "{}")
        d["enabled"] = bool(d["enabled"])
        out.append(d)
    return out


def get_entry(db_path: Path | str, name: str) -> dict[str, Any] | None:
    for r in list_installed(db_path):
        if r["name"] == name:
            return r
    return None


# ---------------------------------------------------------------------------
# Heartbeat (Q-B idle check)
# ---------------------------------------------------------------------------

def heartbeat_path(name: str, *,
                    heartbeat_dir: Path = DEFAULT_HEARTBEAT_DIR) -> Path:
    return heartbeat_dir / f"{name}.heartbeat.json"


def write_heartbeat(name: str, *,
                     pid: int | None = None,
                     active_work_item: str | None = None,
                     heartbeat_dir: Path = DEFAULT_HEARTBEAT_DIR) -> Path:
    """Touch the heartbeat file for ``name``. Called once per
    iteration by the loop runtime so external watchers can tell whether
    the loop is alive.

    The file body carries the pid + active work item id; the file
    mtime carries the "last heartbeat" timestamp the idle check uses.
    """
    heartbeat_dir.mkdir(parents=True, exist_ok=True)
    p = heartbeat_path(name, heartbeat_dir=heartbeat_dir)
    payload = {
        "name": name,
        "pid": pid if pid is not None else os.getpid(),
        "ts": _now(),
        "active_work_item": active_work_item,
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return p


def read_heartbeat(name: str, *,
                    heartbeat_dir: Path = DEFAULT_HEARTBEAT_DIR
                    ) -> dict[str, Any] | None:
    p = heartbeat_path(name, heartbeat_dir=heartbeat_dir)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data


def is_idle(name: str, *,
            idle_after_seconds: int = DEFAULT_IDLE_AFTER_SECONDS,
            heartbeat_dir: Path = DEFAULT_HEARTBEAT_DIR,
            now: datetime | None = None) -> bool:
    """True if the named entry's heartbeat is missing OR older than the
    idle threshold. The resume-checker uses this to decide whether to
    kick off the next iteration."""
    p = heartbeat_path(name, heartbeat_dir=heartbeat_dir)
    if not p.is_file():
        return True
    now = now or datetime.now(timezone.utc)
    data = read_heartbeat(name, heartbeat_dir=heartbeat_dir) or {}
    ts = data.get("ts")
    if not ts:
        return True
    try:
        last = quota_probe.parse_iso(ts)
    except ValueError:
        return True
    return (now - last).total_seconds() > idle_after_seconds


def clear_heartbeat(name: str, *,
                     heartbeat_dir: Path = DEFAULT_HEARTBEAT_DIR) -> None:
    p = heartbeat_path(name, heartbeat_dir=heartbeat_dir)
    if p.exists():
        p.unlink()


# ---------------------------------------------------------------------------
# Pause / resume
# ---------------------------------------------------------------------------

def pause_entry(db_path: Path | str, name: str, *,
                 reason: str = "rate_limit",
                 next_reset_ts: str | None = None,
                 active_work_item: str | None = None,
                 heartbeat_dir: Path = DEFAULT_HEARTBEAT_DIR) -> None:
    """Record a clean pause. Q-D: thin state -- active work item +
    pause timestamp + the reset boundary we're waiting for.

    Clears the entry's heartbeat: by definition, a paused loop is no
    longer doing work and shouldn't masquerade as alive to the
    watchdog's idle check.
    """
    db_path = Path(db_path)
    _ensure_schema(db_path)
    fields = {
        "paused_at": _now(),
        "paused_reason": reason,
        "next_reset_ts": next_reset_ts,
    }
    if active_work_item is not None:
        fields["active_work_item"] = active_work_item
    _set_status(db_path, name, STATUS_PAUSED, **fields)
    clear_heartbeat(name, heartbeat_dir=heartbeat_dir)


def resume_entry(db_path: Path | str, name: str) -> None:
    """Mark an entry resumed. The runtime should already be checking
    idleness via the heartbeat; this just clears the pause markers."""
    db_path = Path(db_path)
    _ensure_schema(db_path)
    _set_status(db_path, name, STATUS_RUNNING,
                 paused_at=None, paused_reason=None,
                 started_at=_now())


def mark_running(db_path: Path | str, name: str, *,
                  pid: int | None = None) -> None:
    db_path = Path(db_path)
    _ensure_schema(db_path)
    _set_status(db_path, name, STATUS_RUNNING,
                 pid=(pid if pid is not None else os.getpid()),
                 started_at=_now())


def record_heartbeat_ts(db_path: Path | str, name: str) -> None:
    db_path = Path(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE loop_continuous_entries SET last_heartbeat_ts=? "
            "WHERE name=?",
            (_now(), name),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Work-item selection (Q-G)
# ---------------------------------------------------------------------------

def default_work_item_picker(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Stub Q-G picker. The real implementation consults today's
    calendar entries + the PD priority queue + idle behaviour. This
    module ships with a stub so the rest of the surface is testable;
    callers inject their own picker via ``dispatch_iteration``.

    Default: returns None ("no work available"). The loop runtime
    interprets None as a no-op iteration.
    """
    return None


# ---------------------------------------------------------------------------
# Iteration: probe -> pick work -> /goal -> invoke L8-L4 -> record
# ---------------------------------------------------------------------------

@dataclass
class IterationResult:
    entry_name: str
    work_item_id: str | None
    rendered_goal: str | None
    outcome: str               # 'succeeded' | 'failed' | 'rate_limited' | 'no_work'
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_name": self.entry_name,
            "work_item_id": self.work_item_id,
            "rendered_goal": self.rendered_goal,
            "outcome": self.outcome,
            "detail": self.detail,
        }


def _record_iteration(db_path: Path, *, entry_name: str,
                       work_item_id: str | None,
                       rendered_goal: str | None,
                       outcome: str, detail: str,
                       started_at: str) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """INSERT INTO loop_continuous_iterations
               (entry_name, started_at, ended_at, work_item_id,
                rendered_goal, outcome, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (entry_name, started_at, _now(), work_item_id,
             rendered_goal, outcome, detail),
        )
        conn.execute(
            "UPDATE loop_continuous_entries SET last_iteration_ts=? "
            "WHERE name=?",
            (_now(), entry_name),
        )
        conn.commit()


def build_stack_plan(entry: _schema.Automation | Mapping[str, Any],
                      *, rendered_goal: str,
                      work_item: Mapping[str, Any]) -> dict[str, Any]:
    """Build the invocation plan for the L8-L4 stack. The real driver
    is AC-S10 (multi-engine driver) -- TODO on AC project. Until it
    lands, this returns a structured plan that the stack runner can
    consume verbatim (or that tests can assert against).

    Per-role engine resolution: the entry's ``engines`` map carries
    LAYER -> engine ids. Per-role overrides within a layer are AC-S2 /
    AC-S9 territory; the registry just hands the layer map down.
    """
    engines = (getattr(entry, "engines", None)
                or (entry.get("engines") if isinstance(entry, Mapping)
                     else None) or {})
    return {
        "goal": rendered_goal,
        "work_item": dict(work_item),
        "layers": {
            "L4": {"engine": engines.get("L4"),
                    "roles": ["coder", "qa", "playtester", "grader"]},
            "L5": {"engine": engines.get("L5"),
                    "roles": ["manager", "guide"]},
            "L6": {"engine": engines.get("L6"),
                    "roles": ["queen"]},
            "L7": {"engine": engines.get("L7"),
                    "roles": ["dispatch"]},
            "L8": {"engine": engines.get("L8"),
                    "roles": ["project_manager"]},
        },
    }


def _default_stack_runner(plan: dict[str, Any]) -> dict[str, Any]:
    """Stub stack runner shipped pre-AC-S10. Returns a 'no_engine' result
    so the loop can still record iterations without crashing. Real
    deployments inject a runner that calls AC's multi-engine driver."""
    return {
        "outcome": "succeeded",
        "detail": ("stub stack runner (AC-S10 pending) -- plan recorded; "
                    "no real engine invocation"),
        "plan": plan,
    }


def dispatch_iteration(
    entry_name: str,
    *,
    yaml_path: Path | str,
    db_path: Path | str,
    work_item_picker: Callable[[Mapping[str, Any]], Mapping[str, Any] | None]
        = default_work_item_picker,
    stack_runner: Callable[[dict[str, Any]], Mapping[str, Any]]
        = _default_stack_runner,
    quota_state: quota_probe.QuotaState | None = None,
    heartbeat_dir: Path = DEFAULT_HEARTBEAT_DIR,
    now: datetime | None = None,
    pid: int | None = None,
) -> IterationResult:
    """Run ONE tick of the L8-L4 stack for ``entry_name``.

    Steps:
      1. Look up the entry. Bail with 'no_work' if disabled / paused.
      2. Pick a work item via ``work_item_picker``. None -> 'no_work'.
      3. Render the /goal via ``goal_renderer.render_goal``.
      4. Build the per-role engine plan; invoke ``stack_runner``.
      5. Inspect runner outcome:
           - 'rate_limited' -> ``pause_entry`` with the quota's
             next_reset_ts; iteration outcome 'rate_limited'.
           - 'succeeded' / 'failed' -> recorded as-is.
      6. Touch the heartbeat + insert iteration row.

    Returns an :class:`IterationResult`.
    """
    db_path = Path(db_path)
    _ensure_schema(db_path)

    started_at = _now()
    autos = _schema.load_automations(yaml_path)
    match = [a for a in autos if a.name == entry_name]
    if not match:
        return IterationResult(
            entry_name=entry_name,
            work_item_id=None,
            rendered_goal=None,
            outcome="failed",
            detail=f"no such entry in yaml: {entry_name!r}",
        )
    entry = match[0]

    row = get_entry(db_path, entry_name)
    if row is None:
        # Not yet reconciled; treat as failed so the operator notices.
        return IterationResult(
            entry_name=entry_name,
            work_item_id=None,
            rendered_goal=None,
            outcome="failed",
            detail="entry not reconciled into sqlite -- run reconcile first",
        )
    if row["status"] == STATUS_DISABLED or not row["enabled"]:
        return IterationResult(
            entry_name=entry_name,
            work_item_id=None,
            rendered_goal=None,
            outcome="no_work",
            detail="entry disabled",
        )
    if row["status"] == STATUS_PAUSED:
        return IterationResult(
            entry_name=entry_name,
            work_item_id=None,
            rendered_goal=None,
            outcome="no_work",
            detail=f"entry paused: {row.get('paused_reason')}",
        )

    work_item = work_item_picker(row)
    if not work_item:
        write_heartbeat(entry_name, pid=pid,
                         active_work_item=None,
                         heartbeat_dir=heartbeat_dir)
        record_heartbeat_ts(db_path, entry_name)
        _record_iteration(db_path,
                           entry_name=entry_name,
                           work_item_id=None,
                           rendered_goal=None,
                           outcome="no_work",
                           detail="picker returned None",
                           started_at=started_at)
        return IterationResult(
            entry_name=entry_name,
            work_item_id=None,
            rendered_goal=None,
            outcome="no_work",
            detail="picker returned None",
        )

    # Q-9: per-iteration /goal render (AR-S3j integration).
    try:
        rendered = goal_renderer.render_goal(entry, work_item, now=now)
    except goal_renderer.GoalRenderError as exc:
        detail = f"goal render failed: {exc!s}"
        _record_iteration(db_path,
                           entry_name=entry_name,
                           work_item_id=str(work_item.get("id") or ""),
                           rendered_goal=None,
                           outcome="failed",
                           detail=detail,
                           started_at=started_at)
        return IterationResult(
            entry_name=entry_name,
            work_item_id=str(work_item.get("id") or ""),
            rendered_goal=None,
            outcome="failed",
            detail=detail,
        )

    plan = build_stack_plan(entry, rendered_goal=rendered,
                             work_item=work_item)
    write_heartbeat(entry_name, pid=pid,
                     active_work_item=str(work_item.get("id") or ""),
                     heartbeat_dir=heartbeat_dir)
    record_heartbeat_ts(db_path, entry_name)

    try:
        runner_out = stack_runner(plan) or {}
    except Exception as exc:  # pragma: no cover -- defensive
        runner_out = {"outcome": "failed", "detail": f"stack_runner raised: {exc!s}"}

    outcome = runner_out.get("outcome", "failed")
    detail = str(runner_out.get("detail", ""))

    if outcome == "rate_limited":
        # Pause + record the reset boundary for the resume timer.
        next_reset = (quota_state.next_reset_ts
                       if quota_state else None)
        pause_entry(db_path, entry_name,
                     reason=detail or "rate_limit",
                     next_reset_ts=next_reset,
                     active_work_item=str(work_item.get("id") or ""),
                     heartbeat_dir=heartbeat_dir)

    _record_iteration(db_path,
                       entry_name=entry_name,
                       work_item_id=str(work_item.get("id") or ""),
                       rendered_goal=rendered,
                       outcome=outcome,
                       detail=detail,
                       started_at=started_at)
    return IterationResult(
        entry_name=entry_name,
        work_item_id=str(work_item.get("id") or ""),
        rendered_goal=rendered,
        outcome=outcome,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Watchdog (Q-F crash recovery)
# ---------------------------------------------------------------------------

@dataclass
class WatchdogResult:
    name: str
    action: str    # 'noop' | 'resumed' | 'flagged_crashed' | 'still_paused'
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "action": self.action, "detail": self.detail}


def watchdog_check(
    db_path: Path | str,
    *,
    heartbeat_dir: Path = DEFAULT_HEARTBEAT_DIR,
    idle_after_seconds: int = DEFAULT_IDLE_AFTER_SECONDS,
    now: datetime | None = None,
    resumer: Callable[[str], None] | None = None,
) -> list[WatchdogResult]:
    """One pass for the paired Desktop scheduled watchdog (per Q-F).

    Logic per entry:
      - status=running + stale heartbeat -> mark CRASHED + log; if a
        ``resumer`` is supplied, invoke it (the resumer is the bit
        that actually restarts the loop process -- subprocess.Popen
        in production, a fake in tests).
      - status=paused + next_reset_ts in the past + loop is idle ->
        resume_entry + invoke resumer.
      - status=paused + still within window -> 'still_paused'.
      - status=installed/disabled -> noop.
    """
    db_path = Path(db_path)
    _ensure_schema(db_path)
    now = now or datetime.now(timezone.utc)
    results: list[WatchdogResult] = []
    for row in list_installed(db_path):
        name = row["name"]
        status = row["status"]
        if status == STATUS_RUNNING:
            if is_idle(name,
                        idle_after_seconds=idle_after_seconds,
                        heartbeat_dir=heartbeat_dir,
                        now=now):
                _set_status(db_path, name, STATUS_CRASHED)
                if resumer is not None:
                    resumer(name)
                results.append(WatchdogResult(
                    name=name, action="flagged_crashed",
                    detail="stale heartbeat",
                ))
            else:
                results.append(WatchdogResult(name=name, action="noop",
                                                detail="alive"))
            continue
        if status == STATUS_PAUSED:
            next_reset = row.get("next_reset_ts")
            if next_reset:
                try:
                    reset_dt = quota_probe.parse_iso(next_reset)
                except ValueError:
                    reset_dt = None
            else:
                reset_dt = None
            if reset_dt and reset_dt <= now and is_idle(
                name, idle_after_seconds=idle_after_seconds,
                heartbeat_dir=heartbeat_dir, now=now,
            ):
                resume_entry(db_path, name)
                if resumer is not None:
                    resumer(name)
                results.append(WatchdogResult(
                    name=name, action="resumed",
                    detail=f"reset_ts {next_reset} passed; loop idle",
                ))
            else:
                results.append(WatchdogResult(
                    name=name, action="still_paused",
                    detail=f"waiting for {next_reset}",
                ))
            continue
        if status == STATUS_CRASHED:
            # Crashed and not yet recovered -- recovery attempt this tick.
            if resumer is not None:
                resumer(name)
                resume_entry(db_path, name)
                results.append(WatchdogResult(
                    name=name, action="resumed",
                    detail="crashed entry restarted",
                ))
            else:
                results.append(WatchdogResult(
                    name=name, action="noop",
                    detail="crashed (no resumer wired)",
                ))
            continue
        results.append(WatchdogResult(name=name, action="noop",
                                        detail=f"status={status}"))
    return results


# ---------------------------------------------------------------------------
# Tiny iteration listing helper for the API
# ---------------------------------------------------------------------------

def list_iterations(db_path: Path | str, name: str, *,
                     limit: int = 50) -> list[dict[str, Any]]:
    db_path = Path(db_path)
    _ensure_schema(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT iter_id, entry_name, started_at, ended_at,
                      work_item_id, rendered_goal, outcome, detail
               FROM loop_continuous_iterations
               WHERE entry_name=?
               ORDER BY started_at DESC LIMIT ?""",
            (name, limit),
        ).fetchall()
    return [dict(r) for r in rows]
