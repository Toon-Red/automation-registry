"""claude_routine mechanism backend (AR-S3i -- DEFERRED STUB).

Tier 2 (cloud) mechanism. Routines run inside an Anthropic-cloud-side
session via the `schedule` skill / Routines API. They survive the
local machine being off and are the right home for cross-machine /
connector-driven workflows (GitHub events, Slack hooks, research
offload).

> *Status:* SHIPPED AS STUB. AR-S3i ships the spec + validator path +
> a placeholder reconcile so the surface is callable today, but no
> Routines API client is wired in. The :func:`reconcile` pass walks
> every ``claude_routine`` entry in the YAML and emits a
> :class:`PendingRoutineOp` describing what an operator (or, once a
> real cloud-side use case is filed, the activated backend) would
> need to do via the ``schedule`` skill. No cloud writes happen here.

When the first cloud-side use case lands (Q-PRESTON in
``docs/proposals/AR-S3-claude-scheduled-evaluation.md``), the
activation work is:

  1. Inject a Routines client (signature already shaped --
     ``routines_client`` kwarg on :func:`reconcile`).
  2. Replace the ``_DEFERRED_CLIENT`` placeholder with the real
     ``schedule`` skill / Routines REST surface.
  3. Add the Pro 5/day / Max 15/day daily-fire cap check before each
     install -- the cap is a property of the user's plan, not the
     entry, so we'll need an env knob.
  4. Idempotent fingerprint matching the cron / loop_continuous
     handlers (same pattern as AR-S3b).

Until then the registry is honest: it reads the YAML, validates, and
returns a deferred status so the API caller doesn't think the routine
is live.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import schema as _schema
import state as _state

log = logging.getLogger("automation-registry.routine")

ROOT = Path(__file__).resolve().parent

# Subscription daily-fire cap. Defaults capture the Pro / Max ceilings
# from the v2 schema doc; the activated backend will read this from
# config + env. Kept as module constants so the validator path can
# reference them today.
DEFAULT_PRO_DAILY_CAP = 5
DEFAULT_MAX_DAILY_CAP = 15

STATUS_DEFERRED = "deferred"
STATUS_INSTALLED = "installed"
STATUS_DISABLED = "disabled"


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS routine_entries (
    name              TEXT PRIMARY KEY,
    schedule          TEXT NOT NULL,
    prompt            TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    enabled           INTEGER NOT NULL DEFAULT 1,
    status            TEXT NOT NULL DEFAULT 'deferred',
    routine_id        TEXT,            -- cloud-side id (populated when activated)
    fingerprint       TEXT NOT NULL,
    last_synced_ts    TEXT NOT NULL
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
# Routines client protocol (placeholder)
# ---------------------------------------------------------------------------

class RoutinesClient(Protocol):
    """Shape of the Anthropic Routines API surface the activated backend
    will consume. Methods mirror the ``schedule`` skill's CronCreate /
    CronList / CronDelete trio.

    AR-S3i ships only the protocol -- no concrete implementation. When
    Q-PRESTON closes with a routine-shaped use case, drop in a real
    client that wraps the schedule skill / REST endpoint and pass it
    to :func:`reconcile` via ``routines_client``.
    """

    def create_routine(self, *, name: str, schedule: str, prompt: str,
                        description: str) -> str:
        """Create a cloud routine. Returns the cloud-side routine id."""
        ...

    def update_routine(self, *, routine_id: str, schedule: str,
                        prompt: str, description: str) -> None:
        ...

    def delete_routine(self, *, routine_id: str) -> None:
        ...


class _DeferredClient:
    """Sentinel client used when no real Routines client is injected.

    Every call raises :class:`RoutineNotActivated` so the reconcile pass
    can't accidentally pretend it wrote to the cloud. The reconcile
    loop catches it and records the entry as ``deferred`` instead.
    """

    def create_routine(self, **_kw: Any) -> str:
        raise RoutineNotActivated()

    def update_routine(self, **_kw: Any) -> None:
        raise RoutineNotActivated()

    def delete_routine(self, **_kw: Any) -> None:
        raise RoutineNotActivated()


_DEFERRED_CLIENT = _DeferredClient()


class RoutineNotActivated(RuntimeError):
    """Raised by :class:`_DeferredClient` to signal that no real
    Routines client is wired -- AR-S3i is intentionally a stub until
    Q-PRESTON closes."""

    def __init__(self, msg: str | None = None) -> None:
        super().__init__(
            msg
            or "claude_routine backend is a deferred stub (AR-S3i). "
            "Pass a concrete routines_client= to reconcile() when "
            "activating."
        )


# ---------------------------------------------------------------------------
# Plan-side dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PendingRoutineOp:
    """An operation that a real :class:`RoutinesClient` would execute.

    Returned from :func:`reconcile` whenever no real client is wired.
    The shape matches the create/update/disable surface so an operator
    can apply it manually via the ``schedule`` skill in the meantime.
    """

    action: str          # 'create' | 'update' | 'disable'
    name: str
    schedule: str | None = None
    prompt: str | None = None
    description: str | None = None
    reason: str = "deferred (AR-S3i stub)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "name": self.name,
            "schedule": self.schedule,
            "prompt": self.prompt,
            "description": self.description,
            "reason": self.reason,
        }


@dataclass
class ReconcileResult:
    installed: list[str] = field(default_factory=list)
    reinstalled: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    # Entries the registry has SEEN + validated but cannot install
    # because no Routines client is wired (the AR-S3i deferred path).
    deferred: list[str] = field(default_factory=list)
    pending_ops: list[PendingRoutineOp] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "installed": list(self.installed),
            "reinstalled": list(self.reinstalled),
            "unchanged": list(self.unchanged),
            "disabled": list(self.disabled),
            "skipped": list(self.skipped),
            "deferred": list(self.deferred),
            "pending_ops": [op.to_dict() for op in self.pending_ops],
        }


# ---------------------------------------------------------------------------
# Fingerprint + sqlite plumbing
# ---------------------------------------------------------------------------

def _fingerprint(entry: _schema.Automation) -> str:
    parts = [
        entry.mechanism,
        entry.schedule or "",
        entry.target,
        entry.target_kind,
        entry.description,
        str(entry.enabled),
    ]
    return "|".join(parts)


def _routine_prompt(entry: _schema.Automation) -> str:
    """Body of the cloud routine. For ``target_kind: claude_prompt`` the
    target IS the prompt body (slash-command or free-form text); the
    activated backend forwards it verbatim to the routine's session."""
    return entry.target


