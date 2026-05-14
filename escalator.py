"""Failure escalation pipeline (AR-S3d).

Reads ``cron_runs`` rows with status=``failed`` that have not yet been
escalated, looks up the parent entry's ``escalation`` block, and acts:

  * ``on_failure: file_pd_task``  -- POST a task to PD. Title prefix is
    ``[cron-fail] <entry_name>``. If an open task (status todo or
    in_progress) already exists with that prefix, append a new failure
    note to the existing task's description (idempotent dedup --
    closed historical tasks do NOT dedupe future failures).

  * ``on_failure: discord_only`` -- log + return (Discord wiring is a
    follow-on; the schema honours the choice without filing PD).

  * ``on_failure: log_only``      -- log only.

Channels (``escalation.channel``) currently only matter for the
discord path; the file_pd_task path doesn't gate on channel.

Each successfully-handled row gets ``escalated_at`` set so the next
poll skips it. If PD is unreachable (or the POST fails), the row
stays un-escalated and gets retried on the next cycle.

CLI:

    python escalator.py --once
    python escalator.py --loop --interval 60

The runner (AR-S3c) intentionally does NOT call into this module -- it
just records failures. The escalator runs on its own schedule (manual
trigger, registry cron entry, or app.py endpoint).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import schema as _schema
import state as _state


log = logging.getLogger("automation-registry.escalator")

ROOT = Path(__file__).resolve().parent
REGISTRY_YAML = ROOT / "automations.yaml"
REGISTRY_DB = ROOT / "data" / "registry.db"

PD_URL = os.environ.get("PD_URL", "http://127.0.0.1:5100")
TITLE_PREFIX = "[cron-fail]"
NOTE_DIVIDER = "\n\n---\n"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Schema extension -- idempotent ALTER for escalated_at
# ---------------------------------------------------------------------------

def _ensure_escalated_at_column(db_path: Path) -> None:
    """Add cron_runs.escalated_at if not present. Idempotent ALTER --
    safe to call on a DB created by state.ensure_schema or after a
    previous escalator pass."""
    _state.ensure_schema(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(cron_runs)")}
        if "escalated_at" not in cols:
            conn.execute("ALTER TABLE cron_runs ADD COLUMN "
                          "escalated_at TEXT")
        if "status" not in cols:
            # Runner adds these too; safe to be defensive.
            conn.execute("ALTER TABLE cron_runs ADD COLUMN status TEXT "
                          "NOT NULL DEFAULT 'running'")
        if "source" not in cols:
            conn.execute("ALTER TABLE cron_runs ADD COLUMN source TEXT "
                          "NOT NULL DEFAULT 'schedule'")
        conn.commit()


# ---------------------------------------------------------------------------
# PD client (stdlib only -- no requests dep)
# ---------------------------------------------------------------------------

@dataclass
class PdClient:
    """Thin REST wrapper. Tests inject their own client class with the
    same surface; production uses this stdlib one."""
    base_url: str = PD_URL
    timeout: float = 5.0

    def list_open_tasks(self, project_id: str) -> list[dict[str, Any]]:
        """Return tasks with status todo or in_progress."""
        url = f"{self.base_url}/api/projects/{project_id}/tasks"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                data = json.loads(r.read())
        except (urllib.error.URLError, urllib.error.HTTPError,
                ConnectionError, TimeoutError, OSError) as exc:
            raise PdUnreachable(str(exc)) from exc
        tasks = data.get("tasks") or data  # endpoint shape varies
        if not isinstance(tasks, list):
            tasks = tasks.get("tasks") if isinstance(tasks, dict) else []
        return [t for t in tasks
                if isinstance(t, dict)
                and t.get("status") in ("todo", "in_progress")]

    def create_task(self, project_id: str, *, title: str,
                     description: str, priority: str = "high",
                     category: str = "bug") -> dict[str, Any]:
        url = f"{self.base_url}/api/projects/{project_id}/tasks"
        body = json.dumps({
            "title": title,
            "description": description,
            "priority": priority,
            "category": category,
        }).encode("utf-8")
        return self._post(url, body)

    def update_task(self, project_id: str, task_id: str, *,
                     description: str) -> dict[str, Any]:
        url = f"{self.base_url}/api/projects/{project_id}/tasks/{task_id}"
        body = json.dumps({"description": description}).encode("utf-8")
        return self._post(url, body)

    def _post(self, url: str, body: bytes) -> dict[str, Any]:
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, urllib.error.HTTPError,
                ConnectionError, TimeoutError, OSError) as exc:
            raise PdUnreachable(str(exc)) from exc


class PdUnreachable(RuntimeError):
    """Raised when PD's HTTP surface can't be reached. Caller should
    log + skip + leave the row un-escalated for retry."""


# ---------------------------------------------------------------------------
# Title + description builders
# ---------------------------------------------------------------------------

def _title_for(entry_name: str) -> str:
    return f"{TITLE_PREFIX} {entry_name}"


def _description_for(*, entry_name: str, schedule: str, run_id: int,
                      started_at: str, exit_code: int | None,
                      stderr_excerpt: str) -> str:
    return (
        f"WHAT: registry cron entry '{entry_name}' failed on schedule "
        f"'{schedule or '<unknown>'}'\n\n"
        f"WHY: AR-S3d escalation pipeline -- on_failure: file_pd_task.\n\n"
        f"FAILURE DETAIL:\n"
        f"- run_id: {run_id}\n"
        f"- started_at: {started_at}\n"
        f"- exit_code: {exit_code}\n"
        f"- stderr excerpt:\n"
        f"```\n{(stderr_excerpt or '').strip()[:4000]}\n```\n\n"
        f"DONE WHEN: a fresh successful run lands "
        f"(cron_runs.status=succeeded for this entry)."
    )


def _append_note(existing_description: str, *, run_id: int,
                  started_at: str, exit_code: int | None,
                  stderr_excerpt: str) -> str:
    """Format an append-note for the dedup path."""
    note = (
        f"Re-fire {_now()} -- "
        f"run_id={run_id}, started_at={started_at}, "
        f"exit_code={exit_code}, "
        f"stderr={(stderr_excerpt or '').strip()[:200]!r}"
    )
    return existing_description.rstrip() + NOTE_DIVIDER + note + "\n"


# ---------------------------------------------------------------------------
# Core: process_failed_runs
# ---------------------------------------------------------------------------

@dataclass
class EscalationResult:
    filed: list[str]         # entry names with newly filed PD tasks
    appended: list[str]      # entry names where existing task got a note
    discord: list[str]       # discord_only handled (logged)
    log_only: list[str]      # log_only handled
    skipped_pd_unreachable: list[str]  # rows left un-escalated for retry
    skipped_no_entry: list[str]        # cron_run with no matching YAML

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "filed": list(self.filed),
            "appended": list(self.appended),
            "discord": list(self.discord),
            "log_only": list(self.log_only),
            "skipped_pd_unreachable": list(self.skipped_pd_unreachable),
            "skipped_no_entry": list(self.skipped_no_entry),
        }


def _fetch_unescalated_failures(db_path: Path, limit: int | None
                                  ) -> list[sqlite3.Row]:
    sql = (
        "SELECT * FROM cron_runs "
        "WHERE status='failed' AND escalated_at IS NULL "
        "ORDER BY started_at ASC"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return list(conn.execute(sql))


def _mark_escalated(db_path: Path, run_id: int) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE cron_runs SET escalated_at = ? WHERE run_id = ?",
            (_now(), run_id),
        )
        conn.commit()


def process_failed_runs(
    *,
    yaml_path: Path = REGISTRY_YAML,
    db_path: Path = REGISTRY_DB,
    pd_client: PdClient | None = None,
    limit: int | None = None,
) -> EscalationResult:
    """Process all un-escalated failed runs. Idempotent."""
    _ensure_escalated_at_column(db_path)
    client = pd_client or PdClient()
    automations = {a.name: a for a in _schema.load_automations(yaml_path)}
    rows = _fetch_unescalated_failures(db_path, limit)

    result = EscalationResult([], [], [], [], [], [])
    for row in rows:
        entry_name = row["entry_name"]
        entry = automations.get(entry_name)
        if entry is None:
            log.warning(
                "cron_run %d: entry %r no longer in YAML -- marking "
                "escalated to stop the loop", row["run_id"], entry_name,
            )
            _mark_escalated(db_path, row["run_id"])
            result.skipped_no_entry.append(entry_name)
            continue

        esc = entry.escalation
        try:
            if esc.on_failure == "file_pd_task":
                _handle_file_pd_task(client, entry, row, result, db_path)
            elif esc.on_failure == "discord_only":
                log.info("cron_run %d (%s): discord_only escalation "
                          "(channel=%s) -- Discord wiring deferred",
                          row["run_id"], entry_name, esc.channel)
                _mark_escalated(db_path, row["run_id"])
                result.discord.append(entry_name)
            else:  # log_only
                log.info("cron_run %d (%s): log_only escalation",
                          row["run_id"], entry_name)
                _mark_escalated(db_path, row["run_id"])
                result.log_only.append(entry_name)
        except PdUnreachable as exc:
            log.warning(
                "cron_run %d (%s): PD unreachable (%s) -- leaving "
                "un-escalated for next cycle",
                row["run_id"], entry_name, exc,
            )
            result.skipped_pd_unreachable.append(entry_name)

    return result


def _handle_file_pd_task(client: PdClient, entry: _schema.Automation,
                          row: sqlite3.Row, result: EscalationResult,
                          db_path: Path) -> None:
    title = _title_for(entry.name)
    pd_project = entry.escalation.pd_project or entry.owner_project
    open_tasks = client.list_open_tasks(pd_project)
    match = next(
        (t for t in open_tasks
         if isinstance(t.get("title"), str)
         and t["title"].startswith(title)),
        None,
    )

    if match is None:
        client.create_task(
            pd_project,
            title=title,
            description=_description_for(
                entry_name=entry.name,
                schedule=entry.schedule or "",
                run_id=row["run_id"],
                started_at=row["started_at"],
                exit_code=row["exit_code"],
                stderr_excerpt=row["stderr_excerpt"] or "",
            ),
            priority="high",
            category="bug",
        )
        result.filed.append(entry.name)
    else:
        client.update_task(
            pd_project, match["id"],
            description=_append_note(
                match.get("description", "") or "",
                run_id=row["run_id"],
                started_at=row["started_at"],
                exit_code=row["exit_code"],
                stderr_excerpt=row["stderr_excerpt"] or "",
            ),
        )
        result.appended.append(entry.name)

    _mark_escalated(db_path, row["run_id"])


# ---------------------------------------------------------------------------
# CLI: --once / --loop
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="automation-registry-escalator",
        description="Process failed cron_runs and fire escalations.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true",
                      help="One escalation pass; exit.")
    mode.add_argument("--loop", action="store_true",
                      help="Keep running, polling on --interval.")
    parser.add_argument("--interval", type=int, default=60,
                        help="Loop poll interval in seconds (default 60).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max rows per pass (default: all).")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Resolve module-level constants at call time so monkeypatching works.
    mod = sys.modules[__name__]

    if args.once:
        out = process_failed_runs(
            yaml_path=mod.REGISTRY_YAML,
            db_path=mod.REGISTRY_DB,
            limit=args.limit,
        )
        print(json.dumps(out.as_dict(), indent=2))
        return 0

    # --loop
    while True:
        try:
            out = process_failed_runs(
                yaml_path=mod.REGISTRY_YAML,
                db_path=mod.REGISTRY_DB,
                limit=args.limit,
            )
            log.info("escalator pass: %s", out.as_dict())
        except Exception:
            log.exception("escalator pass failed; continuing loop")
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
