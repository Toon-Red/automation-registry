"""Tests for state_dsl.evaluate (AR-S3g state-aware predicate evaluator)."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import state_dsl


NOW = datetime(2026, 5, 14, 9, 30)  # 2026-05-14 09:30 local
TODAY = "2026-05-14"
YESTERDAY = "2026-05-13"


class TestEqualityPredicates:
    def test_date_equals_today_true(self):
        state = {"last_sod_date": TODAY}
        assert state_dsl.evaluate("last_sod_date == today", state, now=NOW) is True

    def test_date_equals_today_false(self):
        state = {"last_sod_date": YESTERDAY}
        assert state_dsl.evaluate("last_sod_date == today", state, now=NOW) is False

    def test_date_not_equals_today(self):
        state = {"last_sod_date": YESTERDAY}
        assert state_dsl.evaluate("last_sod_date != today", state, now=NOW) is True

    def test_missing_date_is_null(self):
        # last_sod_date absent from state -> None.
        assert state_dsl.evaluate("last_sod_date == null", {}, now=NOW) is True
        assert state_dsl.evaluate("last_sod_date != today", {}, now=NOW) is True

    def test_yesterday_value(self):
        state = {"last_sod_date": YESTERDAY}
        assert state_dsl.evaluate("last_sod_date == yesterday", state, now=NOW) is True


class TestHourComparisons:
    def test_hour_ge(self):
        assert state_dsl.evaluate("hour >= 9", {}, now=NOW) is True
        assert state_dsl.evaluate("hour >= 10", {}, now=NOW) is False

    def test_hour_lt(self):
        assert state_dsl.evaluate("hour < 17", {}, now=NOW) is True
        assert state_dsl.evaluate("hour < 9", {}, now=NOW) is False


class TestBooleanComposition:
    def test_and(self):
        state = {"last_sod_date": TODAY, "last_eod_date": YESTERDAY}
        expr = "last_sod_date == today AND last_eod_date != today AND hour >= 9"
        assert state_dsl.evaluate(expr, state, now=NOW) is True

    def test_and_short_circuit_false(self):
        state = {"last_sod_date": YESTERDAY}
        expr = "last_sod_date == today AND hour >= 9"
        assert state_dsl.evaluate(expr, state, now=NOW) is False

    def test_or(self):
        state = {"last_sod_date": TODAY}
        expr = "last_sod_date == today OR hour >= 23"
        assert state_dsl.evaluate(expr, state, now=NOW) is True

    def test_or_both_false(self):
        state = {"last_sod_date": YESTERDAY}
        expr = "last_sod_date == today OR hour >= 23"
        assert state_dsl.evaluate(expr, state, now=NOW) is False


class TestErrors:
    def test_empty_expr_rejected(self):
        with pytest.raises(state_dsl.DslError, match="non-empty"):
            state_dsl.evaluate("", {}, now=NOW)

    def test_unknown_ident_rejected(self):
        with pytest.raises(state_dsl.DslError, match="unknown ident"):
            state_dsl.evaluate("rogue_field == today", {}, now=NOW)

    def test_bad_value_rejected(self):
        with pytest.raises(state_dsl.DslError, match="value"):
            state_dsl.evaluate("last_sod_date == garbage", {}, now=NOW)

    def test_hour_requires_int(self):
        with pytest.raises(state_dsl.DslError, match="hour"):
            state_dsl.evaluate("hour >= today", {}, now=NOW)

    def test_order_compare_on_date_rejected(self):
        with pytest.raises(state_dsl.DslError):
            state_dsl.evaluate("last_sod_date > 0", {}, now=NOW)


class TestRealWorldExpressions:
    def test_sod_predicate(self):
        """Schema v2 example: 'fires when SOD not yet run today.'"""
        expr = "last_sod_date != today"
        assert state_dsl.evaluate(expr, {}, now=NOW) is True
        assert state_dsl.evaluate(expr, {"last_sod_date": TODAY}, now=NOW) is False

    def test_eod_predicate(self):
        """Schema v2 example: 'fires when SOD ran today AND hour>=17 AND EOD not yet.'"""
        expr = "last_sod_date == today AND hour >= 17 AND last_eod_date != today"
        now_eod = datetime(2026, 5, 14, 18, 0)
        state = {"last_sod_date": TODAY}
        assert state_dsl.evaluate(expr, state, now=now_eod) is True
        # Same state but earlier in day -> False (hour < 17).
        assert state_dsl.evaluate(expr, state, now=NOW) is False
        # EOD already done today -> False.
        state["last_eod_date"] = TODAY
        assert state_dsl.evaluate(expr, state, now=now_eod) is False