def _upsert_entry(db_path: Path, entry: _schema.Automation, *,
                   fingerprint: str, status: str,
                   routine_id: str | None) -> None:
    now = _now()
    with sqlite3.connect(str(db_path)) as conn:
        existing = conn.execute(
            "SELECT 1 FROM routine_entries WHERE name=?", (entry.name,),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE routine_entries
                   SET schedule=?, prompt=?, description=?, enabled=?,
                       status=?, routine_id=COALESCE(?, routine_id),
                       fingerprint=?, last_synced_ts=?
                   WHERE name=?""",
                (entry.schedule, _routine_prompt(entry), entry.description,
                 int(entry.enabled), status, routine_id,
                 fingerprint, now, entry.name),
            )
        else:
            conn.execute(
                """INSERT INTO routine_entries
                   (name, schedule, prompt, description, enabled, status,
                    routine_id, fingerprint, last_synced_ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (entry.name, entry.schedule, _routine_prompt(entry),
                 entry.description, int(entry.enabled), status,
                 routine_id, fingerprint, now),
            )
        conn.commit()


def _delete_entry(db_path: Path, name: str) -> str | None:
    """Remove the sqlite row for ``name``; return its routine_id (if
    any) so the caller can also delete the cloud-side routine."""
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT routine_id FROM routine_entries WHERE name=?", (name,),
        ).fetchone()
        conn.execute("DELETE FROM routine_entries WHERE name=?", (name,))
        conn.commit()
    return row[0] if row else None


def _set_status(db_path: Path, name: str, status: str) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE routine_entries SET status=?, last_synced_ts=? "
            "WHERE name=?", (status, _now(), name),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------

