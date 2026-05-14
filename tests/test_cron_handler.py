"""AR-S3b tests: schema validation, sqlite state, reconciliation, API."""
from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import schema as _schema
import state as _state
import cron_handler


# ---------------------------------------------------------------------------
# Fake supervisor module (drop-in for scripts.service_supervisor)
# ---------------------------------------------------------------------------

@dataclass
class _FakeCronJob:
    name: str
    schedule: str
    command: str
    working_dir: str
    description: str = ""


class _FakeSupervisor:
    """Records every install_cron / uninstall_cron call."""
    CronJob = _FakeCronJob

    def __init__(self) -> None:
        self.installed: list[_FakeCronJob] = []
        self.uninstalled: list[str] = []

    def install_cron(self, job):
        self.installed.append(job)

    def uninstall_cron(self, name):
        self.uninstalled.append(name)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "registry.db"
    _state.ensure_schema(p)
    return p


@pytest.fixture
def yaml_path(tmp_path):
    return tmp_path / "automations.yaml"


def _write_yaml(yaml_path: Path, body: str) -> Path:
    yaml_path.write_text(body, encoding="utf-8")
    return yaml_path


_GOOD_CRON_ENTRY = """\
schema_version: 1
automations:
  - name: dream-eod
    description: EOD review.
    owner_project: dream
    target: dream.orchestrator:run_eod
    target_kind: python_callable
    mechanism: cron
    schedule: "0 17 * * *"
    escalation:
      channel: discord
      on_failure: file_pd_task
    enabled: true
"""


# ---------------------------------------------------------------------------
# Schema validator
# ---------------------------------------------------------------------------

class TestSchemaLoader:
    def test_empty_yaml_returns_empty_list(self, yaml_path):
        _write_yaml(yaml_path, "schema_version: 1\nautomations: []\n")
        assert _schema.load_automations(yaml_path) == []

    def test_good_cron_entry_validates(self, yaml_path):
        _write_yaml(yaml_path, _GOOD_CRON_ENTRY)
        out = _schema.load_automations(yaml_path)
        assert len(out) == 1
        assert out[0].name == "dream-eod"
        assert out[0].mechanism == "cron"
        # pd_project defaults to owner_project per the schema doc.
        assert out[0].escalation.pd_project == "dream"

    def test_cron_without_schedule_rejected(self, yaml_path):
        body = _GOOD_CRON_ENTRY.replace('schedule: "0 17 * * *"', "schedule: null")
        _write_yaml(yaml_path, body)
        with pytest.raises(_schema.SchemaError, match="non-null schedule"):
            _schema.load_automations(yaml_path)

    def test_unknown_mechanism_rejected(self, yaml_path):
        body = _GOOD_CRON_ENTRY.replace("mechanism: cron", "mechanism: skyhook")
        _write_yaml(yaml_path, body)
        with pytest.raises(_schema.SchemaError, match="mechanism"):
            _schema.load_automations(yaml_path)

    def test_unknown_target_kind_rejected(self, yaml_path):
        body = _GOOD_CRON_ENTRY.replace(
            "target_kind: python_callable", "target_kind: telepathy",
        )
        _write_yaml(yaml_path, body)
        with pytest.raises(_schema.SchemaError, match="target_kind"):
            _schema.load_automations(yaml_path)

    def test_duplicate_names_rejected(self, yaml_path):
        body = _GOOD_CRON_ENTRY + _GOOD_CRON_ENTRY.split("automations:\n", 1)[1]
        _write_yaml(yaml_path, body)
        with pytest.raises(_schema.SchemaError, match="duplicate"):
            _schema.load_automations(yaml_path)

    def test_blocker_callback_requires_unblock_condition(self, yaml_path):
        body = _GOOD_CRON_ENTRY.replace(
            "mechanism: cron", "mechanism: claude_blocker_callback",
        ).replace('schedule: "0 17 * * *"', "schedule: null")
        _write_yaml(yaml_path, body)
        with pytest.raises(_schema.SchemaError, match="unblock_condition"):
            _schema.load_automations(yaml_path)

    def test_executor_requires_executor_ref(self, yaml_path):
        body = _GOOD_CRON_ENTRY.replace(
            "mechanism: cron", "mechanism: executor",
        ).replace('schedule: "0 17 * * *"', "schedule: null")
        _write_yaml(yaml_path, body)
        with pytest.raises(_schema.SchemaError, match="executor_ref"):
            _schema.load_automations(yaml_path)

    def test_unknown_schema_version_rejected(self, yaml_path):
        _write_yaml(yaml_path, "schema_version: 99\nautomations: []\n")
        with pytest.raises(_schema.SchemaError, match="schema_version"):
            _schema.load_automations(yaml_path)


