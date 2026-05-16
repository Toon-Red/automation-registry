"""Smart auto-resume scheduler (AR-S3k).

Refines AR-S3h. The earlier conclusion -- "Dream Auto's 30-min cron is
sufficient" -- is partially wrong: a fixed-interval cron fires regardless
of whether the limit has reset, wasting Anthropic quota AND local compute
on spawns during locked-out windows.

Preston directive (verbatim 2026-05-14):
    "But not every 5 min or so, but the minute after the reset is hit.
     This way we don't have any unneeded instances going on."

This module implements that "fire ONCE at reset+buffer" model:

  1. ``quota_probe`` (already shipped under AR-S3h) extracts the
     ``anthropic-ratelimit-*-reset`` headers AND honours the operator's
     manual subscription timer file. The earliest credible boundary is
     persisted to ``data/quota_state.json``.
  2. :func:`apply_from_quota_state` reads that snapshot, computes
     ``fire_at = next_reset_ts + buffer_seconds`` (default 60s to absorb
     clock drift + provider propagation), and installs a **one-shot**
     scheduled task that runs ``runner.py --entry <name>`` exactly once.
  3. Any prior pending resume for the same entry is cancelled first so
     re-probing after a fresh response simply rolls the timer forward.
  4. The runtime calls :func:`mark_fired` when the one-shot actually
     runs; the next session's first response repeats the cycle.

DONE-WHEN (from the task spec):
  * A session that hits the rate limit auto-resumes within
    ``buffer_seconds`` of refresh (default 60s).
  * Zero fired spawns during a locked-out window -- the one-shot is the
    ONLY firing path; the previous Dream Auto 30-min cron can be
    retired or kept as a once-a-day safety net (operator decision).

Scheduler abstraction
---------------------

The OS-side schedule install is hidden behind a small Protocol so the
module is testable without touching real schtasks/at and so a POSIX
backend can ship later without disturbing the Windows path:

  * :class:`WindowsSchTasksScheduler` -- ``schtasks /Create /SC ONCE``.
  * :class:`FileMarkerScheduler`      -- writes / reads
    ``data/pending_resume/<name>.json`` markers. Used in tests AND in
    POSIX deployments where the operator drives a tiny ``at``-equivalent
    sidecar of their own choosing.

Both expose ``install(task_id, fire_at_iso, command_argv)`` and
``cancel(task_id)``. The default backend is picked by
:func:`default_scheduler` from the running platform.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

import quota_probe
import state as _state

log = logging.getLogger("automation-registry.smart_resume")

ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = ROOT / "data" / "registry.db"
DEFAULT_QUOTA_STATE_PATH = ROOT / "data" / "quota_state.json"
DEFAULT_MARKER_DIR = ROOT / "data" / "pending_resume"

# Buffer in seconds the resume timer adds on top of the reset boundary
# (clock drift + provider propagation; Preston "the minute after the
# reset is hit" -> 60s default).
DEFAULT_BUFFER_SECONDS = 60

STATUS_PENDING = "pending"
STATUS_FIRED = "fired"
STATUS_CANCELLED = "cancelled"

# Windows schtasks task name prefix. Kept short + lower-case so the OS
# task list groups entries together under a single visual prefix.
SCHTASKS_PREFIX = "ar-resume-"


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS smart_resume_pending (
    entry_name        TEXT PRIMARY KEY,
    fire_at_ts        TEXT NOT NULL,
    reset_source_ts   TEXT NOT NULL,
    reset_source      TEXT NOT NULL,    -- 'api-header' | 'operator' | ...
    buffer_seconds    INTEGER NOT NULL,
    scheduled_at      TEXT NOT NULL,
    schedule_id       TEXT NOT NULL,    -- backend-specific handle
    backend           TEXT NOT NULL,    -- 'schtasks' | 'file-marker' | ...
    status            TEXT NOT NULL,    -- pending | fired | cancelled
    last_fired_at     TEXT,
    notes             TEXT
);

CREATE INDEX IF NOT EXISTS idx_smart_resume_pending_status
    ON smart_resume_pending (status, fire_at_ts);
"""


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat(timespec="seconds")


