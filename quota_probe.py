"""Quota probe + state persistence for claude_loop_continuous (AR-S3h).

Per `docs/proposals/AR-S3-Q-MAIN-headers-findings.md`:

Anthropic documents these headers on every successful POST /v1/messages
response:

  - ``anthropic-ratelimit-tokens-reset`` (RFC 3339)
  - ``anthropic-ratelimit-tokens-remaining`` (int)
  - ``anthropic-ratelimit-requests-reset`` (RFC 3339)
  - ``anthropic-ratelimit-requests-remaining`` (int)

The cheapest probe is a tiny ``POST /v1/messages`` with ``max_tokens=1``;
no documented free endpoint returns these headers. We use stdlib
``urllib`` to avoid pulling in the ``anthropic`` SDK as a runtime dep --
the loop runtime should boot without any third-party package beyond
what FastAPI already pulls in.

Two-timer model finding (Preston, 2026-05-14): the documented headers
reflect ORG-LEVEL minute-scale limits; the Pro/Max 5h session + weekly
caps are NOT API-exposed. AR-S3h ships with three pluggable sources;
the handler picks the EARLIEST credible reset boundary:

  (a) ``operator-configured``  -- a flat-file timer the operator sets
      (path is ``data/quota_operator.json`` by default). Always
      consulted; if absent the source is silent.
  (b) ``scraped``              -- placeholder hook so a future DOM
      scraper / MCP relay can drop a value into the same flat file.
      Same source-of-truth as (a) by design.
  (c) ``api-header``           -- the probe in this module. Always
      consulted when an API key is configured.

This module is import-cheap (no network on import) and side-effect free
unless you call ``probe_quota`` or ``read_state`` / ``write_state``.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable


log = logging.getLogger("automation-registry.quota")

ROOT = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = ROOT / "data" / "quota_state.json"
DEFAULT_OPERATOR_PATH = ROOT / "data" / "quota_operator.json"

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_PROBE_MODEL = "claude-3-5-haiku-latest"


class QuotaProbeError(RuntimeError):
    """Raised when the probe cannot establish a reset boundary at all
    (no API key, no operator file, and the network call failed)."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class QuotaState:
    """Snapshot of remaining quota + next-reset moment, persisted to
    ``data/quota_state.json``.

    ``source`` tells the loop runtime which of the three pluggable
    sources produced ``next_reset_ts``. The loop logs this so the
    operator can tell whether the resume timer is anchored on the
    header (subject to the two-timer-model caveat) or on the operator
    file.
    """
    next_reset_ts: str          # RFC 3339 / ISO-8601
    source: str                 # 'api-header' | 'operator' | 'scraped' | 'fallback'
    tokens_remaining: int | None = None
    requests_remaining: int | None = None
    api_tokens_reset_ts: str | None = None
    api_requests_reset_ts: str | None = None
    operator_reset_ts: str | None = None
    checked_at: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def read_state(path: Path | str = DEFAULT_STATE_PATH) -> QuotaState | None:
    """Read the persisted quota state. Returns ``None`` if the file is
    missing or unreadable -- callers treat that as 'no probe yet'."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("read_state(%s) failed: %s", p, exc)
        return None
    return QuotaState(
        next_reset_ts=data.get("next_reset_ts", ""),
        source=data.get("source", "fallback"),
        tokens_remaining=data.get("tokens_remaining"),
        requests_remaining=data.get("requests_remaining"),
        api_tokens_reset_ts=data.get("api_tokens_reset_ts"),
        api_requests_reset_ts=data.get("api_requests_reset_ts"),
        operator_reset_ts=data.get("operator_reset_ts"),
        checked_at=data.get("checked_at", ""),
        notes=list(data.get("notes") or []),
    )


def write_state(state: QuotaState, *,
                 path: Path | str = DEFAULT_STATE_PATH) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(state.as_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def read_operator_timer(path: Path | str = DEFAULT_OPERATOR_PATH
                          ) -> str | None:
    """Return the operator-configured next-reset timestamp (RFC 3339) or
    None. Source of truth for the (a) and (b) modes from the design doc.

    File shape:
        {"next_reset_ts": "2026-05-14T22:00:00-07:00",
         "notes": "5h session, set at 17:00 by operator"}
    """
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    ts = data.get("next_reset_ts")
    return ts if isinstance(ts, str) and ts else None


def write_operator_timer(next_reset_ts: str, *, notes: str = "",
                          path: Path | str = DEFAULT_OPERATOR_PATH) -> None:
    """Operator helper -- writes the manual subscription timer file."""
    # Validate the timestamp parses; raises ValueError on bad input.
    parse_iso(next_reset_ts)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {"next_reset_ts": next_reset_ts, "notes": notes},
            indent=2, sort_keys=True,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat(timespec="seconds")


def parse_iso(ts: str) -> datetime:
    """Parse an RFC 3339 / ISO-8601 timestamp. Accepts the trailing 'Z'
    suffix that the Anthropic docs use."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def _earliest(*candidates: str | None) -> str | None:
    """Return the EARLIEST of the supplied RFC 3339 strings, ignoring
    any that are None or unparseable. None if no candidate parses."""
    parsed: list[tuple[datetime, str]] = []
    for c in candidates:
        if not c:
            continue
        try:
            parsed.append((parse_iso(c), c))
        except ValueError:
            continue
    if not parsed:
        return None
    parsed.sort(key=lambda t: t[0])
    return parsed[0][1]


# ---------------------------------------------------------------------------
# API probe
# ---------------------------------------------------------------------------

def _build_probe_request(api_key: str,
                          model: str = DEFAULT_PROBE_MODEL
                          ) -> urllib.request.Request:
    body = json.dumps({
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "_"}],
    }).encode("utf-8")
    return urllib.request.Request(
        ANTHROPIC_API_URL,
        data=body,
        method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
    )


