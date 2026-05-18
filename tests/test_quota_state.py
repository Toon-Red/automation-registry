"""AR-S3k phase A tests -- quota_state module + /api/registry/quota endpoints."""
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

import quota_state as qs


# ---------------------------------------------------------------------------
# Module-level: parse / write / load / is_locked_out
# ---------------------------------------------------------------------------

class TestParseIso:
    def test_z_suffix_utc(self):
        dt = qs._parse_iso("2026-05-16T22:35:00Z")
        assert dt.tzinfo is not None
        assert dt.year == 2026 and dt.hour == 22

    def test_offset_suffix(self):
        dt = qs._parse_iso("2026-05-16T18:35:00-04:00")
        # Stored normalised UTC -> 22:35
        assert dt.hour == 22

    def test_naive_rejected(self):
        with pytest.raises(qs.QuotaStateError, match="timezone"):
            qs._parse_iso("2026-05-16T22:35:00")

    def test_garbage_rejected(self):
        with pytest.raises(qs.QuotaStateError, match="ISO 8601"):
            qs._parse_iso("tomorrow")

    def test_empty_rejected(self):
        with pytest.raises(qs.QuotaStateError, match="non-empty"):
            qs._parse_iso("")


class TestWriteKind:
    def test_write_session(self, tmp_path):
        p = tmp_path / "quota.json"
        out = qs.write_kind(p, "session", "2026-05-16T22:35:00Z")
        assert "session" in out
        assert out["session"]["reset_at"] == "2026-05-16T22:35:00Z"
        assert "set_at" in out["session"]
        # File on disk matches.
        assert json.loads(p.read_text(encoding="utf-8")) == out

    def test_write_preserves_other_kind(self, tmp_path):
        p = tmp_path / "quota.json"
        qs.write_kind(p, "session", "2026-05-16T22:35:00Z")
        out = qs.write_kind(p, "weekly", "2026-05-19T21:00:00Z")
        assert "session" in out and "weekly" in out
        assert out["session"]["reset_at"] == "2026-05-16T22:35:00Z"
        assert out["weekly"]["reset_at"] == "2026-05-19T21:00:00Z"

    def test_write_overwrites_same_kind(self, tmp_path):
        p = tmp_path / "quota.json"
        qs.write_kind(p, "session", "2026-05-16T22:35:00Z")
        out = qs.write_kind(p, "session", "2026-05-17T01:00:00Z")
        assert out["session"]["reset_at"] == "2026-05-17T01:00:00Z"

    def test_write_bad_kind(self, tmp_path):
        with pytest.raises(qs.QuotaStateError, match="window_kind"):
            qs.write_kind(tmp_path / "q.json", "monthly", "2026-05-16T22:35:00Z")

    def test_write_bad_timestamp(self, tmp_path):
        with pytest.raises(qs.QuotaStateError, match="ISO 8601"):
            qs.write_kind(tmp_path / "q.json", "session", "tomorrow")

    def test_atomic_write_creates_no_tmp_residue(self, tmp_path):
        p = tmp_path / "quota.json"
        qs.write_kind(p, "session", "2026-05-16T22:35:00Z")
        leftover = list(tmp_path.glob("*.tmp"))
        assert leftover == [], f"unexpected tmp residue: {leftover}"


class TestLoadState:
    def test_missing_file_returns_none(self, tmp_path):
        assert qs.load_state(tmp_path / "absent.json") is None

    def test_round_trip(self, tmp_path):
        p = tmp_path / "q.json"
        written = qs.write_kind(p, "session", "2026-05-16T22:35:00Z")
        assert qs.load_state(p) == written


class TestIsLockedOut:
    def test_none_state(self):
        assert qs.is_locked_out(None, "session") is False

    def test_missing_kind(self, tmp_path):
        p = tmp_path / "q.json"
        qs.write_kind(p, "session", "2026-05-16T22:35:00Z")
        assert qs.is_locked_out(qs.load_state(p), "weekly") is False

    def test_future_reset_locked(self):
        now = datetime.now(timezone.utc)
        future = (now + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        state = {"session": {"reset_at": future, "set_at": "x"}}
        assert qs.is_locked_out(state, "session") is True

    def test_past_reset_not_locked(self):
        now = datetime.now(timezone.utc)
        past = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        state = {"session": {"reset_at": past, "set_at": "x"}}
        assert qs.is_locked_out(state, "session") is False

    def test_clock_injection(self):
        state = {"session": {"reset_at": "2026-05-16T22:35:00Z", "set_at": "x"}}
        before = datetime(2026, 5, 16, 20, 0, tzinfo=timezone.utc)
        after = datetime(2026, 5, 16, 23, 0, tzinfo=timezone.utc)
        assert qs.is_locked_out(state, "session", now=before) is True
        assert qs.is_locked_out(state, "session", now=after) is False


# ---------------------------------------------------------------------------
# HTTP endpoints: GET + POST /api/registry/quota
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    import app as registry_app
    monkeypatch.setattr(registry_app, "REGISTRY_QUOTA", tmp_path / "quota.json")
    return TestClient(registry_app.app)


class TestQuotaEndpoints:
    def test_get_missing_returns_404(self, client):
        r = client.get("/api/registry/quota")
        assert r.status_code == 404
        assert "error" in r.json()

    def test_post_session_then_get(self, client):
        r = client.post("/api/registry/quota", json={
            "reset_at": "2026-05-16T22:35:00Z", "window_kind": "session",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["session"]["reset_at"] == "2026-05-16T22:35:00Z"
        # GET sees the same state.
        r2 = client.get("/api/registry/quota")
        assert r2.status_code == 200
        assert r2.json() == body

    def test_post_idempotent_overwrite(self, client):
        client.post("/api/registry/quota", json={
            "reset_at": "2026-05-16T22:35:00Z", "window_kind": "session",
        })
        r = client.post("/api/registry/quota", json={
            "reset_at": "2026-05-17T01:00:00Z", "window_kind": "session",
        })
        assert r.json()["session"]["reset_at"] == "2026-05-17T01:00:00Z"

    def test_post_preserves_other_kind(self, client):
        client.post("/api/registry/quota", json={
            "reset_at": "2026-05-16T22:35:00Z", "window_kind": "session",
        })
        r = client.post("/api/registry/quota", json={
            "reset_at": "2026-05-19T21:00:00Z", "window_kind": "weekly",
        })
        body = r.json()
        assert body["session"]["reset_at"] == "2026-05-16T22:35:00Z"
        assert body["weekly"]["reset_at"] == "2026-05-19T21:00:00Z"

    def test_post_bad_json_400(self, client):
        r = client.post("/api/registry/quota", data="not json",
                        headers={"Content-Type": "application/json"})
        assert r.status_code == 400

    def test_post_bad_kind_400(self, client):
        r = client.post("/api/registry/quota", json={
            "reset_at": "2026-05-16T22:35:00Z", "window_kind": "monthly",
        })
        assert r.status_code == 400
        assert "window_kind" in r.json()["error"]

    def test_post_bad_timestamp_400(self, client):
        r = client.post("/api/registry/quota", json={
            "reset_at": "tomorrow afternoon", "window_kind": "session",
        })
        assert r.status_code == 400
        assert "ISO 8601" in r.json()["error"]

    def test_post_naive_timestamp_400(self, client):
        r = client.post("/api/registry/quota", json={
            "reset_at": "2026-05-16T22:35:00", "window_kind": "session",
        })
        assert r.status_code == 400
        assert "timezone" in r.json()["error"]