def compute_fire_at(reset_ts: str, *,
                     buffer_seconds: int = DEFAULT_BUFFER_SECONDS,
                     now: datetime | None = None) -> str:
    """Return the ISO timestamp at which the one-shot should fire.

    ``fire_at = reset_ts + buffer_seconds``. If the resulting moment is
    already in the past relative to ``now``, returns ``now + 1s`` so the
    OS scheduler doesn't reject the task (schtasks refuses fire_at in
    the past). That "fire immediately" behaviour is correct here:
    the operator/runtime is re-scheduling AFTER a reset they already
    missed, so we want it to kick on the next tick.
    """
    reset = quota_probe.parse_iso(reset_ts)
    fire_at = reset + timedelta(seconds=buffer_seconds)
    now = now or _now_utc()
    if fire_at <= now:
        fire_at = now + timedelta(seconds=1)
    return fire_at.astimezone(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Scheduler protocol + concrete backends
# ---------------------------------------------------------------------------

class Scheduler(Protocol):
    """One-shot scheduler abstraction.

    Implementations install a single firing at ``fire_at_iso`` that
    executes ``command_argv`` and self-deletes afterwards. ``task_id``
    is a stable handle the registry uses to identify / cancel a prior
    install for the same entry.
    """
    name: str

    def install(self, task_id: str, fire_at_iso: str,
                 command_argv: list[str]) -> str:
        """Install the one-shot. Returns a backend-specific schedule_id
        the registry stores so it can later cancel."""

    def cancel(self, task_id: str) -> None:
        """Cancel a previously-installed one-shot. No-op if absent."""


@dataclass
class WindowsSchTasksScheduler:
    """Default Windows backend. Wraps ``schtasks /Create /SC ONCE``.

    schtasks lifecycle:
      * Install: ``schtasks /Create /SC ONCE /TN <id> /TR <argv> /SD
        yyyy/MM/dd /ST HH:mm /F`` (the ``/F`` forces overwrite if a stale
        task with the same name still exists).
      * Cancel: ``schtasks /Delete /TN <id> /F`` -- exit non-zero if the
        task is absent, which we swallow (cancel is idempotent).
      * Self-delete: schtasks ONCE entries are NOT auto-removed; the
        runner takes care of that by calling :func:`mark_fired` which
        deletes the task. (We keep the historical sqlite row for audit.)
    """
    name: str = "schtasks"

    def install(self, task_id: str, fire_at_iso: str,
                 command_argv: list[str]) -> str:
        fire_at = quota_probe.parse_iso(fire_at_iso)
        # schtasks insists on /SD = MM/dd/yyyy on US-English locales and
        # /SD = yyyy/MM/dd elsewhere; the documented neutral format is
        # ISO yyyy/MM/dd which schtasks accepts on all supported locales.
        sd = fire_at.strftime("%Y/%m/%d")
        st = fire_at.strftime("%H:%M")
        tr = subprocess.list2cmdline(command_argv)
        argv = [
            "schtasks", "/Create",
            "/SC", "ONCE",
            "/TN", task_id,
            "/TR", tr,
            "/SD", sd,
            "/ST", st,
            "/F",
        ]
        log.info("schtasks install: %s", argv)
        proc = subprocess.run(argv, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"schtasks install failed ({proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        return task_id

    def cancel(self, task_id: str) -> None:
        argv = ["schtasks", "/Delete", "/TN", task_id, "/F"]
        log.info("schtasks cancel: %s", argv)
        proc = subprocess.run(argv, capture_output=True, text=True)
        if proc.returncode != 0:
            # 'task does not exist' is fine -- cancel is idempotent.
            msg = (proc.stderr.strip() or proc.stdout.strip()).lower()
            if "cannot find" in msg or "does not exist" in msg:
                return
            log.warning("schtasks /Delete returned %d: %s",
                         proc.returncode, msg)


@dataclass
class FileMarkerScheduler:
    """File-marker backend used in tests AND on POSIX.

    Writes ``<marker_dir>/<task_id>.json`` containing the fire_at + argv.
    A separate poller (the operator's choice -- ``at``, a tiny cron, an
    in-process timer) picks up these markers and runs them. The registry
    treats them as the source of truth for "is there a resume pending."
    """
    name: str = "file-marker"
    marker_dir: Path = DEFAULT_MARKER_DIR

    def install(self, task_id: str, fire_at_iso: str,
                 command_argv: list[str]) -> str:
        self.marker_dir.mkdir(parents=True, exist_ok=True)
        marker = self.marker_dir / f"{task_id}.json"
        marker.write_text(
            json.dumps({
                "task_id": task_id,
                "fire_at_ts": fire_at_iso,
                "command_argv": list(command_argv),
                "installed_at": _now_iso(),
            }, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return str(marker)

    def cancel(self, task_id: str) -> None:
        marker = self.marker_dir / f"{task_id}.json"
        try:
            marker.unlink()
        except FileNotFoundError:
            return


def default_scheduler() -> Scheduler:
    """Pick the right scheduler for the running platform."""
    if sys.platform == "win32":
        return WindowsSchTasksScheduler()
    return FileMarkerScheduler()


def task_id_for(entry_name: str) -> str:
    """Stable OS-task id for ``entry_name``. Lower-cased + hyphen-safe so
    schtasks accepts it on every locale."""
    safe = entry_name.replace(" ", "-").replace("_", "-").lower()
    return f"{SCHTASKS_PREFIX}{safe}"


# ---------------------------------------------------------------------------
# sqlite plumbing
# ---------------------------------------------------------------------------

def _ensure_schema(db_path: Path | str) -> None:
    p = Path(db_path)
    _state.ensure_schema(p)
    with sqlite3.connect(str(p)) as conn:
        conn.executescript(_SCHEMA_SQL)
        conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def list_pending(db_path: Path | str,
                  *, status: str | Iterable[str] | None = STATUS_PENDING
                  ) -> list[dict[str, Any]]:
    """Return all rows matching ``status`` (string, iterable, or None
    for 'all rows'), ordered by fire_at_ts ascending."""
    db_path = Path(db_path)
    _ensure_schema(db_path)
    if status is None:
        where, params = "", ()
    elif isinstance(status, str):
        where, params = "WHERE status = ?", (status,)
    else:
        statuses = tuple(status)
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        where = f"WHERE status IN ({placeholders})"
        params = statuses
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT entry_name, fire_at_ts, reset_source_ts, reset_source,
                       buffer_seconds, scheduled_at, schedule_id, backend,
                       status, last_fired_at, notes
                FROM smart_resume_pending
                {where}
                ORDER BY fire_at_ts ASC""",
            params,
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_pending(db_path: Path | str, entry_name: str) -> dict[str, Any] | None:
    db_path = Path(db_path)
    _ensure_schema(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        r = conn.execute(
            """SELECT entry_name, fire_at_ts, reset_source_ts, reset_source,
                      buffer_seconds, scheduled_at, schedule_id, backend,
                      status, last_fired_at, notes
               FROM smart_resume_pending WHERE entry_name=?""",
            (entry_name,),
        ).fetchone()
    return _row_to_dict(r) if r is not None else None


def _upsert_pending(db_path: Path, *, entry_name: str, fire_at_ts: str,
                     reset_source_ts: str, reset_source: str,
                     buffer_seconds: int, schedule_id: str,
                     backend: str, notes: str = "") -> None:
    now = _now_iso()
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """INSERT INTO smart_resume_pending
                (entry_name, fire_at_ts, reset_source_ts, reset_source,
                 buffer_seconds, scheduled_at, schedule_id, backend,
                 status, last_fired_at, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
               ON CONFLICT(entry_name) DO UPDATE SET
                 fire_at_ts=excluded.fire_at_ts,
                 reset_source_ts=excluded.reset_source_ts,
                 reset_source=excluded.reset_source,
                 buffer_seconds=excluded.buffer_seconds,
                 scheduled_at=excluded.scheduled_at,
                 schedule_id=excluded.schedule_id,
                 backend=excluded.backend,
                 status='pending',
                 notes=excluded.notes""",
            (entry_name, fire_at_ts, reset_source_ts, reset_source,
             buffer_seconds, now, schedule_id, backend, STATUS_PENDING, notes),
        )
        conn.commit()


def _set_status(db_path: Path, entry_name: str, status: str, *,
                 last_fired_at: str | None = None) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """UPDATE smart_resume_pending
               SET status=?, last_fired_at=COALESCE(?, last_fired_at)
               WHERE entry_name=?""",
            (status, last_fired_at, entry_name),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

@dataclass
class ScheduleResult:
    entry_name: str
    fire_at_ts: str
    reset_source_ts: str
    reset_source: str
    buffer_seconds: int
    schedule_id: str
    backend: str
    rescheduled: bool   # True if we cancelled a prior pending entry
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_name": self.entry_name,
            "fire_at_ts": self.fire_at_ts,
            "reset_source_ts": self.reset_source_ts,
            "reset_source": self.reset_source,
            "buffer_seconds": self.buffer_seconds,
            "schedule_id": self.schedule_id,
            "backend": self.backend,
            "rescheduled": self.rescheduled,
            "notes": self.notes,
        }


def _runner_argv(entry_name: str) -> list[str]:
    """The one-shot command the OS scheduler should fire. Surfaces
    ``runner.py --entry <name>`` so the standard cron_runs / advisory
    lock / escalation pipeline picks up the fire."""
    return [
        sys.executable,
        str(ROOT / "runner.py"),
        "--entry", entry_name,
    ]


def schedule_resume(entry_name: str, fire_at_ts: str, *,
                     reset_source_ts: str,
                     reset_source: str,
                     buffer_seconds: int = DEFAULT_BUFFER_SECONDS,
                     command_argv: list[str] | None = None,
                     db_path: Path | str = DEFAULT_DB_PATH,
                     scheduler: Scheduler | None = None,
                     notes: str = "") -> ScheduleResult:
    """Install (or replace) a one-shot resume timer for ``entry_name``.

    Idempotency contract: if a prior pending row exists, its OS task
    is cancelled FIRST so two one-shots never coexist for the same
    entry. The replacement is recorded with status='pending'.
    """
    db_path = Path(db_path)
    _ensure_schema(db_path)
    sched = scheduler or default_scheduler()
    argv = command_argv or _runner_argv(entry_name)
    task_id = task_id_for(entry_name)

    prior = get_pending(db_path, entry_name)
    if prior and prior.get("status") == STATUS_PENDING:
        try:
            sched.cancel(prior["schedule_id"])
        except Exception as exc:  # pragma: no cover -- defensive
            log.warning("cancel(prior) raised: %s", exc)
        _set_status(db_path, entry_name, STATUS_CANCELLED)

    schedule_id = sched.install(task_id, fire_at_ts, argv)
    _upsert_pending(
        db_path,
        entry_name=entry_name,
        fire_at_ts=fire_at_ts,
        reset_source_ts=reset_source_ts,
        reset_source=reset_source,
        buffer_seconds=buffer_seconds,
        schedule_id=schedule_id,
        backend=sched.name,
        notes=notes,
    )
    return ScheduleResult(
        entry_name=entry_name,
        fire_at_ts=fire_at_ts,
        reset_source_ts=reset_source_ts,
        reset_source=reset_source,
        buffer_seconds=buffer_seconds,
        schedule_id=schedule_id,
        backend=sched.name,
        rescheduled=bool(prior and prior.get("status") == STATUS_PENDING),
        notes=notes,
    )


def cancel_resume(entry_name: str, *,
                   db_path: Path | str = DEFAULT_DB_PATH,
                   scheduler: Scheduler | None = None) -> bool:
    """Cancel any pending one-shot for ``entry_name``. Returns True if
    something was cancelled, False if no row existed. Idempotent."""
    db_path = Path(db_path)
    _ensure_schema(db_path)
    row = get_pending(db_path, entry_name)
    if not row:
        return False
    sched = scheduler or default_scheduler()
    try:
        sched.cancel(row["schedule_id"])
    except Exception as exc:  # pragma: no cover -- defensive
        log.warning("cancel raised: %s", exc)
    _set_status(db_path, entry_name, STATUS_CANCELLED)
    return True


def mark_fired(entry_name: str, *,
                db_path: Path | str = DEFAULT_DB_PATH,
                scheduler: Scheduler | None = None,
                cleanup_os_task: bool = True) -> None:
    """Record that the one-shot fired. By default ALSO cancels the OS
    task (Windows schtasks ONCE entries don't auto-remove themselves --
    leaving stale entries clutters Task Scheduler).
    """
    db_path = Path(db_path)
    _ensure_schema(db_path)
    _set_status(db_path, entry_name, STATUS_FIRED, last_fired_at=_now_iso())
    if cleanup_os_task:
        row = get_pending(db_path, entry_name)
        if row:
            sched = scheduler or default_scheduler()
            try:
                sched.cancel(row["schedule_id"])
            except Exception as exc:  # pragma: no cover -- defensive
                log.warning("post-fire cleanup raised: %s", exc)


def apply_from_quota_state(entry_name: str, *,
                            state_path: Path | str | None = None,
                            db_path: Path | str | None = None,
                            buffer_seconds: int = DEFAULT_BUFFER_SECONDS,
                            scheduler: Scheduler | None = None,
                            command_argv: list[str] | None = None,
                            now: datetime | None = None,
                            ) -> ScheduleResult:
    """End-to-end: read the latest quota probe state, compute fire_at,
    schedule the one-shot. Raises :class:`quota_probe.QuotaProbeError`
    if no probe has been written yet.

    ``state_path`` / ``db_path`` resolve at call time from the module
    globals (NOT default-arg-bound) so tests + dashboards can
    monkeypatch them ahead of an endpoint hit.
    """
    if state_path is None:
        state_path = DEFAULT_QUOTA_STATE_PATH
    if db_path is None:
        db_path = DEFAULT_DB_PATH
    state = quota_probe.read_state(state_path)
    if state is None or not state.next_reset_ts:
        raise quota_probe.QuotaProbeError(
            f"no quota probe state at {state_path!s}; call "
            "quota_probe.probe_quota() first (e.g. via "
            "POST /api/registry/loop_continuous/probe_quota)"
        )
    fire_at = compute_fire_at(
        state.next_reset_ts,
        buffer_seconds=buffer_seconds,
        now=now,
    )
    return schedule_resume(
        entry_name,
        fire_at,
        reset_source_ts=state.next_reset_ts,
        reset_source=state.source,
        buffer_seconds=buffer_seconds,
        command_argv=command_argv,
        db_path=db_path,
        scheduler=scheduler,
        notes=(f"buffer={buffer_seconds}s; source={state.source}; "
                f"checked_at={state.checked_at}"),
    )


def on_session_start(entry_name: str, *,
                      api_key: str | None = None,
                      operator_path: Path | str = quota_probe.DEFAULT_OPERATOR_PATH,
                      state_path: Path | str = DEFAULT_QUOTA_STATE_PATH,
                      db_path: Path | str = DEFAULT_DB_PATH,
                      buffer_seconds: int = DEFAULT_BUFFER_SECONDS,
                      scheduler: Scheduler | None = None,
                      command_argv: list[str] | None = None,
                      opener: Callable | None = None,
                      now: datetime | None = None,
                      ) -> ScheduleResult:
    """Full smart-resume cycle for one entry. Called from a Stop hook
    (or first response handler): re-probe the quota, compute fire_at,
    reschedule. Replaces any pending one-shot for the same entry.

    This is the loop step (5) of the AR-S3k spec: "On the next
    session's first response, read the new headers + reschedule."
    """
    quota_probe.probe_quota(
        api_key=api_key,
        operator_path=operator_path,
        state_path=state_path,
        opener=opener,
    )
    return apply_from_quota_state(
        entry_name,
        state_path=state_path,
        db_path=db_path,
        buffer_seconds=buffer_seconds,
        scheduler=scheduler,
        command_argv=command_argv,
        now=now,
    )


# ---------------------------------------------------------------------------
# Operator helper -- "what's the registry waiting on right now?"
# ---------------------------------------------------------------------------

@dataclass
class StatusSummary:
    """Compact snapshot the dashboard renders next to each entry."""
    entry_name: str
    has_pending: bool
    fire_at_ts: str | None
    seconds_until_fire: int | None
    reset_source: str | None
    backend: str | None
    last_fired_at: str | None
    status: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_name": self.entry_name,
            "has_pending": self.has_pending,
            "fire_at_ts": self.fire_at_ts,
            "seconds_until_fire": self.seconds_until_fire,
            "reset_source": self.reset_source,
            "backend": self.backend,
            "last_fired_at": self.last_fired_at,
            "status": self.status,
        }


def status(entry_name: str, *,
            db_path: Path | str = DEFAULT_DB_PATH,
            now: datetime | None = None) -> StatusSummary:
    row = get_pending(db_path, entry_name)
    if not row:
        return StatusSummary(
            entry_name=entry_name,
            has_pending=False,
            fire_at_ts=None,
            seconds_until_fire=None,
            reset_source=None,
            backend=None,
            last_fired_at=None,
            status=None,
        )
    now = now or _now_utc()
    try:
        fire_at = quota_probe.parse_iso(row["fire_at_ts"])
        delta = int((fire_at - now).total_seconds())
    except (ValueError, KeyError):
        delta = None
    return StatusSummary(
        entry_name=entry_name,
        has_pending=row.get("status") == STATUS_PENDING,
        fire_at_ts=row.get("fire_at_ts"),
        seconds_until_fire=delta,
        reset_source=row.get("reset_source"),
        backend=row.get("backend"),
        last_fired_at=row.get("last_fired_at"),
        status=row.get("status"),
    )
