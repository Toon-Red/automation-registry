"""Tests for the claude_desktop_scheduled mechanism backend (AR-S3g)."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import schema as _schema
import state as _state
import desktop_scheduled_handler as dsh


# ---------------------------------------------------------------------------
# YAML fixtures
# ---------------------------------------------------------------------------

_TIME_OF_DAY_YAML = """\
schema_version: 1
automations:
  - name: dream-1400-poll
    description: 14:00 hourly poll
    owner_project: dream
    target: dream.orchestrator:poll
    target_kind: python_callable
    mechanism: claude_desktop_scheduled
    schedule: "0 14 * * *"
    escalation:
      channel: discord
      on_failure: log_only
    enabled: true
"""

_STATE_AWARE_YAML = """\
schema_version: 1
automations:
  - name: dream-morning-summary
    description: SOD standup -- fires on session-open past midnight when SOD not yet run today.
    owner_project: dream
    target: dream.orchestrator:run_morning
    target_kind: python_callable
    mechanism: claude_desktop_scheduled
    schedule: null
    trigger:
      kind: state
      fire_when: "last_sod_date != today"
      after_event: session_open
      state_file: data/workflow_state.json
      on_fire_update: last_sod_date
    escalation:
      channel: discord
      on_failure: file_pd_task
    enabled: true
"""


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def yaml_path(tmp_path):
    return tmp_path / "automations.yaml"


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "registry.db"
    dsh._ensure_schema(p)
    return p


@pytest.fixture
def hooks_dir(tmp_path):
    d = tmp_path / "hooks"
    d.mkdir()
    return d


@pytest.fixture
def registry_root(tmp_path):
    r = tmp_path / "registry"
    r.mkdir()
    return r


# ---------------------------------------------------------------------------
# Schema validator (v2 additions)
# ---------------------------------------------------------------------------

class TestSchemaValidator:
    def test_time_of_day_accepted(self, yaml_path):
        _write(yaml_path, _TIME_OF_DAY_YAML)
        autos = _schema.load_automations(yaml_path)
        assert autos[0].mechanism == "claude_desktop_scheduled"
        assert autos[0].schedule == "0 14 * * *"
        assert autos[0].trigger is None

    def test_state_aware_accepted(self, yaml_path):
        _write(yaml_path, _STATE_AWARE_YAML)
        autos = _schema.load_automations(yaml_path)
        assert autos[0].schedule is None
        assert autos[0].trigger["kind"] == "state"
        assert autos[0].trigger["fire_when"] == "last_sod_date != today"

    def test_both_flavours_rejected(self, yaml_path):
        body = _STATE_AWARE_YAML.replace("schedule: null", 'schedule: "0 8 * * *"')
        _write(yaml_path, body)
        with pytest.raises(_schema.SchemaError, match="cannot have BOTH"):
            _schema.load_automations(yaml_path)

    def test_neither_flavour_rejected(self, yaml_path):
        body = _TIME_OF_DAY_YAML.replace('schedule: "0 14 * * *"', "schedule: null")
        _write(yaml_path, body)
        with pytest.raises(_schema.SchemaError, match="either"):
            _schema.load_automations(yaml_path)

    def test_state_aware_missing_field_rejected(self, yaml_path):
        body = _STATE_AWARE_YAML.replace(
            "      on_fire_update: last_sod_date\n", "",
        )
        _write(yaml_path, body)
        with pytest.raises(_schema.SchemaError, match="on_fire_update"):
            _schema.load_automations(yaml_path)

    def test_loop_continuous_requires_engines(self, yaml_path):
        body = """\
schema_version: 1
automations:
  - name: dream-work-cycle
    description: L8-L4 hierarchy operating continuously.
    owner_project: dream
    target: dream.orchestrator:run_work_cycle
    target_kind: python_callable
    mechanism: claude_loop_continuous
    schedule: null
    escalation:
      channel: discord
      on_failure: file_pd_task
    enabled: true