# ---------------------------------------------------------------------------
# SQLite state layer
# ---------------------------------------------------------------------------

class TestState:
    def test_ensure_schema_creates_db_and_tables(self, tmp_path):
        p = tmp_path / "registry.db"
        _state.ensure_schema(p)
        with sqlite3.connect(str(p)) as conn:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert {"cron_entries", "cron_runs"} <= tables

    def test_ensure_schema_idempotent(self, tmp_path):
        p = tmp_path / "registry.db"
        _state.ensure_schema(p)
        _state.ensure_schema(p)  # second call must not raise

    def test_upsert_and_list(self, db_path):
        _state.upsert_entry(db_path, name="x", schedule="0 17 * * *",
                            command="/r", working_dir="/wd", description="",
                            enabled=True)
        rows = _state.list_entries(db_path)
        assert len(rows) == 1 and rows[0].name == "x"

    def test_upsert_updates_existing(self, db_path):
        _state.upsert_entry(db_path, name="x", schedule="0 17 * * *",
                            command="/r", working_dir="/wd", description="",
                            enabled=True)
        _state.upsert_entry(db_path, name="x", schedule="0 18 * * *",
                            command="/r", working_dir="/wd", description="",
                            enabled=True)
        rows = _state.list_entries(db_path)
        assert len(rows) == 1
        assert rows[0].schedule == "0 18 * * *"

    def test_delete_entry(self, db_path):
        _state.upsert_entry(db_path, name="x", schedule="0 17 * * *",
                            command="/r", working_dir="/wd", description="",
                            enabled=True)
        _state.delete_entry(db_path, "x")
        assert _state.list_entries(db_path) == []


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