def _call_api(api_key: str, *,
               opener: Callable | None = None,
               model: str = DEFAULT_PROBE_MODEL,
               timeout: float = 10.0
               ) -> dict[str, str]:
    """Issue the tiny probe call, return the response headers as a
    flat dict. Raises QuotaProbeError on any failure."""
    req = _build_probe_request(api_key, model=model)
    try:
        if opener is None:
            response = urllib.request.urlopen(req, timeout=timeout)
        else:
            response = opener(req, timeout=timeout)
        with response as r:
            # We don't care about the body; the headers carry the data.
            r.read()
            return {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as exc:
        # Even 4xx responses carry rate-limit headers, but if the API
        # rejected the key entirely there's nothing useful to read.
        headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        if "anthropic-ratelimit-tokens-reset" in headers:
            return headers
        raise QuotaProbeError(
            f"api probe HTTPError {exc.code}: {exc.reason}"
        ) from exc
    except (urllib.error.URLError, ConnectionError,
             TimeoutError, OSError) as exc:
        raise QuotaProbeError(f"api probe failed: {exc!s}") from exc


def _state_from_headers(headers: dict[str, str], *,
                         operator_ts: str | None = None
                         ) -> QuotaState:
    tokens_reset = headers.get("anthropic-ratelimit-tokens-reset")
    requests_reset = headers.get("anthropic-ratelimit-requests-reset")
    tokens_remaining = headers.get("anthropic-ratelimit-tokens-remaining")
    requests_remaining = headers.get("anthropic-ratelimit-requests-remaining")

    # Pick the earliest of all known boundaries -- the binding constraint
    # is the one that fires first.
    next_reset = _earliest(tokens_reset, requests_reset, operator_ts)
    if operator_ts and next_reset == operator_ts:
        source = "operator"
    elif next_reset is None:
        # Nothing parseable; fall back to a conservative 1h window so
        # the loop still has SOMETHING to schedule against.
        next_reset = (_now_utc() + timedelta(hours=1)).isoformat(
            timespec="seconds"
        )
        source = "fallback"
    else:
        source = "api-header"

    return QuotaState(
        next_reset_ts=next_reset,
        source=source,
        tokens_remaining=_to_int(tokens_remaining),
        requests_remaining=_to_int(requests_remaining),
        api_tokens_reset_ts=tokens_reset,
        api_requests_reset_ts=requests_reset,
        operator_reset_ts=operator_ts,
        checked_at=_now_iso(),
    )


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def probe_quota(*, api_key: str | None = None,
                 operator_path: Path | str = DEFAULT_OPERATOR_PATH,
                 state_path: Path | str = DEFAULT_STATE_PATH,
                 opener: Callable | None = None,
                 model: str = DEFAULT_PROBE_MODEL,
                 ) -> QuotaState:
    """One-shot probe used at loop session start.

    Resolution order, EARLIEST boundary wins:
      1. ``operator_path`` flat file (modes (a) + (b)).
      2. Anthropic API headers via tiny POST (mode (c)).

    If no API key is supplied AND no operator file exists, falls back
    to a conservative 1h window with ``source='fallback'``. The loop
    runtime should log this and the operator should drop a file at
    ``operator_path`` ASAP.

    Persists the resulting state to ``state_path`` and returns it.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    operator_ts = read_operator_timer(operator_path)

    if api_key:
        try:
            headers = _call_api(api_key, opener=opener, model=model)
            state = _state_from_headers(headers, operator_ts=operator_ts)
        except QuotaProbeError as exc:
            log.warning("api probe failed (%s); falling back to operator/"
                         "default", exc)
            state = _state_without_api(operator_ts,
                                         note=f"api probe failed: {exc!s}")
    else:
        state = _state_without_api(operator_ts,
                                     note="ANTHROPIC_API_KEY not set")
    write_state(state, path=state_path)
    return state


def _state_without_api(operator_ts: str | None, *,
                        note: str) -> QuotaState:
    if operator_ts:
        return QuotaState(
            next_reset_ts=operator_ts,
            source="operator",
            operator_reset_ts=operator_ts,
            checked_at=_now_iso(),
            notes=[note],
        )
    # No source at all -- conservative 1h fallback so the daemon still
    # has a wake-up moment.
    fallback = (_now_utc() + timedelta(hours=1)).isoformat(timespec="seconds")
    return QuotaState(
        next_reset_ts=fallback,
        source="fallback",
        checked_at=_now_iso(),
        notes=[note, "fallback=now+1h (no operator file, no api key)"],
    )


# ---------------------------------------------------------------------------
# Pause/resume decision helpers
# ---------------------------------------------------------------------------

def should_pause(state: QuotaState, *, pause_at_remaining_pct: float = 5.0,
                  initial_tokens: int | None = None) -> bool:
    """Has the loop hit its safety margin?

    Per AR-S3-limit-aware-resume.md Q-E: default 5% remaining. Without
    a known ``initial_tokens`` baseline the percentage is unmeasurable,
    so we fall back to "pause if tokens_remaining <= 0".
    """
    remaining = state.tokens_remaining
    if remaining is None:
        return False
    if initial_tokens and initial_tokens > 0:
        return (remaining / initial_tokens * 100.0) <= pause_at_remaining_pct
    return remaining <= 0


def seconds_until_reset(state: QuotaState, *,
                          now: datetime | None = None) -> int:
    """Seconds from ``now`` (default: real now-UTC) until the next
    reset boundary. Negative if the reset is in the past (which means
    the resume check should fire immediately)."""
    now = now or _now_utc()
    try:
        reset = parse_iso(state.next_reset_ts)
    except ValueError:
        return 0
    delta = reset - now
    return int(delta.total_seconds())