"""
        _write(yaml_path, body)
        with pytest.raises(_schema.SchemaError, match="engines"):
            _schema.load_automations(yaml_path)


# ---------------------------------------------------------------------------
# Reconcile -- time-of-day flavour
# ---------------------------------------------------------------------------

class TestTimeOfDayReconcile:
    def test_creates_pending_op_on_first_run(self, yaml_path, db_path,
                                                hooks_dir, registry_root):
        _write(yaml_path, _TIME_OF_DAY_YAML)
        result = dsh.reconcile(yaml_path, db_path,
                                hooks_dir=hooks_dir,
                                registry_root=registry_root)
        assert len(result.pending_ops) == 1
        op = result.pending_ops[0]
        assert op.action == "create"
        assert op.task_id == "dream-1400-poll"
        assert op.cron_expression == "0 14 * * *"
        assert "runner.py" in op.prompt
        assert "dream-1400-poll" in op.prompt
        # sqlite row recorded.
        assert dsh.list_installed(db_path)[0]["name"] == "dream-1400-poll"

    def test_idempotent_second_run(self, yaml_path, db_path,
                                     hooks_dir, registry_root):
        _write(yaml_path, _TIME_OF_DAY_YAML)
        dsh.reconcile(yaml_path, db_path,
                       hooks_dir=hooks_dir, registry_root=registry_root)
        result = dsh.reconcile(yaml_path, db_path,
                                hooks_dir=hooks_dir,
                                registry_root=registry_root)
        assert result.pending_ops == []
        assert result.unchanged == ["dream-1400-poll"]

    def test_update_when_schedule_changes(self, yaml_path, db_path,
                                            hooks_dir, registry_root):
        _write(yaml_path, _TIME_OF_DAY_YAML)
        dsh.reconcile(yaml_path, db_path,
                       hooks_dir=hooks_dir, registry_root=registry_root)
        _write(yaml_path, _TIME_OF_DAY_YAML.replace("0 14", "0 15"))
        result = dsh.reconcile(yaml_path, db_path,
                                hooks_dir=hooks_dir,
                                registry_root=registry_root)
        assert len(result.pending_ops) == 1
        assert result.pending_ops[0].action == "update"
        assert result.pending_ops[0].cron_expression == "0 15 * * *"

    def test_disable_when_removed(self, yaml_path, db_path,
                                    hooks_dir, registry_root):
        _write(yaml_path, _TIME_OF_DAY_YAML)
        dsh.reconcile(yaml_path, db_path,
                       hooks_dir=hooks_dir, registry_root=registry_root)
        _write(yaml_path, "schema_version: 1\nautomations: []\n")
        result = dsh.reconcile(yaml_path, db_path,
                                hooks_dir=hooks_dir,
                                registry_root=registry_root)
        assert any(op.action == "disable"
                    and op.task_id == "dream-1400-poll"
                    for op in result.pending_ops)
        assert result.disabled == ["dream-1400-poll"]

    def test_ack_marks_applied(self, yaml_path, db_path,
                                 hooks_dir, registry_root):
        _write(yaml_path, _TIME_OF_DAY_YAML)
        dsh.reconcile(yaml_path, db_path,
                       hooks_dir=hooks_dir, registry_root=registry_root)
        count = dsh.ack_applied(db_path, ["dream-1400-poll"])
        assert count == 1
        rec = dsh.list_installed(db_path)[0]
        assert rec["last_applied_ts"] is not None


# ---------------------------------------------------------------------------
# Reconcile -- state-aware flavour
# ---------------------------------------------------------------------------

class TestStateAwareReconcile:
    def test_installs_hook_file(self, yaml_path, db_path,
                                  hooks_dir, registry_root):
        _write(yaml_path, _STATE_AWARE_YAML)
        result = dsh.reconcile(yaml_path, db_path,
                                hooks_dir=hooks_dir,
                                registry_root=registry_root)
        assert result.pending_ops == []  # state-aware doesn't need MCP
        assert len(result.state_aware_installs) == 1
        install = result.state_aware_installs[0]
        assert install.name == "dream-morning-summary"
        hook_path = Path(install.hook_path)
        assert hook_path.is_file()
        content = hook_path.read_text(encoding="utf-8")
        assert "ENTRY_NAME = 'dream-morning-summary'" in content
        assert "last_sod_date != today" in content
        assert "ON_FIRE_UPDATE = 'last_sod_date'" in content

    def test_hook_uninstalled_on_remove(self, yaml_path, db_path,
                                          hooks_dir, registry_root):
        _write(yaml_path, _STATE_AWARE_YAML)
        dsh.reconcile(yaml_path, db_path,
                       hooks_dir=hooks_dir, registry_root=registry_root)
        hook_file = hooks_dir / "registry_state_check_dream_morning_summary.py"
        assert hook_file.is_file()
        _write(yaml_path, "schema_version: 1\nautomations: []\n")
        result = dsh.reconcile(yaml_path, db_path,
                                hooks_dir=hooks_dir,
                                registry_root=registry_root)
        assert not hook_file.exists()
        assert result.disabled == ["dream-morning-summary"]

    def test_state_aware_idempotent(self, yaml_path, db_path,
                                       hooks_dir, registry_root):
        _write(yaml_path, _STATE_AWARE_YAML)
        dsh.reconcile(yaml_path, db_path,
                       hooks_dir=hooks_dir, registry_root=registry_root)
        result = dsh.reconcile(yaml_path, db_path,
                                hooks_dir=hooks_dir,
                                registry_root=registry_root)
        # No new installs on second run; entry is unchanged.
        assert result.state_aware_installs == []
        assert result.unchanged == ["dream-morning-summary"]


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

class TestApiEndpoints:
    def test_list_endpoint_empty(self, tmp_path, monkeypatch):
        import app as registry_app
        db = tmp_path / "registry.db"
        dsh._ensure_schema(db)
        monkeypatch.setattr(registry_app, "REGISTRY_DB", db)
        client = TestClient(registry_app.app)
        r = client.get("/api/registry/desktop_scheduled")
        assert r.status_code == 200
        assert r.json() == {"entries": []}

    def test_reconcile_endpoint(self, tmp_path, monkeypatch,
                                  hooks_dir, registry_root):
        import app as registry_app
        db = tmp_path / "registry.db"
        yaml = tmp_path / "automations.yaml"
        _write(yaml, _TIME_OF_DAY_YAML)
        dsh._ensure_schema(db)
        monkeypatch.setattr(registry_app, "REGISTRY_DB", db)
        monkeypatch.setattr(registry_app, "REGISTRY_YAML", yaml)
        # Reconcile defaults to ROOT-derived paths; rebind in the test
        # via monkeypatching the handler's module-level defaults.
        monkeypatch.setattr(dsh, "DEFAULT_HOOKS_DIR", hooks_dir)
        monkeypatch.setattr(dsh, "ROOT", registry_root)
        client = TestClient(registry_app.app)
        r = client.post("/api/registry/desktop_scheduled/reconcile")
        assert r.status_code == 200
        body = r.json()
        assert len(body["pending_ops"]) == 1
        assert body["pending_ops"][0]["action"] == "create"

    def test_ack_endpoint(self, tmp_path, monkeypatch,
                            hooks_dir, registry_root):
        import app as registry_app
        db = tmp_path / "registry.db"
        yaml = tmp_path / "automations.yaml"
        _write(yaml, _TIME_OF_DAY_YAML)
        dsh._ensure_schema(db)
        monkeypatch.setattr(registry_app, "REGISTRY_DB", db)
        monkeypatch.setattr(registry_app, "REGISTRY_YAML", yaml)
        monkeypatch.setattr(dsh, "DEFAULT_HOOKS_DIR", hooks_dir)
        monkeypatch.setattr(dsh, "ROOT", registry_root)
        # Reconcile to create the sqlite row.
        dsh.reconcile(yaml, db,
                       hooks_dir=hooks_dir, registry_root=registry_root)
        client = TestClient(registry_app.app)
        r = client.post(
            "/api/registry/desktop_scheduled/ack",
            json={"task_ids": ["dream-1400-poll"]},
        )
        assert r.status_code == 200
        assert r.json()["acknowledged"] == 1
