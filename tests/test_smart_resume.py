"""Tests for the smart auto-resume scheduler (AR-S3k).

Covers:
  * compute_fire_at adds the buffer correctly AND nudges past-due
    timestamps forward so OS schedulers don't reject them.
  * task_id_for is stable + schtasks-safe.
  * FileMarkerScheduler install/cancel round-trips.
  * schedule_resume cancels a prior one-shot before installing a new
    one (so two firings never coexist for the same entry).
  * cancel_resume + mark_fired update sqlite + the OS task.
  * apply_from_quota_state reads quota_probe state end-to-end.
  * on_session_start re-probes + reschedules.
  * status() returns the dashboard-shaped snapshot.
  * App endpoints round-trip via TestClient.
  * DONE-WHEN guarantees:
      - resume within ``buffer_seconds`` of refresh (default 60s)
      - zero firings during a locked-out window (only ONE pending one-shot
        per entry, no fixed-interval polling)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import quota_probe
import smart_resume


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeScheduler:
    """Records install/cancel calls in memory so tests can inspect."""

    def __init__(self):
        self.name = "fake"
        self.installs: list[dict] = []
        self.cancels: list[str] = []
        self.installed: dict[str, dict] = {}

    def install(self, task_id, fire_at_iso, command_argv):
        record = {
            "task_id": task_id,
            "fire_at_iso": fire_at_iso,
            "command_argv": list(command_argv),
        }
        self.installs.append(record)
        self.installed[task_id] = record
        return task_id

    def cancel(self, task_id):
        self.cancels.append(task_id)
        self.installed.pop(task_id, None)


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "registry.db"
    smart_resume._ensure_schema(p)
    return p


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "quota_state.json"


@pytest.fixture
def scheduler():
    return FakeScheduler()


# ---------------------------------------------------------------------------
# compute_fire_at
# ---------------------------------------------------------------------------

class TestComputeFireAt:
    def test_adds_buffer(self):
        # Use a far-future timestamp so the past-due nudge never kicks in.
        reset = "2099-01-01T00:00:00+00:00"
        fire = smart_resume.compute_fire_at(reset, buffer_seconds=60)
        fire_dt = quota_probe.parse_iso(fire)
        reset_dt = quota_probe.parse_iso(reset)
        assert (fire_dt - reset_dt).total_seconds() == 60

    def test_custom_buffer(self):
        reset = "2099-01-01T00:00:00+00:00"
        fire = smart_resume.compute_fire_at(reset, buffer_seconds=15)
        assert (quota_probe.parse_iso(fire)
                - quota_probe.parse_iso(reset)).total_seconds() == 15

    def test_past_reset_nudged_to_now_plus_one(self):
        """A reset in the past should NOT be returned as fire_at -- OS
        schedulers reject past times. Nudge to now+1s so the resume
        kicks immediately."""
        now = datetime(2099, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        past_reset = (now - timedelta(hours=2)).isoformat()
        fire = smart_resume.compute_fire_at(
            past_reset, buffer_seconds=60, now=now,
        )
        fire_dt = quota_probe.parse_iso(fire)
        assert fire_dt > now
        # Sanity: it shouldn't lurch forward by an hour or anything daft.
        assert (fire_dt - now).total_seconds() < 5


# ---------------------------------------------------------------------------
# task_id_for
# ---------------------------------------------------------------------------

class TestTaskId:
    def test_prefix_and_lowercase(self):
        assert smart_resume.task_id_for("Dream-Work-Cycle") == "ar-resume-dream-work-cycle"

    def test_safe_for_schtasks(self):
        # No spaces, no underscores -- schtasks accepts hyphens on every
        # locale.
        tid = smart_resume.task_id_for("foo bar_baz")
        assert " " not in tid
        assert "_" not in tid


# ---------------------------------------------------------------------------
# FileMarkerScheduler
# ---------------------------------------------------------------------------

class TestFileMarkerScheduler:
    def test_install_writes_marker(self, tmp_path):
        sched = smart_resume.FileMarkerScheduler(marker_dir=tmp_path / "m")
        path = sched.install("ar-resume-x", "2099-01-01T00:00:00+00:00",
                              ["python", "runner.py", "--entry", "x"])
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert data["task_id"] == "ar-resume-x"
        assert data["fire_at_ts"] == "2099-01-01T00:00:00+00:00"
        assert data["command_argv"][-1] == "x"

    def test_cancel_removes_marker(self, tmp_path):
        sched = smart_resume.FileMarkerScheduler(marker_dir=tmp_path / "m")
        sched.install("ar-resume-y", "2099-01-01T00:00:00+00:00",
                       ["python", "x"])
        assert (tmp_path / "m" / "ar-resume-y.json").exists()
        sched.cancel("ar-resume-y")
        assert not (tmp_path / "m" / "ar-resume-y.json").exists()

    def test_cancel_is_idempotent(self, tmp_path):
        sched = smart_resume.FileMarkerScheduler(marker_dir=tmp_path / "m")
        sched.cancel("missing")  # no exception


# ---------------------------------------------------------------------------
# schedule_resume / cancel_resume / mark_fired
# ---------------------------------------------------------------------------

class TestScheduleResume:
    def test_first_schedule_records_pending(self, db_path, scheduler):
        result = smart_resume.schedule_resume(
            "dream-work-cycle",
            "2099-01-01T00:01:00+00:00",
            reset_source_ts="2099-01-01T00:00:00+00:00",
            reset_source="api-header",
            db_path=db_path,
            scheduler=scheduler,
        )
        assert result.rescheduled is False
        assert result.fire_at_ts == "2099-01-01T00:01:00+00:00"
        assert len(scheduler.installs) == 1
        assert scheduler.cancels == []

        row = smart_resume.get_pending(db_path, "dream-work-cycle")
        assert row["status"] == "pending"
        assert row["fire_at_ts"] == "2099-01-01T00:01:00+00:00"
        assert row["reset_source"] == "api-header"
        assert row["backend"] == "fake"

    def test_reschedule_cancels_prior(self, db_path, scheduler):
        """DONE-WHEN: zero spawns during a locked-out window. Enforced
        by 'only one pending one-shot per entry at a time'."""
        smart_resume.schedule_resume(
            "dream-work-cycle",
            "2099-01-01T00:01:00+00:00",
            reset_source_ts="2099-01-01T00:00:00+00:00",
            reset_source="api-header",
            db_path=db_path, scheduler=scheduler,
        )
        result = smart_resume.schedule_resume(
            "dream-work-cycle",
            "2099-01-01T02:01:00+00:00",
            reset_source_ts="2099-01-01T02:00:00+00:00",
            reset_source="api-header",
            db_path=db_path, scheduler=scheduler,
        )
        assert result.rescheduled is True
        # Exactly one install survived, and there was exactly one cancel
        # in between -- no overlapping one-shots.
        assert len(scheduler.installed) == 1
        assert scheduler.cancels == ["ar-resume-dream-work-cycle"]
        row = smart_resume.get_pending(db_path, "dream-work-cycle")
        assert row["fire_at_ts"] == "2099-01-01T02:01:00+00:00"
        assert row["status"] == "pending"

    def test_cancel_resume_updates_state(self, db_path, scheduler):
        smart_resume.schedule_resume(
            "dream-work-cycle",
            "2099-01-01T00:01:00+00:00",
            reset_source_ts="2099-01-01T00:00:00+00:00",
            reset_source="api-header",
            db_path=db_path, scheduler=scheduler,
        )
        cancelled = smart_resume.cancel_resume(
            "dream-work-cycle", db_path=db_path, scheduler=scheduler,
        )
        assert cancelled is True
        row = smart_resume.get_pending(db_path, "dream-work-cycle")
        assert row["status"] == "cancelled"
        assert scheduler.cancels == ["ar-resume-dream-work-cycle"]

    def test_cancel_resume_when_absent(self, db_path, scheduler):
        cancelled = smart_resume.cancel_resume(
            "never-scheduled", db_path=db_path, scheduler=scheduler,
        )
        assert cancelled is False
        assert scheduler.cancels == []

    def test_mark_fired_records_history(self, db_path, scheduler):
        smart_resume.schedule_resume(
            "dream-work-cycle",
            "2099-01-01T00:01:00+00:00",
            reset_source_ts="2099-01-01T00:00:00+00:00",
            reset_source="api-header",
            db_path=db_path, scheduler=scheduler,
        )
        smart_resume.mark_fired(
            "dream-work-cycle", db_path=db_path, scheduler=scheduler,
        )
        row = smart_resume.get_pending(db_path, "dream-work-cycle")
        assert row["status"] == "fired"
        assert row["last_fired_at"]
        # The OS task was cleaned up so Task Scheduler doesn't keep
        # stale ONCE entries.
        assert scheduler.cancels == ["ar-resume-dream-work-cycle"]


# ---------------------------------------------------------------------------
# apply_from_quota_state / on_session_start
# ---------------------------------------------------------------------------

class TestApplyFromQuotaState:
    def test_reads_persisted_state(self, db_path, state_path, scheduler):
        quota_probe.write_state(
            quota_probe.QuotaState(
                next_reset_ts="2099-01-01T00:00:00+00:00",
                source="api-header",
                tokens_remaining=0,
                checked_at="2099-01-01T00:00:00+00:00",
            ),
            path=state_path,
        )
        result = smart_resume.apply_from_quota_state(
            "dream-work-cycle",
            state_path=state_path, db_path=db_path,
            scheduler=scheduler,
            now=datetime(2098, 12, 31, 23, 0, 0, tzinfo=timezone.utc),
        )
        # DONE-WHEN: resume fires within buffer_seconds of refresh.
        fire_dt = quota_probe.parse_iso(result.fire_at_ts)
        reset_dt = quota_probe.parse_iso(result.reset_source_ts)
        assert (fire_dt - reset_dt).total_seconds() == 60
        assert result.reset_source == "api-header"

    def test_errors_when_no_probe_state(self, db_path, state_path, scheduler):
        with pytest.raises(quota_probe.QuotaProbeError):
            smart_resume.apply_from_quota_state(
                "dream-work-cycle",
                state_path=state_path, db_path=db_path,
                scheduler=scheduler,
            )

    def test_on_session_start_reprobes_and_reschedules(
        self, db_path, state_path, scheduler, tmp_path,
    ):
        """Step (5) of the spec: 'on the next session's first response,
        read the new headers + reschedule.'"""
        # First cycle -- pretend the previous probe wrote an old reset.
        quota_probe.write_state(
            quota_probe.QuotaState(
                next_reset_ts="2099-01-01T00:00:00+00:00",
                source="api-header",
                tokens_remaining=0,
                checked_at="2099-01-01T00:00:00+00:00",
            ),
            path=state_path,
        )
        smart_resume.apply_from_quota_state(
            "dream-work-cycle",
            state_path=state_path, db_path=db_path,
            scheduler=scheduler,
        )

        # New session's first response -- fresh headers say the reset
        # has moved forward by 5 hours (mock the urllib opener).
        new_reset = "2099-01-01T05:00:00+00:00"

        class _Resp:
            def __init__(self, headers):
                self.headers = headers
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b""

        def opener(_req, timeout):
            return _Resp({
                "anthropic-ratelimit-tokens-reset": new_reset,
                "anthropic-ratelimit-requests-reset": new_reset,
                "anthropic-ratelimit-tokens-remaining": "0",
                "anthropic-ratelimit-requests-remaining": "0",
            })

        op_path = tmp_path / "operator.json"  # absent
        result = smart_resume.on_session_start(
            "dream-work-cycle",
            api_key="sk-test",
            operator_path=op_path,
            state_path=state_path,
            db_path=db_path,
            scheduler=scheduler,
            opener=opener,
            now=datetime(2099, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        )
        # Re-scheduling -> the prior pending was cancelled cleanly.
        assert result.rescheduled is True
        # Fire = reset + 60s.
        assert result.reset_source_ts == new_reset
        assert quota_probe.parse_iso(
            result.fire_at_ts
        ) == quota_probe.parse_iso(new_reset) + timedelta(seconds=60)
        # Only ONE pending one-shot at a time.
        assert len(scheduler.installed) == 1


# ---------------------------------------------------------------------------
# list_pending + status
# ---------------------------------------------------------------------------

class TestQueries:
    def test_list_pending_returns_only_pending_by_default(
        self, db_path, scheduler,
    ):
        smart_resume.schedule_resume(
            "a", "2099-01-01T00:01:00+00:00",
            reset_source_ts="2099-01-01T00:00:00+00:00",
            reset_source="api-header",
            db_path=db_path, scheduler=scheduler,
        )
        smart_resume.schedule_resume(
            "b", "2099-01-02T00:01:00+00:00",
            reset_source_ts="2099-01-02T00:00:00+00:00",
            reset_source="operator",
            db_path=db_path, scheduler=scheduler,
        )
        smart_resume.mark_fired("a", db_path=db_path, scheduler=scheduler)

        pending = smart_resume.list_pending(db_path, status="pending")
        assert [r["entry_name"] for r in pending] == ["b"]
        fired = smart_resume.list_pending(db_path, status="fired")
        assert [r["entry_name"] for r in fired] == ["a"]
        all_rows = smart_resume.list_pending(db_path, status=None)
        assert {r["entry_name"] for r in all_rows} == {"a", "b"}

    def test_status_returns_seconds_until_fire(self, db_path, scheduler):
        # Fire 5 minutes out from a fixed "now".
        now = datetime(2099, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        fire = (now + timedelta(minutes=5)).isoformat()
        smart_resume.schedule_resume(
            "x", fire,
            reset_source_ts=now.isoformat(),
            reset_source="api-header",
            db_path=db_path, scheduler=scheduler,
        )
        s = smart_resume.status("x", db_path=db_path, now=now)
        assert s.has_pending is True
        assert s.seconds_until_fire == 300
        assert s.reset_source == "api-header"

    def test_status_for_unknown_entry(self, db_path):
        s = smart_resume.status("ghost", db_path=db_path)
        assert s.has_pending is False
        assert s.fire_at_ts is None


# ---------------------------------------------------------------------------
# App endpoints
# ---------------------------------------------------------------------------

class TestApiEndpoints:
    def test_list_endpoint_empty(self, tmp_path, monkeypatch):
        import app as registry_app
        db = tmp_path / "registry.db"
        smart_resume._ensure_schema(db)
        monkeypatch.setattr(registry_app, "REGISTRY_DB", db)
        client = TestClient(registry_app.app)
        r = client.get("/api/registry/smart_resume")
        assert r.status_code == 200
        body = r.json()
        assert body == {"pending": [], "history": []}

    def test_schedule_endpoint_errors_without_probe(self, tmp_path,
                                                      monkeypatch):
        import app as registry_app
        db = tmp_path / "registry.db"
        smart_resume._ensure_schema(db)
        monkeypatch.setattr(registry_app, "REGISTRY_DB", db)
        client = TestClient(registry_app.app)
        r = client.post(
            "/api/registry/smart_resume/schedule",
            json={"entry_name": "dream-work-cycle"},
        )
        assert r.status_code == 200
        assert "error" in r.json()
        assert "quota probe" in r.json()["error"]

    def test_schedule_endpoint_happy_path(self, tmp_path, monkeypatch):
        import app as registry_app
        db = tmp_path / "registry.db"
        smart_resume._ensure_schema(db)
        state_path = tmp_path / "quota_state.json"
        quota_probe.write_state(
            quota_probe.QuotaState(
                next_reset_ts="2099-01-01T00:00:00+00:00",
                source="api-header",
                tokens_remaining=0,
                checked_at="2099-01-01T00:00:00+00:00",
            ),
            path=state_path,
        )
        # Point the module-level state path at the temp file AND
        # inject the FakeScheduler via monkeypatching default_scheduler.
        monkeypatch.setattr(smart_resume, "DEFAULT_QUOTA_STATE_PATH",
                             state_path)
        fake = FakeScheduler()
        monkeypatch.setattr(smart_resume, "default_scheduler",
                             lambda: fake)
        monkeypatch.setattr(registry_app, "REGISTRY_DB", db)

        client = TestClient(registry_app.app)
        r = client.post(
            "/api/registry/smart_resume/schedule",
            json={"entry_name": "dream-work-cycle",
                  "buffer_seconds": 60},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["entry_name"] == "dream-work-cycle"
        assert body["reset_source"] == "api-header"
        # Fire = reset + 60s.
        assert quota_probe.parse_iso(
            body["fire_at_ts"]
        ) == quota_probe.parse_iso(
            body["reset_source_ts"]
        ) + timedelta(seconds=60)

        # Subsequent GET surfaces the pending row.
        r2 = client.get("/api/registry/smart_resume")
        assert r2.status_code == 200
        pending = r2.json()["pending"]
        assert len(pending) == 1
        assert pending[0]["entry_name"] == "dream-work-cycle"

        # status endpoint matches.
        r3 = client.get(
            "/api/registry/smart_resume/status/dream-work-cycle"
        )
        assert r3.status_code == 200
        assert r3.json()["has_pending"] is True

    def test_cancel_endpoint(self, tmp_path, monkeypatch):
        import app as registry_app
        db = tmp_path / "registry.db"
        smart_resume._ensure_schema(db)
        fake = FakeScheduler()
        smart_resume.schedule_resume(
            "dream-work-cycle",
            "2099-01-01T00:01:00+00:00",
            reset_source_ts="2099-01-01T00:00:00+00:00",
            reset_source="api-header",
            db_path=db, scheduler=fake,
        )
        monkeypatch.setattr(smart_resume, "default_scheduler",
                             lambda: fake)
        monkeypatch.setattr(registry_app, "REGISTRY_DB", db)
        client = TestClient(registry_app.app)
        r = client.post("/api/registry/smart_resume/cancel",
                         json={"entry_name": "dream-work-cycle"})
        assert r.status_code == 200
        assert r.json()["cancelled"] is True
        row = smart_resume.get_pending(db, "dream-work-cycle")
        assert row["status"] == "cancelled"

    def test_mark_fired_endpoint(self, tmp_path, monkeypatch):
        import app as registry_app
        db = tmp_path / "registry.db"
        smart_resume._ensure_schema(db)
        fake = FakeScheduler()
        smart_resume.schedule_resume(
            "dream-work-cycle",
            "2099-01-01T00:01:00+00:00",
            reset_source_ts="2099-01-01T00:00:00+00:00",
            reset_source="api-header",
            db_path=db, scheduler=fake,
        )
        monkeypatch.setattr(smart_resume, "default_scheduler",
                             lambda: fake)
        monkeypatch.setattr(registry_app, "REGISTRY_DB", db)
        client = TestClient(registry_app.app)
        r = client.post("/api/registry/smart_resume/mark_fired",
                         json={"entry_name": "dream-work-cycle"})
        assert r.status_code == 200
        assert r.json()["status"] == "fired"
        assert smart_resume.get_pending(db, "dream-work-cycle")[
            "status"
        ] == "fired"


# ---------------------------------------------------------------------------
# DONE-WHEN smoke: 24h-shaped trace -- one fire, zero polling
# ---------------------------------------------------------------------------

class TestDoneWhen:
    def test_no_extra_firings_during_locked_window(self, db_path, scheduler):
        """The whole point of AR-S3k: during a locked-out window the
        registry installs ONE one-shot and STOPS. No fixed-interval
        polling, no spawn attempts during the window."""
        # 24h window starting "now". The simulated reset is 5h out.
        now = datetime(2099, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        reset = now + timedelta(hours=5)
        smart_resume.schedule_resume(
            "dream-work-cycle",
            (reset + timedelta(seconds=60)).isoformat(),
            reset_source_ts=reset.isoformat(),
            reset_source="api-header",
            db_path=db_path, scheduler=scheduler,
        )
        # Across the 24h window, the registry has exactly ONE install
        # and ZERO cancels (no "fire every 30 min" pattern).
        assert len(scheduler.installs) == 1
        assert scheduler.cancels == []
        # And there's only one pending row -- not a queue of speculative
        # fires.
        assert len(smart_resume.list_pending(db_path, status="pending")) == 1
