"""Tests for the claude_routine mechanism backend (AR-S3i).

AR-S3i ships as a deferred stub: the schema validator + reconcile +
sqlite plumbing exist today so an operator can drop a YAML entry, run
reconcile, and get a structured ``PendingRoutineOp`` plan back. The
real Routines API client is unimplemented -- the activated backend
swaps in a concrete :class:`routine_handler.RoutinesClient` once
Q-PRESTON closes with a cloud-side use case.

Coverage:
  * Schema validator rules (claude_routine specifically):
      - non-null schedule required
      - sub-hourly schedules rejected (1h minimum)
      - target_kind=claude_prompt is mandatory
      - mismatched (target_kind, mechanism) combos rejected
  * Reconcile in the *deferred* path:
      - first run records the entry as deferred + emits a pending_op
      - second run is idempotent (no duplicate pending_op)
      - fingerprint change re-emits the pending_op
      - enabled: false / removal both emit disable pending_ops
  * Reconcile in the *activated* path (mocked RoutinesClient):
      - create_routine called on first install, routine_id persisted
      - update_routine called when fingerprint changes
      - delete_routine called when YAML drops the entry
      - if the injected client raises RoutineNotActivated the reconcile
        falls back to the deferred path (defence-in-depth so half-wired
        clients can't pretend they wrote to the cloud)
  * App endpoints: list + reconcile match the loop_continuous pattern.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import schema as _schema
import routine_handler as rh


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ROUTINE_YAML = """\
schema_version: 1
automations:
  - name: github-issue-triage
    description: Cloud-side GitHub issue triage routine.
    owner_project: automation-registry
    target: "/triage-github-issues"
    target_kind: claude_prompt
    mechanism: claude_routine
    schedule: "0 * * * *"
    escalation:
      channel: discord
      on_failure: file_pd_task
    enabled: true
