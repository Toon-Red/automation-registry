"""Tests for the claude_loop_continuous mechanism backend (AR-S3h).

Covers:
  * Schema v2 validation (already in test_desktop_scheduled.py for the
    'requires engines' rule -- here we cross-check the dataclass shape).
  * Reconcile install / unchanged / disable.
  * Heartbeat idle check (Q-B).
  * Pause / resume state (Q-D).
  * Mocked-429 -> pauses cleanly -> watchdog resumes on reset (Q-F).
  * Quota probe header parsing + (a)/(b)/(c) source ordering.
  * dispatch_iteration end-to-end with injected work-item picker +
    stack_runner.
  * /goal rendering wired into dispatch.
  * App endpoints (list / reconcile / dispatch / pause / resume /
    watchdog).
"""
from __future__ import annotations

import io
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import schema as _schema
import loop_continuous_handler as lch
import quota_probe


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_LOOP_YAML = """\
schema_version: 1
automations:
  - name: dream-work-cycle
    description: L8-L4 hierarchy operating continuously.
    owner_project: dream
    target: dream.orchestrator:run_work_cycle
    target_kind: python_callable
    mechanism: claude_loop_continuous
    schedule: null
    engines:
      L4: ollama-qwen2.5-coder
      L5: claude-haiku
      L6: claude-sonnet
      L7: claude-opus
      L8: claude-opus
    goal_template: "Process task {task.id} ({task.title}) -- {entry.description}"
    limit_aware:
      pause_at_remaining_pct: 5
      resume_on_reset: true
      idle_check: heartbeat_file
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
    return _write(tmp_path / "automations.yaml", _LOOP_YAML)


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "registry.db"
    lch._ensure_schema(p)
    return p


@pytest.fixture
def heartbeat_dir(tmp_path):
    d = tmp_path / "heartbeats"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Schema (cross-check; the 'requires engines' rejection is already
# covered in test_desktop_scheduled.TestSchemaValidator)
# ---------------------------------------------------------------------------

class TestSchemaShape:
    def test_loop_continuous_dataclass_fields(self, yaml_path):
        autos = _schema.load_automations(yaml_path)
        e = autos[0]
        assert e.mechanism == "claude_loop_continuous"
        assert e.schedule is None
        assert e.engines["L4"] == "ollama-qwen2.5-coder"
        assert e.engines["L8"] == "claude-opus"
        assert e.goal_template.startswith("Process task")
        assert e.limit_aware["pause_at_remaining_pct"] == 5
        assert e.limit_aware["idle_check"] == "heartbeat_file"


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------

class TestReconcile:
    def test_first_run_installs(self, yaml_path, db_path):
        result = lch.reconcile(yaml_path, db_path)
        assert result.installed == ["dream-work-cycle"]
        assert result.unchanged == []
        rows = lch.list_installed(db_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "installed"
        assert row["engines"]["L7"] == "claude-opus"
        assert row["limit_aware"]["resume_on_reset"] is True

    def test_idempotent_second_run(self, yaml_path, db_path):
        lch.reconcile(yaml_path, db_path)
        result = lch.reconcile(yaml_path, db_path)
        assert result.installed == []
        assert result.unchanged == ["dream-work-cycle"]

    def test_reinstall_on_engine_change(self, yaml_path, db_path):
        lch.reconcile(yaml_path, db_path)
        new_yaml = _LOOP_YAML.replace("claude-opus", "claude-sonnet")
        _write(yaml_path, new_yaml)
        result = lch.reconcile(yaml_path, db_path)
        assert result.reinstalled == ["dream-work-cycle"]

    def test_disable_when_removed(self, yaml_path, db_path):
        lch.reconcile(yaml_path, db_path)
        _write(yaml_path, "schema_version: 1\nautomations: []\n")
        result = lch.reconcile(yaml_path, db_path)
        assert result.disabled == ["dream-work-cycle"]
        # Row was pruned entirely.
        assert lch.list_installed(db_path) == []

    def test_enabled_false_disables(self, yaml_path, db_path):
        lch.reconcile(yaml_path, db_path)
        _write(yaml_path, _LOOP_YAML.replace("enabled: true", "enabled: false"))
        result = lch.reconcile(yaml_path, db_path)
        assert "dream-work-cycle" in result.disabled
        row = lch.get_entry(db_path, "dream-work-cycle")
        assert row["status"] == "disabled"


# ---------------------------------------------------------------------------
# Heartbeat + idle check (Q-B)
# ---------------------------------------------------------------------------

class TestHeartbeat:
    def test_idle_when_no_heartbeat(self, heartbeat_dir):
        assert lch.is_idle("x", heartbeat_dir=heartbeat_dir) is True

    def test_not_idle_immediately_after_write(self, heartbeat_dir):
        lch.write_heartbeat("x", pid=123, heartbeat_dir=heartbeat_dir)
        assert lch.is_idle("x", heartbeat_dir=heartbeat_dir,
                            idle_after_seconds=60) is False

    def test_idle_after_threshold(self, heartbeat_dir):
        lch.write_heartbeat("x", pid=123, heartbeat_dir=heartbeat_dir)
        # Pretend an hour has passed.
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        assert lch.is_idle("x", heartbeat_dir=heartbeat_dir,
                            idle_after_seconds=60, now=future) is True

    def test_heartbeat_body_carries_pid_and_work_item(self, heartbeat_dir):
        lch.write_heartbeat("x", pid=42, active_work_item="task-7",
                              heartbeat_dir=heartbeat_dir)
        body = lch.read_heartbeat("x", heartbeat_dir=heartbeat_dir)
        assert body["pid"] == 42
        assert body["active_work_item"] == "task-7"


# ---------------------------------------------------------------------------
# Pause / resume (Q-D)
# ---------------------------------------------------------------------------

class TestPauseResume:
    def test_pause_sets_paused_state(self, yaml_path, db_path):
        lch.reconcile(yaml_path, db_path)
        lch.pause_entry(db_path, "dream-work-cycle",
                          reason="rate_limit",
                          next_reset_ts="2026-05-15T01:00:00+00:00",
                          active_work_item="task-9")
        row = lch.get_entry(db_path, "dream-work-cycle")
        assert row["status"] == "paused"
        assert row["paused_reason"] == "rate_limit"
        assert row["next_reset_ts"] == "2026-05-15T01:00:00+00:00"
        assert row["active_work_item"] == "task-9"

    def test_resume_clears_pause_markers(self, yaml_path, db_path):
        lch.reconcile(yaml_path, db_path)
        lch.pause_entry(db_path, "dream-work-cycle")
        lch.resume_entry(db_path, "dream-work-cycle")
        row = lch.get_entry(db_path, "dream-work-cycle")
        assert row["status"] == "running"
        assert row["paused_reason"] is None
        assert row["paused_at"] is None


# ---------------------------------------------------------------------------
# Quota probe
# ---------------------------------------------------------------------------

def _fake_response(headers: dict[str, str], status: int = 200):
    """Build a stand-in for the urllib response that ``with`` works on."""
    class _Resp:
        def __init__(self):
            self.headers = headers
            self.status = status
        def __enter__(self):
            return self
        def __exit__(self, *_a):
            return False
        def read(self):
            return b""
    return _Resp()


class TestQuotaProbe:
    def test_parse_headers_picks_earliest_reset(self, tmp_path):
        headers = {
            "anthropic-ratelimit-tokens-reset": "2026-05-14T20:00:00+00:00",
            "anthropic-ratelimit-tokens-remaining": "5000",
            "anthropic-ratelimit-requests-reset": "2026-05-14T19:30:00+00:00",
            "anthropic-ratelimit-requests-remaining": "100",
        }

        def fake_opener(req, timeout):
            return _fake_response(headers)

        state = quota_probe.probe_quota(
            api_key="sk-test",
            operator_path=tmp_path / "op.json",
            state_path=tmp_path / "state.json",
            opener=fake_opener,
        )
        assert state.source == "api-header"
        # Requests reset is earlier than tokens reset.
        assert state.next_reset_ts == "2026-05-14T19:30:00+00:00"
        assert state.tokens_remaining == 5000

    def test_operator_file_overrides_when_earlier(self, tmp_path):
        op_path = tmp_path / "op.json"
        op_path.write_text(json.dumps({
            "next_reset_ts": "2026-05-14T18:00:00+00:00",
        }), encoding="utf-8")
        headers = {
            "anthropic-ratelimit-tokens-reset": "2026-05-14T22:00:00+00:00",
            "anthropic-ratelimit-tokens-remaining": "5000",
            "anthropic-ratelimit-requests-reset": "2026-05-14T22:00:00+00:00",
            "anthropic-ratelimit-requests-remaining": "100",
        }

        def fake_opener(req, timeout):
            return _fake_response(headers)

        state = quota_probe.probe_quota(
            api_key="sk-test",
            operator_path=op_path,
            state_path=tmp_path / "state.json",
            opener=fake_opener,
        )
        assert state.source == "operator"
        assert state.next_reset_ts == "2026-05-14T18:00:00+00:00"

    def test_no_api_key_falls_back_to_operator(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        op = tmp_path / "op.json"
        op.write_text(json.dumps({
            "next_reset_ts": "2026-05-14T18:00:00+00:00",
        }), encoding="utf-8")
        state = quota_probe.probe_quota(
            operator_path=op, state_path=tmp_path / "state.json",
        )
        assert state.source == "operator"
        assert state.next_reset_ts == "2026-05-14T18:00:00+00:00"

    def test_no_api_no_operator_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        state = quota_probe.probe_quota(
            operator_path=tmp_path / "missing.json",
            state_path=tmp_path / "state.json",
        )
        assert state.source == "fallback"
        # Round-trips through persisted file.
        loaded = quota_probe.read_state(tmp_path / "state.json")
        assert loaded.source == "fallback"

    def test_should_pause_on_zero_remaining(self):
        state = quota_probe.QuotaState(
            next_reset_ts="2026-05-14T20:00:00+00:00",
            source="api-header",
            tokens_remaining=0,
        )
        assert quota_probe.should_pause(state) is True

    def test_should_pause_below_safety_margin(self):
        state = quota_probe.QuotaState(
            next_reset_ts="2026-05-14T20:00:00+00:00",
            source="api-header",
            tokens_remaining=300,
        )
        # 300/10000 = 3% -- below the 5% threshold.
        assert quota_probe.should_pause(state, initial_tokens=10000) is True
        assert quota_probe.should_pause(state, initial_tokens=10000,
                                          pause_at_remaining_pct=2.0) is False


# ---------------------------------------------------------------------------
# dispatch_iteration end-to-end
# ---------------------------------------------------------------------------

class TestDispatchIteration:
    def test_no_work_path(self, yaml_path, db_path, heartbeat_dir):
        lch.reconcile(yaml_path, db_path)

        def picker(_row):
            return None

        result = lch.dispatch_iteration(
            "dream-work-cycle",
            yaml_path=yaml_path, db_path=db_path,
            work_item_picker=picker,
            heartbeat_dir=heartbeat_dir,
        )
        assert result.outcome == "no_work"
        # Heartbeat still written so the watchdog sees liveness.
        assert lch.read_heartbeat("dream-work-cycle",
                                    heartbeat_dir=heartbeat_dir) is not None

    def test_successful_iteration_renders_goal(self, yaml_path, db_path,
                                                  heartbeat_dir):
        lch.reconcile(yaml_path, db_path)
        seen_plans = []

        def picker(_row):
            return {"id": "task-9", "title": "Migrate Dream Auto",
                     "project_id": "automation-registry",
                     "priority": "high"}

        def runner(plan):
            seen_plans.append(plan)
            return {"outcome": "succeeded", "detail": "ok"}

        result = lch.dispatch_iteration(
            "dream-work-cycle",
            yaml_path=yaml_path, db_path=db_path,
            work_item_picker=picker, stack_runner=runner,
            heartbeat_dir=heartbeat_dir,
        )
        assert result.outcome == "succeeded"
        assert "task-9" in result.rendered_goal
        # /goal text substituted from work item + entry.
        assert "Migrate Dream Auto" in result.rendered_goal
        plan = seen_plans[0]
        assert plan["layers"]["L4"]["engine"] == "ollama-qwen2.5-coder"
        assert plan["layers"]["L8"]["engine"] == "claude-opus"
        assert "grader" in plan["layers"]["L4"]["roles"]
        # Iteration persisted.
        iters = lch.list_iterations(db_path, "dream-work-cycle")
        assert iters[0]["outcome"] == "succeeded"
        assert iters[0]["work_item_id"] == "task-9"

    def test_rate_limit_pauses_entry(self, yaml_path, db_path,
                                       heartbeat_dir):
        lch.reconcile(yaml_path, db_path)
        quota_state = quota_probe.QuotaState(
            next_reset_ts="2026-05-14T22:00:00+00:00",
            source="api-header",
            tokens_remaining=0,
        )

        def picker(_row):
            return {"id": "task-9", "title": "x", "project_id": "p"}

        def runner(plan):
            # Simulate the L8-L4 stack catching a 429 mid-call.
            return {"outcome": "rate_limited", "detail": "mocked 429"}

        result = lch.dispatch_iteration(
            "dream-work-cycle",
            yaml_path=yaml_path, db_path=db_path,
            work_item_picker=picker, stack_runner=runner,
            quota_state=quota_state, heartbeat_dir=heartbeat_dir,
        )
        assert result.outcome == "rate_limited"
        row = lch.get_entry(db_path, "dream-work-cycle")
        assert row["status"] == "paused"
        assert row["paused_reason"] == "mocked 429"
        assert row["next_reset_ts"] == "2026-05-14T22:00:00+00:00"
        assert row["active_work_item"] == "task-9"

    def test_paused_entry_skipped(self, yaml_path, db_path, heartbeat_dir):
        lch.reconcile(yaml_path, db_path)
        lch.pause_entry(db_path, "dream-work-cycle", reason="manual")

        def picker(_row):
            raise AssertionError("picker should not be called when paused")

        def runner(plan):
            raise AssertionError("runner should not be called when paused")

        result = lch.dispatch_iteration(
            "dream-work-cycle",
            yaml_path=yaml_path, db_path=db_path,
            work_item_picker=picker, stack_runner=runner,
            heartbeat_dir=heartbeat_dir,
        )
        assert result.outcome == "no_work"
        assert "paused" in result.detail


# ---------------------------------------------------------------------------
# Watchdog (Q-F crash recovery)
# ---------------------------------------------------------------------------

class TestWatchdog:
    def test_running_with_stale_heartbeat_marks_crashed(self, yaml_path,
                                                            db_path,
                                                            heartbeat_dir):
        lch.reconcile(yaml_path, db_path)
        lch.mark_running(db_path, "dream-work-cycle", pid=999)
        lch.write_heartbeat("dream-work-cycle", pid=999,
                              heartbeat_dir=heartbeat_dir)
        # Jump an hour forward so the heartbeat is stale.
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        restarted = []
        results = lch.watchdog_check(
            db_path,
            heartbeat_dir=heartbeat_dir,
            idle_after_seconds=60,
            now=future,
            resumer=restarted.append,
        )
        actions = {r.name: r.action for r in results}
        assert actions["dream-work-cycle"] == "flagged_crashed"
        assert restarted == ["dream-work-cycle"]

    def test_paused_with_past_reset_resumes(self, yaml_path, db_path,
                                              heartbeat_dir):
        lch.reconcile(yaml_path, db_path)
        # Pause with a reset that's already in the past.
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        lch.pause_entry(db_path, "dream-work-cycle",
                          reason="rate_limit", next_reset_ts=past)
        # No heartbeat (file absent -> is_idle True).
        restarted = []
        results = lch.watchdog_check(
            db_path, heartbeat_dir=heartbeat_dir,
            resumer=restarted.append,
        )
        actions = {r.name: r.action for r in results}
        assert actions["dream-work-cycle"] == "resumed"
        assert restarted == ["dream-work-cycle"]
        row = lch.get_entry(db_path, "dream-work-cycle")
        assert row["status"] == "running"

    def test_paused_with_future_reset_stays_paused(self, yaml_path, db_path,
                                                      heartbeat_dir):
        lch.reconcile(yaml_path, db_path)
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        lch.pause_entry(db_path, "dream-work-cycle",
                          reason="rate_limit", next_reset_ts=future)
        results = lch.watchdog_check(db_path, heartbeat_dir=heartbeat_dir)
        actions = {r.name: r.action for r in results}
        assert actions["dream-work-cycle"] == "still_paused"

    def test_full_429_cycle(self, yaml_path, db_path, heartbeat_dir):
        """End-to-end: dispatch hits a mocked 429, watchdog later
        resumes after the reset_ts passes."""
        lch.reconcile(yaml_path, db_path)
        # Use a reset 1 second in the future so the cycle is observable.
        soon = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        quota_state = quota_probe.QuotaState(
            next_reset_ts=soon, source="api-header", tokens_remaining=0,
        )

        def picker(_row):
            return {"id": "task-1", "title": "x", "project_id": "p"}

        def runner(plan):
            return {"outcome": "rate_limited", "detail": "429 received"}

        r1 = lch.dispatch_iteration(
            "dream-work-cycle",
            yaml_path=yaml_path, db_path=db_path,
            work_item_picker=picker, stack_runner=runner,
            quota_state=quota_state, heartbeat_dir=heartbeat_dir,
        )
        assert r1.outcome == "rate_limited"

        # Advance time past reset.
        after = datetime.now(timezone.utc) + timedelta(seconds=5)
        restarted = []
        results = lch.watchdog_check(
            db_path, heartbeat_dir=heartbeat_dir,
            now=after, resumer=restarted.append,
        )
        assert {r.name: r.action for r in results} == {
            "dream-work-cycle": "resumed",
        }
        assert restarted == ["dream-work-cycle"]


# ---------------------------------------------------------------------------
# App endpoints
# ---------------------------------------------------------------------------

class TestApiEndpoints:
    def test_list_endpoint(self, tmp_path, monkeypatch, yaml_path):
        import app as registry_app
        db = tmp_path / "registry.db"
        lch._ensure_schema(db)
        monkeypatch.setattr(registry_app, "REGISTRY_DB", db)
        monkeypatch.setattr(registry_app, "REGISTRY_YAML", yaml_path)
        client = TestClient(registry_app.app)
        r = client.get("/api/registry/loop_continuous")
        assert r.status_code == 200
        assert r.json() == {"entries": []}

    def test_reconcile_endpoint(self, tmp_path, monkeypatch, yaml_path):
        import app as registry_app
        db = tmp_path / "registry.db"
        lch._ensure_schema(db)
        monkeypatch.setattr(registry_app, "REGISTRY_DB", db)
        monkeypatch.setattr(registry_app, "REGISTRY_YAML", yaml_path)
        client = TestClient(registry_app.app)
        r = client.post("/api/registry/loop_continuous/reconcile")
        assert r.status_code == 200
        assert r.json()["installed"] == ["dream-work-cycle"]

    def test_pause_resume_endpoints(self, tmp_path, monkeypatch, yaml_path):
        import app as registry_app
        db = tmp_path / "registry.db"
        lch._ensure_schema(db)
        monkeypatch.setattr(registry_app, "REGISTRY_DB", db)
        monkeypatch.setattr(registry_app, "REGISTRY_YAML", yaml_path)
        lch.reconcile(yaml_path, db)
        client = TestClient(registry_app.app)
        r = client.post("/api/registry/loop_continuous/pause",
                         json={"entry_name": "dream-work-cycle",
                                "reason": "manual"})
        assert r.status_code == 200
        assert r.json()["status"] == "paused"
        assert lch.get_entry(db, "dream-work-cycle")["status"] == "paused"
        r = client.post("/api/registry/loop_continuous/resume",
                         json={"entry_name": "dream-work-cycle"})
        assert r.status_code == 200
        assert lch.get_entry(db, "dream-work-cycle")["status"] == "running"

    def test_watchdog_endpoint(self, tmp_path, monkeypatch, yaml_path):
        import app as registry_app
        db = tmp_path / "registry.db"
        lch._ensure_schema(db)
        monkeypatch.setattr(registry_app, "REGISTRY_DB", db)
        monkeypatch.setattr(registry_app, "REGISTRY_YAML", yaml_path)
        lch.reconcile(yaml_path, db)
        client = TestClient(registry_app.app)
        r = client.post("/api/registry/loop_continuous/watchdog")
        assert r.status_code == 200
        # 'installed' state -> noop action.
        body = r.json()
        assert body["results"][0]["action"] == "noop"
