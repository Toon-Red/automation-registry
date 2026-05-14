"""SQLite state layer for the automation registry (AR-S3b).

Single database at ``data/registry.db`` (gitignored). Schema v1
covers what AR-S3b needs:

  ``cron_entries`` -- one row per installed cron-mechanism automation.
    Columns:
      name TEXT PRIMARY KEY
      schedule TEXT NOT NULL
      command  TEXT NOT NULL
      working_dir TEXT NOT NULL
      description TEXT
      enabled INTEGER NOT NULL DEFAULT 1
      last_install_ts TEXT NOT NULL
      last_modified_ts TEXT NOT NULL
      status TEXT NOT NULL DEFAULT 'installed'

  ``cron_runs`` -- run history. Created here so AR-S3c can populate it.
    Columns:
      run_id INTEGER PRIMARY KEY AUTOINCREMENT
      entry_name TEXT NOT NULL
      started_at TEXT NOT NULL
      ended_at TEXT
      exit_code INTEGER
      stdout_excerpt TEXT
      stderr_excerpt TEXT

Reconciliation diffs YAML-desired state against this table; everything
not in YAML gets uninstalled, new entries get installed, modified
entries get re-installed.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


@dataclass
class CronEntryRow:
    name: str
    schedule: str
    command: str
    working_dir: str
    description: str
    enabled: bool
    last_install_ts: str
    last_modified_ts: str
    status: str

    @property
    def install_fingerprint(self) -> tuple[str, str, str, str, bool]:
        """Tuple of fields that, when unchanged, mean no re-install is
        needed. Excludes timestamps + status."""
        return (self.schedule, self.command, self.working_dir,
                self.description, self.enabled)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cron_entries (
    name             TEXT PRIMARY KEY,
    schedule         TEXT NOT NULL,
    command          TEXT NOT NULL,
    working_dir      TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    enabled          INTEGER NOT NULL DEFAULT 1,
    last_install_ts  TEXT NOT NULL,
    last_modified_ts TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'installed'
);

CREATE TABLE IF NOT EXISTS cron_runs (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_name       TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    ended_at         TEXT,
    exit_code        INTEGER,
    stdout_excerpt   TEXT,
    stderr_excerpt   TEXT
);

CREATE INDEX IF NOT EXISTS idx_cron_runs_entry_started
    ON cron_runs (entry_name, started_at DESC);
"""


def ensure_schema(db_path: Path | str) -> None:
    """Create the database file (+ parent dir) if needed and apply
    the v1 schema. Idempotent."""
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _connect(p) as conn:
        conn.executescript(_SCHEMA_SQL)


@contextmanager
def _connect(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_entries(db_path: Path | str) -> list[CronEntryRow]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM cron_entries ORDER BY name"
        ).fetchall()
    return [_row_to_entry(r) for r in rows]


def get_entry(db_path: Path | str, name: str) -> CronEntryRow | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM cron_entries WHERE name = ?", (name,)
        ).fetchone()
    return _row_to_entry(row) if row else None


def upsert_entry(db_path: Path | str, *, name: str, schedule: str,
                  command: str, working_dir: str, description: str,
                  enabled: bool) -> None:
    now = _now()
    with _connect(db_path) as conn:
        existing = conn.execute(
            "SELECT 1 FROM cron_entries WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE cron_entries
                   SET schedule=?, command=?, working_dir=?, description=?,
                       enabled=?, last_install_ts=?, last_modified_ts=?,
                       status='installed'
                   WHERE name=?""",
                (schedule, command, working_dir, description,
                 int(enabled), now, now, name),
            )
        else:
            conn.execute(
                """INSERT INTO cron_entries
                   (name, schedule, command, working_dir, description,
                    enabled, last_install_ts, last_modified_ts, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'installed')""",
                (name, schedule, command, working_dir, description,
                 int(enabled), now, now),
            )


def delete_entry(db_path: Path | str, name: str) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM cron_entries WHERE name = ?", (name,))


def _row_to_entry(row: sqlite3.Row) -> CronEntryRow:
    return CronEntryRow(
        name=row["name"],
        schedule=row["schedule"],
        command=row["command"],
        working_dir=row["working_dir"],
        description=row["description"] or "",
        enabled=bool(row["enabled"]),
        last_install_ts=row["last_install_ts"],
        last_modified_ts=row["last_modified_ts"],
        status=row["status"],
    )
