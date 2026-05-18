"""Operator-anchored quota state (AR-S3k phase A).

Stores the operator-reported reset timestamps for the Pro/Max subscription
windows that aren't exposed via API headers:

  * ``session`` -- the rolling ~5h Pro/Max session window. UI shows
    "Resets in X hr Y min".
  * ``weekly``  -- the weekly absolute reset. UI shows "Resets Tue 9:00 PM".

When the operator hits a limit and the UI surfaces the reset countdown,
they (or a UI button that posts on their behalf) record the timestamp
here. Dream Auto reads the file at startup, schedules its next fire for
``reset_at + 60s``, and exits -- no useless cycles inside a locked-out
window.

Persistence shape (``data/quota_state.json``)::

    {
      "session": {"reset_at": "2026-05-16T22:35:00Z",
                  "set_at":   "2026-05-16T18:14:00Z"},
      "weekly":  {"reset_at": "2026-05-19T21:00:00Z",
                  "set_at":   "2026-05-13T18:00:00Z"}
    }

Either key may be absent if that window has never been recorded. Reads
on a missing file return ``None``; the caller decides whether absent
state is "fail-open (proceed)" or "block" -- for Dream Auto it's the
former.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

VALID_KINDS: tuple[str, ...] = ("session", "weekly")


class QuotaStateError(ValueError):
    """Raised when input cannot be parsed / validated."""


def _parse_iso(ts: str) -> datetime:
    """Strict RFC 3339 / ISO 8601 parse with timezone required.

    Accepts ``Z`` as a UTC marker (Python 3.11+ supports this natively in
    ``fromisoformat``). Rejects naive timestamps -- the operator must say
    which clock the reset is on.
    """
    if not isinstance(ts, str) or not ts:
        raise QuotaStateError("reset_at must be a non-empty ISO 8601 string")
    # Python's fromisoformat is strict about format. Accept 'Z' shorthand.
    candidate = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise QuotaStateError(f"reset_at not a valid ISO 8601 timestamp: {ts!r}") from exc
    if dt.tzinfo is None:
        raise QuotaStateError(
            f"reset_at must include a timezone (got naive {ts!r}); use Z or +HH:MM"
        )
    return dt.astimezone(timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state(state_path: Path | str) -> dict | None:
    """Return the stored state dict, or None if the file doesn't exist.

    No mojibake / heal pass needed -- this file is operator-written and
    contains only ASCII timestamps + dict keys.
    """
    p = Path(state_path)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def write_kind(state_path: Path | str, kind: str, reset_at_iso: str) -> dict:
    """Record a reset timestamp for one window. Returns the updated state.

    Atomic via tmp + os.replace so a crash mid-write can't corrupt the
    file. Other window's state is preserved (idempotent on the unaffected
    kind).
    """
    if kind not in VALID_KINDS:
        raise QuotaStateError(
            f"window_kind must be one of {VALID_KINDS}, got {kind!r}"
        )
    parsed = _parse_iso(reset_at_iso)
    # Normalise to Z-suffixed UTC for storage so downstream comparisons
    # are unambiguous.
    canonical = parsed.strftime("%Y-%m-%dT%H:%M:%SZ")

    p = Path(state_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    existing = load_state(p) or {}
    existing[kind] = {"reset_at": canonical, "set_at": _now_iso()}

    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(p))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return existing


def is_locked_out(state: dict | None, kind: str, *, now: datetime | None = None) -> bool:
    """Return True if the given window's reset is in the future.

    Helper for consumers (Dream orchestrator) so they don't reimplement
    the comparison. ``None`` state or missing kind -> not locked out
    (graceful default).
    """
    if not state or kind not in state:
        return False
    reset_str = state[kind].get("reset_at")
    if not reset_str:
        return False
    try:
        reset = _parse_iso(reset_str)
    except QuotaStateError:
        return False
    return reset > (now or datetime.now(timezone.utc))
