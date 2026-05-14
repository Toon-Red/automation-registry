"""AR-S3d tests for escalator.py.

Fully mocked PD client -- no real REST calls. Covers:
  * Single failed run with file_pd_task -> task filed
  * Failed run with discord_only / log_only -> no PD call, escalated
  * Open task already exists -> append (no duplicate)
  * Existing task is `done` -> file fresh (closed history does not dedupe)
  * Multiple failed runs -> each processed once, escalated_at gates re-run
  * PD unreachable -> row stays un-escalated, retried next cycle
  * cron_run with no matching YAML entry -> marked escalated, no PD call
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import escalator
import state as _state


_BASE_YAML = """\
schema_version: 1
automations:
  - name: dream-eod
    description: EOD review.
    owner_project: dream
    target: orchestrator:run_eod
    target_kind: python_callable
    mechanism: cron
    schedule: "0 17 * * *"
    escalation:
      channel: discord
      on_failure: {on_failure}
      {pd_project}
    enabled: true
"""


def _yaml(tmp_path, on_failure="file_pd_task", pd_project=""):
    body = _BASE_YAML.format(
        on_failure=on_failure,
        pd_project=f"pd_project: {pd_project}" if pd_project else "",
    )
    p = tmp_path / "automations.yaml"
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "registry.db"
    escalator._ensure_escalated_at_column(p)
    return p


def _insert_failed_run(db_path: Path, *, entry_name: str = "dream-eod",
                        stderr: str = "boom",
                        started_at: str = "2026-05-12T17:00:00+00:00",
                        exit_code: int = 1) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO cron_runs
               (entry_name, started_at, ended_at, exit_code,
                stdout_excerpt, stderr_excerpt, status, source)
               VALUES (?, ?, ?, ?, ?, ?, 'failed', 'schedule')""",
            (entry_name, started_at, started_at, exit_code, "", stderr),
        )
        conn.commit()
        return cur.lastrowid


class _FakePD:
    """In-memory PD double. Records calls; satisfies escalator.PdClient
    surface."""
    def __init__(self, open_tasks: list[dict] | None = None,
                  raise_on=None):
        self._open = open_tasks or []
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.raise_on = raise_on or set()

    def list_open_tasks(self, project_id):
        if "list" in self.raise_on:
            raise escalator.PdUnreachable("simulated")
        return [t for t in self._open if t.get("project_id", project_id) == project_id]

    def create_task(self, project_id, *, title, description,
                     priority="high", category="bug"):
        if "create" in self.raise_on:
            raise escalator.PdUnreachable("simulated")
        rec = {"id": f"t{len(self.created)+1:04d}", "project_id": project_id,
               "title": title, "description": description,
               "priority": priority, "category": category,
               "status": "todo"}
        self.created.append(rec)
        # Real PD would surface the new task in subsequent list_open_tasks
        # calls; mirror that so dedup tests see realistic state.
        self._open.append(rec)
        return rec

    def update_task(self, project_id, task_id, *, description):
        self.updated.append({"project_id": project_id,
                              "task_id": task_id,
                              "description": description})
        return {"id": task_id, "description": description}


# ---------------------------------------------------------------------------
# Schema extension
# ---------------------------------------------------------------------------

class TestSchemaExtension:
    def test_escalated_at_column_added(self, tmp_path):
        db = tmp_path / "registry.db"
        _state.ensure_schema(db)
        escalator._ensure_escalated_at_column(db)
        with sqlite3.connect(str(db)) as conn:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(cron_runs)"
            )}
        assert "escalated_at" in cols

    def test_idempotent_alter(self, tmp_path):
        db = tmp_path / "registry.db"
        escalator._ensure_escalated_at_column(db)
        escalator._ensure_escalated_at_column(db)  # no raise


# ---------------------------------------------------------------------------
# file_pd_task path
# ---------------------------------------------------------------------------