"""


def _write(p: Path, body: str) -> Path:
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture
def yaml_path(tmp_path):
    return _write(tmp_path / "automations.yaml", _ROUTINE_YAML)


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "registry.db"
    rh._ensure_schema(p)
    return p


# ---------------------------------------------------------------------------
# Schema validator (rules specific to claude_routine)
# ---------------------------------------------------------------------------

class TestSchemaValidator:
    def test_valid_routine_round_trips(self, yaml_path):
        autos = _schema.load_automations(yaml_path)
        assert len(autos) == 1
        e = autos[0]
        assert e.mechanism == "claude_routine"
        assert e.target_kind == "claude_prompt"
        assert e.schedule == "0 * * * *"
        # Prompt body lives in `target` for target_kind=claude_prompt.
        assert e.target == "/triage-github-issues"

    def test_missing_schedule_rejected(self, tmp_path):
        bad = _ROUTINE_YAML.replace('schedule: "0 * * * *"',
                                     "schedule: null")
        p = _write(tmp_path / "bad.yaml", bad)
        with pytest.raises(_schema.SchemaError, match="claude_routine"):
            _schema.load_automations(p)

    def test_sub_hourly_minute_wildcard_rejected(self, tmp_path):
        bad = _ROUTINE_YAML.replace('schedule: "0 * * * *"',
                                     'schedule: "* * * * *"')
        p = _write(tmp_path / "bad.yaml", bad)
        with pytest.raises(_schema.SchemaError, match="60-minute"):
            _schema.load_automations(p)

    def test_sub_hourly_step_rejected(self, tmp_path):
        bad = _ROUTINE_YAML.replace('schedule: "0 * * * *"',
                                     'schedule: "*/5 * * * *"')
        p = _write(tmp_path / "bad.yaml", bad)
        with pytest.raises(_schema.SchemaError, match="60-minute"):
            _schema.load_automations(p)

    def test_sub_hourly_range_rejected(self, tmp_path):
        bad = _ROUTINE_YAML.replace('schedule: "0 * * * *"',
                                     'schedule: "0-30 * * * *"')
        p = _write(tmp_path / "bad.yaml", bad)
        with pytest.raises(_schema.SchemaError, match="60-minute"):
            _schema.load_automations(p)

    def test_sub_hourly_list_rejected(self, tmp_path):
        bad = _ROUTINE_YAML.replace('schedule: "0 * * * *"',
                                     'schedule: "0,30 * * * *"')
        p = _write(tmp_path / "bad.yaml", bad)
        with pytest.raises(_schema.SchemaError, match="60-minute"):
            _schema.load_automations(p)

    def test_hourly_step_accepted(self, tmp_path):
        # */60 fires once per hour -- legal under the v2 schema.
        ok = _ROUTINE_YAML.replace('schedule: "0 * * * *"',
                                    'schedule: "*/60 * * * *"')
        p = _write(tmp_path / "ok.yaml", ok)
        # Should NOT raise.
        autos = _schema.load_automations(p)
        assert autos[0].schedule == "*/60 * * * *"

    def test_daily_accepted(self, tmp_path):
        ok = _ROUTINE_YAML.replace('schedule: "0 * * * *"',
                                    'schedule: "0 9 * * *"')
        p = _write(tmp_path / "ok.yaml", ok)
        autos = _schema.load_automations(p)
        assert autos[0].schedule == "0 9 * * *"

    def test_target_kind_must_be_claude_prompt(self, tmp_path):
        # routine + target_kind=shell -> rule #5 rejects.
        bad = _ROUTINE_YAML.replace(
            "target_kind: claude_prompt", "target_kind: shell",
        )
        p = _write(tmp_path / "bad.yaml", bad)
        with pytest.raises(_schema.SchemaError,
                            match="target_kind"):
            _schema.load_automations(p)

    def test_claude_prompt_rejected_for_other_mechanisms(self, tmp_path):
        # target_kind=claude_prompt + mechanism=cron is illegal because
        # cron has no prompt-consuming path.
        bad = _ROUTINE_YAML.replace(
            "mechanism: claude_routine", "mechanism: cron",
        )
        p = _write(tmp_path / "bad.yaml", bad)
        with pytest.raises(_schema.SchemaError, match="claude_prompt"):
            _schema.load_automations(p)


# ---------------------------------------------------------------------------
# Reconcile -- deferred path (no client)
# ---------------------------------------------------------------------------

class TestReconcileDeferred:
    def test_first_run_records_deferred(self, yaml_path, db_path):
        result = rh.reconcile(yaml_path, db_path)
        assert result.deferred == ["github-issue-triage"]
        assert result.installed == []
        # Exactly one create pending_op surfaced for the operator.
        assert len(result.pending_ops) == 1
        op = result.pending_ops[0]
        assert op.action == "create"
        assert op.name == "github-issue-triage"
        assert op.schedule == "0 * * * *"
        assert op.prompt == "/triage-github-issues"
        # sqlite row visible.
        rows = rh.list_installed(db_path)
        assert len(rows) == 1
        assert rows[0]["status"] == rh.STATUS_DEFERRED
        assert rows[0]["routine_id"] is None

    def test_second_run_is_idempotent(self, yaml_path, db_path):
        rh.reconcile(yaml_path, db_path)
        result = rh.reconcile(yaml_path, db_path)
        # Already-deferred entries do not re-emit a pending_op.
        assert result.deferred == ["github-issue-triage"]
        assert result.pending_ops == []

    def test_fingerprint_change_re_emits_pending_op(self, yaml_path, db_path):
        rh.reconcile(yaml_path, db_path)
        new = _ROUTINE_YAML.replace(
            "Cloud-side GitHub issue triage routine.",
            "Cloud-side GitHub issue triage routine v2.",
        )
        _write(yaml_path, new)
        result = rh.reconcile(yaml_path, db_path)
        # The sqlite row exists from the first reconcile, so this is an
        # 'update' pending_op even though no cloud-side install ever
        # happened. That's the right semantics -- the operator's
        # follow-up call to the ``schedule`` skill should be an update
        # if they previously applied the create.
        assert result.deferred == ["github-issue-triage"]
        assert len(result.pending_ops) == 1
        assert result.pending_ops[0].action == "update"
        assert "v2" in result.pending_ops[0].description

    def test_enabled_false_emits_disable_pending_op(self, yaml_path,
                                                       db_path):
        rh.reconcile(yaml_path, db_path)
        _write(yaml_path, _ROUTINE_YAML.replace(
            "enabled: true", "enabled: false",
        ))
        result = rh.reconcile(yaml_path, db_path)
        assert result.disabled == ["github-issue-triage"]
        assert any(op.action == "disable" for op in result.pending_ops)
        # Row pruned.
        assert rh.list_installed(db_path) == []

    def test_removed_from_yaml_emits_disable_pending_op(self, yaml_path,
                                                            db_path):
        rh.reconcile(yaml_path, db_path)
        _write(yaml_path, "schema_version: 1\nautomations: []\n")
        result = rh.reconcile(yaml_path, db_path)
        assert result.disabled == ["github-issue-triage"]
        assert any(op.action == "disable" for op in result.pending_ops)
        assert rh.list_installed(db_path) == []


# ---------------------------------------------------------------------------
# Reconcile -- activated path (mocked RoutinesClient)
# ---------------------------------------------------------------------------

class _FakeRoutinesClient:
    """In-memory stand-in for the real cloud Routines client.

    Records every call so tests can assert the reconcile loop hit the
    right surface in the right order. Generates monotonic cloud ids.
    """

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.deleted: list[str] = []
        self._counter = 0

    def create_routine(self, *, name, schedule, prompt, description):
        self._counter += 1
        rid = f"routine-{self._counter}"
        self.created.append({
            "routine_id": rid, "name": name, "schedule": schedule,
            "prompt": prompt, "description": description,
        })
        return rid

    def update_routine(self, *, routine_id, schedule, prompt, description):
        self.updated.append({
            "routine_id": routine_id, "schedule": schedule,
            "prompt": prompt, "description": description,
        })

    def delete_routine(self, *, routine_id):
        self.deleted.append(routine_id)


class TestReconcileActivated:
    def test_first_run_calls_create(self, yaml_path, db_path):
        client = _FakeRoutinesClient()
        result = rh.reconcile(yaml_path, db_path, routines_client=client)
        assert result.installed == ["github-issue-triage"]
        assert result.deferred == []
        assert len(client.created) == 1
        assert client.created[0]["name"] == "github-issue-triage"
        assert client.created[0]["prompt"] == "/triage-github-issues"
        # Cloud id stored.
        row = rh.get_entry(db_path, "github-issue-triage")
        assert row["status"] == rh.STATUS_INSTALLED
        assert row["routine_id"] == "routine-1"

    def test_idempotent_when_unchanged(self, yaml_path, db_path):
        client = _FakeRoutinesClient()
        rh.reconcile(yaml_path, db_path, routines_client=client)
        client.created.clear()
        result = rh.reconcile(yaml_path, db_path, routines_client=client)
        assert result.unchanged == ["github-issue-triage"]
        assert client.created == []
        assert client.updated == []

    def test_fingerprint_change_calls_update(self, yaml_path, db_path):
        client = _FakeRoutinesClient()
        rh.reconcile(yaml_path, db_path, routines_client=client)
        _write(yaml_path, _ROUTINE_YAML.replace(
            'schedule: "0 * * * *"', 'schedule: "0 9 * * *"',
        ))
        result = rh.reconcile(yaml_path, db_path, routines_client=client)
        assert result.reinstalled == ["github-issue-triage"]
        assert len(client.updated) == 1
        assert client.updated[0]["schedule"] == "0 9 * * *"
        # Routine id preserved across update.
        assert client.updated[0]["routine_id"] == "routine-1"

    def test_yaml_removal_calls_delete(self, yaml_path, db_path):
        client = _FakeRoutinesClient()
        rh.reconcile(yaml_path, db_path, routines_client=client)
        _write(yaml_path, "schema_version: 1\nautomations: []\n")
        result = rh.reconcile(yaml_path, db_path, routines_client=client)
        assert result.disabled == ["github-issue-triage"]
        assert client.deleted == ["routine-1"]
        assert rh.list_installed(db_path) == []

    def test_enabled_false_calls_delete(self, yaml_path, db_path):
        client = _FakeRoutinesClient()
        rh.reconcile(yaml_path, db_path, routines_client=client)
        _write(yaml_path, _ROUTINE_YAML.replace(
            "enabled: true", "enabled: false",
        ))
        result = rh.reconcile(yaml_path, db_path, routines_client=client)
        assert result.disabled == ["github-issue-triage"]
        assert client.deleted == ["routine-1"]

    def test_half_wired_client_falls_back_to_deferred(self, yaml_path,
                                                          db_path):
        """A client that claims to be real but raises
        :class:`RoutineNotActivated` is treated as deferred -- the
        reconcile must never silently swallow that and pretend it wrote
        to the cloud."""

        class _LyingClient:
            def create_routine(self, **kw):
                raise rh.RoutineNotActivated("not wired yet")

            def update_routine(self, **kw):
                raise rh.RoutineNotActivated("not wired yet")

            def delete_routine(self, **kw):
                pass

        result = rh.reconcile(yaml_path, db_path,
                                routines_client=_LyingClient())
        assert result.deferred == ["github-issue-triage"]
        assert result.installed == []
        assert len(result.pending_ops) == 1
        assert "RoutineNotActivated" in result.pending_ops[0].reason
        row = rh.get_entry(db_path, "github-issue-triage")
        assert row["status"] == rh.STATUS_DEFERRED


# ---------------------------------------------------------------------------
# PendingRoutineOp shape
# ---------------------------------------------------------------------------

class TestPendingRoutineOp:
    def test_to_dict_is_json_safe(self, yaml_path, db_path):
        rh.reconcile(yaml_path, db_path)
        result = rh.reconcile(yaml_path, db_path)
        # No pending ops second time around -- exercise the to_dict on
        # a fresh op.
        op = rh.PendingRoutineOp(
            action="create", name="x", schedule="0 * * * *",
            prompt="/p", description="d",
        )
        d = op.to_dict()
        assert d == {
            "action": "create", "name": "x", "schedule": "0 * * * *",
            "prompt": "/p", "description": "d",
            "reason": "deferred (AR-S3i stub)",
        }


# ---------------------------------------------------------------------------
# App endpoints
# ---------------------------------------------------------------------------

class TestApiEndpoints:
    def test_list_endpoint_empty(self, tmp_path, monkeypatch, yaml_path):
        import app as registry_app
        db = tmp_path / "registry.db"
        rh._ensure_schema(db)
        monkeypatch.setattr(registry_app, "REGISTRY_DB", db)
        monkeypatch.setattr(registry_app, "REGISTRY_YAML", yaml_path)
        client = TestClient(registry_app.app)
        r = client.get("/api/registry/routine")
        assert r.status_code == 200
        assert r.json() == {"entries": []}

    def test_reconcile_endpoint_returns_deferred(self, tmp_path, monkeypatch,
                                                      yaml_path):
        import app as registry_app
        db = tmp_path / "registry.db"
        rh._ensure_schema(db)
        monkeypatch.setattr(registry_app, "REGISTRY_DB", db)
        monkeypatch.setattr(registry_app, "REGISTRY_YAML", yaml_path)
        client = TestClient(registry_app.app)
        r = client.post("/api/registry/routine/reconcile")
        assert r.status_code == 200
        body = r.json()
        assert body["deferred"] == ["github-issue-triage"]
        assert body["installed"] == []
        assert len(body["pending_ops"]) == 1
        assert body["pending_ops"][0]["action"] == "create"

    def test_list_endpoint_after_reconcile(self, tmp_path, monkeypatch,
                                               yaml_path):
        import app as registry_app
        db = tmp_path / "registry.db"
        rh._ensure_schema(db)
        monkeypatch.setattr(registry_app, "REGISTRY_DB", db)
        monkeypatch.setattr(registry_app, "REGISTRY_YAML", yaml_path)
        rh.reconcile(yaml_path, db)
        client = TestClient(registry_app.app)
        r = client.get("/api/registry/routine")
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert len(entries) == 1
        assert entries[0]["name"] == "github-issue-triage"
        assert entries[0]["status"] == rh.STATUS_DEFERRED