class TestReconcile:
    def test_install_new_entry(self, yaml_path, db_path):
        _write_yaml(yaml_path, _GOOD_CRON_ENTRY)
        sup = _FakeSupervisor()
        result = cron_handler.reconcile(yaml_path, db_path, supervisor=sup)
        assert result.installed == ["dream-eod"]
        assert result.reinstalled == []
        assert result.uninstalled == []
        assert len(sup.installed) == 1
        assert sup.installed[0].name == "dream-eod"
        # State is recorded.
        assert _state.get_entry(db_path, "dream-eod") is not None

    def test_idempotent_second_run_is_unchanged(self, yaml_path, db_path):
        _write_yaml(yaml_path, _GOOD_CRON_ENTRY)
        sup = _FakeSupervisor()
        cron_handler.reconcile(yaml_path, db_path, supervisor=sup)
        sup2 = _FakeSupervisor()
        result = cron_handler.reconcile(yaml_path, db_path, supervisor=sup2)
        assert result.unchanged == ["dream-eod"]
        assert sup2.installed == []
        assert sup2.uninstalled == []

    def test_reinstall_when_schedule_changes(self, yaml_path, db_path):
        _write_yaml(yaml_path, _GOOD_CRON_ENTRY)
        sup = _FakeSupervisor()
        cron_handler.reconcile(yaml_path, db_path, supervisor=sup)
        # Mutate the schedule.
        _write_yaml(yaml_path, _GOOD_CRON_ENTRY.replace(
            "0 17 * * *", "0 18 * * *",
        ))
        sup2 = _FakeSupervisor()
        result = cron_handler.reconcile(yaml_path, db_path, supervisor=sup2)
        assert result.reinstalled == ["dream-eod"]
        assert len(sup2.installed) == 1
        assert sup2.installed[0].schedule == "0 18 * * *"

    def test_uninstall_when_entry_removed_from_yaml(self, yaml_path, db_path):
        _write_yaml(yaml_path, _GOOD_CRON_ENTRY)
        cron_handler.reconcile(yaml_path, db_path, supervisor=_FakeSupervisor())
        # Empty the YAML.
        _write_yaml(yaml_path, "schema_version: 1\nautomations: []\n")
        sup2 = _FakeSupervisor()
        result = cron_handler.reconcile(yaml_path, db_path, supervisor=sup2)
        assert result.uninstalled == ["dream-eod"]
        assert sup2.uninstalled == ["dream-eod"]
        assert _state.get_entry(db_path, "dream-eod") is None

    def test_disabled_entry_uninstalled(self, yaml_path, db_path):
        _write_yaml(yaml_path, _GOOD_CRON_ENTRY)
        cron_handler.reconcile(yaml_path, db_path, supervisor=_FakeSupervisor())
        _write_yaml(yaml_path, _GOOD_CRON_ENTRY.replace(
            "enabled: true", "enabled: false",
        ))
        sup2 = _FakeSupervisor()
        result = cron_handler.reconcile(yaml_path, db_path, supervisor=sup2)
        assert result.uninstalled == ["dream-eod"]
        assert sup2.uninstalled == ["dream-eod"]

    def test_non_cron_entries_ignored(self, yaml_path, db_path):
        body = _GOOD_CRON_ENTRY + """\
  - name: dream-self-heal
    description: Operator-invoked recovery.
    owner_project: dream
    target: self_heal:ensure_services
    target_kind: python_callable
    mechanism: manual
    schedule: null
    escalation:
      channel: discord
      on_failure: discord_only
    enabled: true
"""
        _write_yaml(yaml_path, body)
        sup = _FakeSupervisor()
        result = cron_handler.reconcile(yaml_path, db_path, supervisor=sup)
        assert result.installed == ["dream-eod"]  # only the cron entry
        assert len(sup.installed) == 1


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

class TestApiEndpoints:
    def test_list_cron_empty(self, tmp_path, monkeypatch):
        # Point app at a fresh empty DB.
        import app as registry_app
        db = tmp_path / "registry.db"
        _state.ensure_schema(db)
        monkeypatch.setattr(registry_app, "REGISTRY_DB", db)
        client = TestClient(registry_app.app)
        r = client.get("/api/registry/cron")
        assert r.status_code == 200
        assert r.json() == {"entries": []}

    def test_reconcile_endpoint_returns_diff(self, tmp_path, monkeypatch):
        import app as registry_app
        db = tmp_path / "registry.db"
        yaml = tmp_path / "automations.yaml"
        _write_yaml(yaml, _GOOD_CRON_ENTRY)
        _state.ensure_schema(db)
        monkeypatch.setattr(registry_app, "REGISTRY_DB", db)
        monkeypatch.setattr(registry_app, "REGISTRY_YAML", yaml)
        # Inject the fake supervisor so reconcile doesn't shell out.
        sup = _FakeSupervisor()
        import cron_handler as ch
        monkeypatch.setattr(ch, "_load_supervisor",
                             lambda pd_path=None: sup)
        client = TestClient(registry_app.app)
        r = client.post("/api/registry/reconcile")
        assert r.status_code == 200
        body = r.json()
        assert body["installed"] == ["dream-eod"]
        assert sup.installed[0].schedule == "0 17 * * *"