def reconcile(
    yaml_path: Path | str,
    db_path: Path | str,
    *,
    routines_client: RoutinesClient | None = None,
) -> ReconcileResult:
    """Catalog-side reconcile for ``mechanism: claude_routine`` entries.

    Behaviour:

      * No ``routines_client`` passed -> uses the deferred sentinel.
        Every desired entry lands in ``result.deferred`` with a matching
        :class:`PendingRoutineOp` so an operator can apply it manually
        via the ``schedule`` skill until the backend activates. sqlite
        records the entry with status ``deferred`` so subsequent
        reconciles can detect drift.

      * Real ``routines_client`` passed -> the reconcile loop calls
        create / update / delete and records the cloud-side routine id.
        This path is dormant until Q-PRESTON closes with a use case
        and the client is wired; the surface is here so activation is
        a one-line change in app.py.

    Idempotent under both paths: re-running with the same YAML against
    the same sqlite changes nothing.
    """
    db_path = Path(db_path)
    _ensure_schema(db_path)
    client = routines_client or _DEFERRED_CLIENT
    is_deferred = client is _DEFERRED_CLIENT

    automations = _schema.load_automations(yaml_path)
    relevant = [a for a in automations if a.mechanism == "claude_routine"]
    desired_names = {a.name for a in relevant}

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        existing = {
            r["name"]: dict(r) for r in conn.execute(
                "SELECT * FROM routine_entries"
            )
        }

    result = ReconcileResult()

    for auto in relevant:
        fp = _fingerprint(auto)
        prior = existing.get(auto.name)

        if not auto.enabled:
            if prior:
                routine_id = prior.get("routine_id")
                if not is_deferred and routine_id:
                    try:
                        client.delete_routine(routine_id=routine_id)
                    except RoutineNotActivated:
                        pass
                _delete_entry(db_path, auto.name)
                result.disabled.append(auto.name)
                if is_deferred:
                    result.pending_ops.append(PendingRoutineOp(
                        action="disable", name=auto.name,
                    ))
            else:
                result.skipped.append(auto.name)
            continue

        # Unchanged: same fingerprint AND we successfully installed last
        # time (i.e. not stuck in deferred). Deferred entries stay
        # 'deferred' across reconciles -- they're not 'unchanged' in
        # the sense the operator cares about.
        if prior and prior.get("fingerprint") == fp:
            prior_status = prior.get("status")
            if prior_status == STATUS_INSTALLED:
                result.unchanged.append(auto.name)
                continue
            if prior_status == STATUS_DEFERRED and is_deferred:
                # Still deferred, still the same shape -- record and
                # move on without spamming a duplicate pending_op.
                result.deferred.append(auto.name)
                continue

        # Install or reinstall path.
        if is_deferred:
            _upsert_entry(db_path, auto, fingerprint=fp,
                           status=STATUS_DEFERRED, routine_id=None)
            result.deferred.append(auto.name)
            action = "update" if prior else "create"
            result.pending_ops.append(PendingRoutineOp(
                action=action,
                name=auto.name,
                schedule=auto.schedule,
                prompt=_routine_prompt(auto),
                description=auto.description,
            ))
            continue

        # Real-client path (dormant until Q-PRESTON closes).
        prior_routine_id = prior.get("routine_id") if prior else None
        try:
            if prior_routine_id:
                client.update_routine(
                    routine_id=prior_routine_id,
                    schedule=auto.schedule or "",
                    prompt=_routine_prompt(auto),
                    description=auto.description,
                )
                routine_id = prior_routine_id
                result.reinstalled.append(auto.name)
            else:
                routine_id = client.create_routine(
                    name=auto.name,
                    schedule=auto.schedule or "",
                    prompt=_routine_prompt(auto),
                    description=auto.description,
                )
                result.installed.append(auto.name)
        except RoutineNotActivated:
            # Client lied about being real -- fall back to deferred.
            _upsert_entry(db_path, auto, fingerprint=fp,
                           status=STATUS_DEFERRED, routine_id=None)
            result.deferred.append(auto.name)
            result.pending_ops.append(PendingRoutineOp(
                action=("update" if prior else "create"),
                name=auto.name,
                schedule=auto.schedule,
                prompt=_routine_prompt(auto),
                description=auto.description,
                reason="injected client raised RoutineNotActivated",
            ))
            continue

        _upsert_entry(db_path, auto, fingerprint=fp,
                       status=STATUS_INSTALLED, routine_id=routine_id)

    # Prune entries the YAML no longer mentions.
    for name in list(existing.keys()):
        if name not in desired_names:
            routine_id = existing[name].get("routine_id")
            if not is_deferred and routine_id:
                try:
                    client.delete_routine(routine_id=routine_id)
                except RoutineNotActivated:
                    pass
            _delete_entry(db_path, name)
            result.disabled.append(name)
            if is_deferred:
                result.pending_ops.append(PendingRoutineOp(
                    action="disable", name=name,
                ))

    return result


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def list_installed(db_path: Path | str) -> list[dict[str, Any]]:
    """Return the sqlite-backed view of all claude_routine entries the
    registry has seen (any status: installed | deferred | disabled).
    """
    db_path = Path(db_path)
    _ensure_schema(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT name, schedule, prompt, description, enabled,
                      status, routine_id, last_synced_ts
               FROM routine_entries ORDER BY name"""
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["enabled"] = bool(d["enabled"])
        out.append(d)
    return out


def get_entry(db_path: Path | str, name: str) -> dict[str, Any] | None:
    for r in list_installed(db_path):
        if r["name"] == name:
            return r
    return None
