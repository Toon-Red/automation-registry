# AR-S3h: Limit-aware auto-resume — research conclusion

**Status:** research-only (cancelled the in-process timer build)
**Date:** 2026-05-14
**Reframe origin:** [`2026-05-13-parallel-autopilot-investigation.md`](../../../pipeline-dashboard/docs/audits/2026-05-13-parallel-autopilot-investigation.md)

## Question

Does the L-complexity `claude_loop_continuous` backend (an in-process timer that watches token budget, pauses near the quota wall, and auto-resumes on the next window) need to be built?

## Conclusion

**No.** Dream Auto's existing cron-polled fresh-spawn pattern is sufficient for the foreseeable future. The in-process build is cancelled.

## Evidence

1. **Each fire spawns a fresh subprocess with a fresh quota window.** `dream/orchestrator.py:run_auto` is invoked by a Windows schtask every 30 minutes. There is no parent session to inherit a saturated quota from. A blown quota in cycle N has zero effect on cycle N+1 — the schtask fires regardless of what happened inside the previous subprocess.

2. **The per-task lock prevents racing now that we have a parallel Dispatch session.** Shipped:
   - PD primitive: `pipeline-dashboard@c44d761` (claim/heartbeat/release, 10-min stale takeover)
   - Dream orchestrator integration: `dream@f5fa032` (claim around swarm + agent paths, degraded-mode fail-open)
   So the "two actors might pick up the same task" failure mode the in-process build was meant to obviate is already neutralised.

3. **30-min cadence is well below the work-cycle horizon.** A typical task lasts minutes to hours. A 30-min upper bound on "time between fires" is finer than the granularity Preston needs.

4. **Self-heal travels with the existing run_morning + run_eod hooks** and the per-task lock — no separate watchdog needed.

## What we'd build only if a specific gap surfaces

The in-process limit-aware resume only becomes worth building if one of these happens:

- A real Preston-observed scenario where the 30-min schtask cadence is too coarse (e.g., a task that gets blocked, would otherwise resume in 5 minutes, and the 25-minute wait costs real money or causes a regression).
- A real Preston-observed scenario where the spawn-per-fire cost (subprocess startup, fresh context load) becomes the binding constraint on throughput.

Neither has surfaced. Track them implicitly through the parallel-autopilot followup audit; revisit this proposal only if the audit flags one.

## What this leaves in the schema

`claude_loop_continuous` stays in `SUPPORTED_MECHANISMS` (schema v2). The mechanism is reserved for the eventual in-process build but ships unused. No handler module is required; nothing dispatches to it.

## What replaces the original AR-S3h deliverable

The Dream Auto schtask, catalogued in `automations.yaml` as an `external: true` cron entry by AR-S3e (sibling task). The registry sees Dream Auto exists; it does not manage its lifecycle.