class TestFilePdTask:
    def test_new_failure_files_task(self, tmp_path, db):
        yaml = _yaml(tmp_path)
        _insert_failed_run(db)
        pd = _FakePD(open_tasks=[])
        out = escalator.process_failed_runs(
            yaml_path=yaml, db_path=db, pd_client=pd,
        )
        assert out.filed == ["dream-eod"]
        assert out.appended == []
        assert len(pd.created) == 1
        # Title prefix matches.
        assert pd.created[0]["title"].startswith("[cron-fail] dream-eod")
        # pd_project defaults to owner_project.
        assert pd.created[0]["project_id"] == "dream"

    def test_existing_open_task_gets_appended(self, tmp_path, db):
        yaml = _yaml(tmp_path)
        _insert_failed_run(db)
        existing = {
            "id": "t0001",
            "title": "[cron-fail] dream-eod",
            "description": "WHAT: previous failure\n",
            "status": "todo",
        }
        pd = _FakePD(open_tasks=[existing])
        out = escalator.process_failed_runs(
            yaml_path=yaml, db_path=db, pd_client=pd,
        )
        assert out.filed == []
        assert out.appended == ["dream-eod"]
        assert len(pd.created) == 0
        assert len(pd.updated) == 1
        assert "Re-fire" in pd.updated[0]["description"]
        # Original description preserved (with separator + note).
        assert "previous failure" in pd.updated[0]["description"]

    def test_done_task_does_not_dedupe(self, tmp_path, db):
        # "done" tasks are NOT in list_open_tasks() per the contract;
        # _FakePD only returns what's given. Simulate by passing none.
        yaml = _yaml(tmp_path)
        _insert_failed_run(db)
        pd = _FakePD(open_tasks=[])  # list_open_tasks already filters
        out = escalator.process_failed_runs(
            yaml_path=yaml, db_path=db, pd_client=pd,
        )
        assert out.filed == ["dream-eod"]
        assert len(pd.created) == 1

    def test_each_failure_processed_once(self, tmp_path, db):
        yaml = _yaml(tmp_path)
        _insert_failed_run(db)
        _insert_failed_run(db, started_at="2026-05-12T18:00:00+00:00")
        pd = _FakePD(open_tasks=[])
        out1 = escalator.process_failed_runs(
            yaml_path=yaml, db_path=db, pd_client=pd,
        )
        # First run creates the task, second appends.
        assert out1.filed == ["dream-eod"]
        assert out1.appended == ["dream-eod"]
        # Second cycle: nothing left to do.
        pd2 = _FakePD(open_tasks=[{"id": "t0001",
                                     "title": "[cron-fail] dream-eod",
                                     "description": "x",
                                     "status": "todo"}])
        out2 = escalator.process_failed_runs(
            yaml_path=yaml, db_path=db, pd_client=pd2,
        )
        assert out2.filed == []
        assert out2.appended == []
        assert pd2.created == [] and pd2.updated == []

    def test_pd_unreachable_leaves_row_unescalated(self, tmp_path, db):
        yaml = _yaml(tmp_path)
        rid = _insert_failed_run(db)
        pd = _FakePD(raise_on={"list"})
        out = escalator.process_failed_runs(
            yaml_path=yaml, db_path=db, pd_client=pd,
        )
        assert out.skipped_pd_unreachable == ["dream-eod"]
        with sqlite3.connect(str(db)) as conn:
            esc = conn.execute(
                "SELECT escalated_at FROM cron_runs WHERE run_id = ?",
                (rid,),
            ).fetchone()[0]
        assert esc is None  # available for retry

    def test_pd_project_override_respected(self, tmp_path, db):
        # When escalation.pd_project is explicitly set, it overrides
        # owner_project.
        yaml = _yaml(tmp_path, pd_project="pipeline-dashboard")
        _insert_failed_run(db)
        pd = _FakePD(open_tasks=[])
        escalator.process_failed_runs(
            yaml_path=yaml, db_path=db, pd_client=pd,
        )
        assert pd.created[0]["project_id"] == "pipeline-dashboard"


