# AR-S3k: Smart auto-resume — fire once at limit reset + buffer

**Status:** implemented
**Date:** 2026-05-16
**Refines:** [`AR-S3h`](AR-S3h-resume-research-conclusion.md) (closed 2026-05-14, partially superseded)
**Preston directive (verbatim 2026-05-14):**
> "But not every 5 min or so, but the minute after the reset is hit.
> This way we don't have any unneeded instances going on."

## Why AR-S3h was partially wrong

AR-S3h concluded "Dream Auto's 30-min cron is sufficient." The flaw:

- **Dream Auto's fixed-interval cron fires regardless of whether the limit
  has reset.** Every 30 min schtask fire during a locked-out window burns
  Anthropic quota AND local compute on subprocess spawns that immediately
  bounce on a 429.
- **The schtask was firing 0/3 completed cycles per Preston's screenshot.**
  Pure noise during the lockout.
- **The right cadence is event-driven, not interval-driven.** The
  Anthropic response headers tell us *exactly* when the limit refreshes;
  we should fire a one-shot AT that moment + a small clock-drift buffer
  and NEVER poll in between.

## Approach

The Q-MAIN research (`wiki/research/Q-MAIN -- Is x-ratelimit- reachable
from a Claude Code Stop hook.md`) confirmed `x-ratelimit-*` headers are
reachable. `quota_probe.py` (AR-S3h) already extracts them and persists
the earliest reset boundary to `data/quota_state.json`.

AR-S3k adds the one-shot scheduler on top:

  1. Session start (or any successful Anthropic response) calls
     `quota_probe.probe_quota()` to refresh `data/quota_state.json`.
  2. `smart_resume.apply_from_quota_state(entry_name)` reads that snapshot
     and computes `fire_at = next_reset_ts + buffer_seconds` (default
     60s, absorbing clock drift + provider propagation).
  3. A platform one-shot is installed (Windows `schtasks /Create /SC
     ONCE`, file-marker fallback on POSIX) that invokes `runner.py
     --entry <name>` exactly once.
  4. Any prior pending one-shot for the same entry is cancelled first —
     re-probing simply rolls the timer forward.
  5. On fire, the runner calls `mark_fired` so the OS task is cleaned
     up and the sqlite history reflects reality.
  6. The new session's first response repeats the cycle.

**No fixed-interval polling. No spawns during locked-out windows.**

## Implementation surface

Option (B) from the task spec was chosen: a new handler in
`automation-registry`. The Tier 1 `claude_loop_continuous` mechanism
schema reserved a slot for this work back at AR-S3h.

Files:

- `smart_resume.py` — module with scheduler abstraction, sqlite-backed
  pending table, public surface (`schedule_resume`, `cancel_resume`,
  `mark_fired`, `apply_from_quota_state`, `on_session_start`, `status`,
  `list_pending`).
- `app.py` — five new endpoints under `/api/registry/smart_resume/*`.
- `tests/test_smart_resume.py` — 25 tests covering the scheduler
  contract, OS-task lifecycle, the DONE-WHEN guarantees, and the API
  round-trip.

### Scheduler abstraction

```python
class Scheduler(Protocol):
    name: str
    def install(self, task_id, fire_at_iso, command_argv) -> str: ...
    def cancel(self, task_id) -> None: ...
```

Two concrete backends:

- `WindowsSchTasksScheduler` — `schtasks /Create /SC ONCE /F` install,
  `/Delete /F` cancel. Cancel is idempotent (swallows "task does not
  exist"). Default on Windows.
- `FileMarkerScheduler` — writes `data/pending_resume/<task_id>.json`.
  Used in tests AND on POSIX where the operator drives a tiny `at`
  sidecar of their choosing. Default on non-Windows.

### sqlite schema

```sql
CREATE TABLE smart_resume_pending (
    entry_name        TEXT PRIMARY KEY,
    fire_at_ts        TEXT NOT NULL,
    reset_source_ts   TEXT NOT NULL,
    reset_source      TEXT NOT NULL,
    buffer_seconds    INTEGER NOT NULL,
    scheduled_at      TEXT NOT NULL,
    schedule_id       TEXT NOT NULL,
    backend           TEXT NOT NULL,
    status            TEXT NOT NULL,   -- pending | fired | cancelled
    last_fired_at     TEXT,
    notes             TEXT
);
```

`entry_name` is the PRIMARY KEY so by construction there can be at most
one pending one-shot per entry.

### API endpoints

| Method | Path | Body | Behaviour |
| --- | --- | --- | --- |
| `GET`  | `/api/registry/smart_resume` | — | List `pending` + recent `history` (fired/cancelled). |
| `GET`  | `/api/registry/smart_resume/status/{entry_name}` | — | Compact dashboard chip (`has_pending`, `seconds_until_fire`, `reset_source`). |
| `POST` | `/api/registry/smart_resume/schedule` | `{entry_name, buffer_seconds?}` | Read persisted quota state, install one-shot. |
| `POST` | `/api/registry/smart_resume/cancel` | `{entry_name}` | Cancel pending one-shot (idempotent). |
| `POST` | `/api/registry/smart_resume/mark_fired` | `{entry_name}` | Record that the one-shot fired; clean up OS task. |

## DONE-WHEN evidence

The task spec set three "done when" criteria; the test suite enforces
each:

1. **A session that hits the rate limit auto-resumes within 60s of
   refresh.** Encoded in `TestComputeFireAt.test_adds_buffer` +
   `TestApplyFromQuotaState.test_reads_persisted_state`: `fire_at -
   reset_ts == buffer_seconds`.

2. **Zero fired spawns during a locked-out window across a 24h
   observation.** Encoded in `TestScheduleResume.test_reschedule_cancels_prior`
   (only one OS task survives per re-probe) AND
   `TestDoneWhen.test_no_extra_firings_during_locked_window` (zero
   installs + zero cancels happen between successive probes during a
   single window — no polling cadence at all).

3. **Dream Auto schtask is either retired or kicks once a day as a
   safety net (operator decision).** Out of scope for code — operator
   handles via `schtasks /Change` or by leaving Dream Auto as a single
   daily fire that re-probes if `quota_state.json` is stale. The
   registry catalogs the existing `dream-auto` entry as
   `external: true` so it stays visible without registry lifecycle
   ownership (AR-S3e).

## Cross-references

- AR-S3h research conclusion: `docs/proposals/AR-S3h-resume-research-conclusion.md`
- Q-MAIN x-ratelimit reachability research:
  `wiki/research/Q-MAIN -- Is x-ratelimit- reachable from a Claude Code Stop hook.md`
- Parallel-autopilot audit:
  `pipeline-dashboard/docs/audits/2026-05-13-parallel-autopilot-investigation.md`
- Quota probe shipped under AR-S3h: `quota_probe.py`