# ---------------------------------------------------------------------------
# discord_only + log_only paths
# ---------------------------------------------------------------------------

class TestNonPdEscalations:
    def test_discord_only_no_pd_call(self, tmp_path, db):
        yaml = _yaml(tmp_path, on_failure="discord_only")
        _insert_failed_run(db)
        pd = _FakePD(open_tasks=[])
        out = escalator.process_failed_runs(
            yaml_path=yaml, db_path=db, pd_client=pd,
        )
        assert out.discord == ["dream-eod"]
        assert pd.created == [] and pd.updated == []
        # Row IS marked escalated to stop the loop.
        with sqlite3.connect(str(db)) as conn:
            esc = conn.execute("SELECT escalated_at FROM cron_runs").fetchone()[0]
        assert esc is not None

    def test_log_only_no_pd_call(self, tmp_path, db):
        yaml = _yaml(tmp_path, on_failure="log_only")
        _insert_failed_run(db)
        pd = _FakePD(open_tasks=[])
        out = escalator.process_failed_runs(
            yaml_path=yaml, db_path=db, pd_client=pd,
        )
        assert out.log_only == ["dream-eod"]
        assert pd.created == [] and pd.updated == []


# ---------------------------------------------------------------------------
# Orphan run (entry deleted from YAML)
# ---------------------------------------------------------------------------

class TestOrphanRuns:
    def test_failed_run_with_no_yaml_entry_marked_escalated(self,
                                                               tmp_path, db):
        # Empty YAML.
        yaml = tmp_path / "automations.yaml"
        yaml.write_text("schema_version: 1\nautomations: []\n", encoding="utf-8")
        rid = _insert_failed_run(db, entry_name="ghost-entry")
        pd = _FakePD(open_tasks=[])
        out = escalator.process_failed_runs(
            yaml_path=yaml, db_path=db, pd_client=pd,
        )
        assert out.skipped_no_entry == ["ghost-entry"]
        with sqlite3.connect(str(db)) as conn:
            esc = conn.execute(
                "SELECT escalated_at FROM cron_runs WHERE run_id = ?",
                (rid,),
            ).fetchone()[0]
        # Marked escalated so we don't loop forever on it.
        assert esc is not None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCLI:
    def test_once_returns_zero(self, tmp_path, db, monkeypatch):
        yaml = _yaml(tmp_path)
        monkeypatch.setattr(escalator, "REGISTRY_YAML", yaml)
        monkeypatch.setattr(escalator, "REGISTRY_DB", db)
        # Patch the PdClient class so the CLI's default client is faked.
        monkeypatch.setattr(escalator, "PdClient",
                             lambda *a, **k: _FakePD(open_tasks=[]))
        rc = escalator.main(["--once"])
        assert rc == 0

    def test_requires_once_or_loop(self):
        with pytest.raises(SystemExit):
            escalator.main([])


# ---------------------------------------------------------------------------
# /api/registry/escalate endpoint
# ---------------------------------------------------------------------------

class TestApiEndpoint:
    def test_escalate_endpoint_runs_one_pass(self, tmp_path, db, monkeypatch):
        import app as registry_app
        yaml = _yaml(tmp_path)
        _insert_failed_run(db)
        monkeypatch.setattr(registry_app, "REGISTRY_YAML", yaml)
        monkeypatch.setattr(registry_app, "REGISTRY_DB", db)
        # Inject the fake PD client into escalator's default constructor.
        monkeypatch.setattr(escalator, "PdClient",
                             lambda *a, **k: _FakePD(open_tasks=[]))
        client = TestClient(registry_app.app)
        r = client.post("/api/registry/escalate")
        assert r.status_code == 200
        body = r.json()
        assert body["filed"] == ["dream-eod"]
