# AR-S3e -- Dream Auto migration scoping (no migration yet)

> **REVISED 2026-05-14 per Preston's tier reshape (schema v2).**
> Migration targets MOVED from cron (Tier 4) to Tier 1 Claude-native
> mechanisms. Old cron-based plan preserved in commit history.
>
> Per Preston's "always playtest, don't fake it" rule + "don't lose
> functionality" rule: production migrations need explicit go-ahead.
> This doc is the plan; execution is gated on Preston's sign-off.

## 2026-05-14 REVISION: targets are now Tier 1

After the Phase 1 subscription-cost verification (Pro/Max cover all
three Claude scheduling features; no extra cost), the migration
targets reshape:

| Old (v1 plan) | New (v2 plan) |
|---|---|
| `dream-morning-summary` cron `0 8 * * *` -> orchestrator:run_morning | `dream-morning-summary` **`claude_desktop_scheduled`** `0 8 * * *` -> run_morning |
| `dream-eod-review` cron `0 18 * * *` -> orchestrator:run_eod | `dream-eod-review` **`claude_desktop_scheduled`** `0 18 * * *` -> run_eod |
| `dream-work-cycle` cron `0 9-17 * * 1-5` -> orchestrator:run_work_cycle | `dream-work-cycle` **`claude_loop_continuous`** -> run_work_cycle (NOT cron-scheduled; continuous until limit) |

The work-cycle change is the meaningful architectural shift:
hourly fires become continuous limit-aware operation. Per Preston:
"run till the limit is hit, or near. This way you actually fully
utilize what you can." The auto-resume infrastructure is part of
the `claude_loop_continuous` backend (see
`AR-S3-limit-aware-resume.md`).

Sub-task changes:
  * **Cron backend (AR-S3a..d) stays as Tier 4 fallback.** Not
    deprecated. Useful for sub-hour / no-Desktop-required cases.
  * **AR-S3e blocks on AR-S3g + AR-S3h** (the Tier 1 backends) --
    those need to ship before this migration can execute. Filed.
  * The 5-step reversible migration plan below remains structurally
    valid; substitute "register new entries" for "POST reconcile"
    once the new backends exist.

The Dream Auto observation: still PT30M cadence, defaults to
`run_auto`, runs morning + cycle-loop + EOD. Migration retires it.
Section "Dream Auto today" below is unchanged.

---

## Dream Auto today -- actual behaviour

Live schtask XML (`schtasks /Query /TN "Dream Auto" /XML`):

  * **Fires every PT30M.**
  * `ExecutionTimeLimit PT15M` -- each fire is killed after 15 min.
  * `MultipleInstancesPolicy IgnoreNew` -- a running fire blocks a new
    one (so the effective cadence is 30 min, not 15).
  * Command: `pythonw.exe "C:/Users/prest/Desktop/code/dream/orchestrator.py"`
    -- no arg, so the entry block at `orchestrator.py:1598` defaults
    to `mode = "auto"` -> `run_auto()` (NOT `run_auto_once`).

What `run_auto()` does (`orchestrator.py:1408`):

| Condition | Action |
|---|---|
| `now < morning_hour` (default 8) | Log + exit. No Discord. |
| `now >= eod_hour` (default 18) | If EOD not done today, `run_eod()` once. Exit. |
| `morning_hour <= now < eod_hour` | If morning not done, `run_morning()` once. Then enter a **cycle loop** -- `run_work_cycle()` every `cycle_interval_seconds` (default 3600s = 1h). Loop continues until EOD hour or 15-min schtask kill, whichever first. |

So in a normal day the schtask fires ~48 times (every 30 min). Most
of those exit immediately because they're outside work hours OR
because `MultipleInstancesPolicy` rejects the new fire. Inside work
hours, the loop is what generates the real activity.

**Three behaviours produce Discord pings:**

| Function | When | Discord shape |
|---|---|---|
| `run_morning` | 08:00 (once) | "Morning Standup" embed -- overdue items + agents assigned. **Self-heal inline** (top of function, line 697). |
| `run_work_cycle` | Hourly during work hours, ONLY if tasks were dispatched | "Work Cycle" embed -- per-task status + quality probe tags. No ping on empty cycles. **Self-heal NOT inline here.** |
| `run_eod` | 18:00 (once) | "EOD Review" embed -- completion rate + carry-over + at-risk goals + action items. **Self-heal inline** (line 1036). |

**Self-heal correction:** earlier framing assumed self-heal fired
every 30 min. That's wrong. `ensure_services()` is called ONLY from
inside `run_morning` and `run_eod`. **It fires twice per day, not
every 30 min.** The "Self-Heal Escalation" Discord noise we saw on
2026-05-12 was the EOD fire failing on calendar-service -- not a
loop. That root cause was fixed in dream commit `0d34db7` (this
session). Self-heal does not need its own registry entry; it
travels with run_morning + run_eod.

## Migration mapping (proposed)

| Dream Auto behaviour today | New registry entry |
|---|---|
| `run_morning` at 08:00 | `dream-morning-summary` cron `0 8 * * *` -> `dream.orchestrator:run_morning` |
| `run_eod` at 18:00 | `dream-eod-review` cron `0 18 * * *` -> `dream.orchestrator:run_eod` |
| `ensure_services` self-heal | No separate entry -- preserved inline within run_morning + run_eod |
| `run_work_cycle` every hour 09-17 inside the loop | **OPEN -- see "Work cycle question" below.** |

## Self-heal disposition

**Recommendation: NONE of the original a/b/c options.** Self-heal is
not an independent behaviour -- it's an inline pre-task step inside
the two functions we're migrating. Migrating those two functions
preserves self-heal automatically. The original question
mis-framed: it assumed self-heal was scheduled separately. It isn't.

## Work cycle question (the real open decision)

What should happen to `run_work_cycle`'s hourly autonomous-dispatch
behaviour after Dream Auto is retired? Three options:

| Option | Description | Trade-off |
|---|---|---|
| **W1** (recommended) | Migrate as `dream-work-cycle` cron `0 9-17 * * 1-5` -> `dream.orchestrator:run_work_cycle` (hourly 09:00-17:00, weekdays). | Preserves current behaviour. Simple. Adds 9 fires/day to the registry surface. |
| W2 | Defer until AR-S4 (`claude_hook` mechanism). Work cycles would only fire when a Claude Code session is open. | Reduces fire count when nobody's around; but loses overnight / weekend autonomous dispatch (Preston explicitly approved overnight AI on limit reset -- conflict). |
| W3 | Drop. No automated work dispatch; operator initiates via UI. | Real functionality loss. Agents only spawn when Preston manually triggers. |

**Recommendation: W1.** Preserves Dream Auto's autonomous work
dispatch with minimum behavioural change. Discord-side this becomes
0-9 "Work Cycle" pings per workday (same as today -- empty cycles
already silent).

## Discord ping shape: before vs after

| Window | Today | After (W1 + this migration) |
|---|---|---|
| `< 08:00` | Silent (schtask exits quickly) | Silent |
| 08:00 | 1 "Morning Standup" embed | 1 "Morning Standup" embed (same shape) |
| 09:00-17:00 | 0-9 "Work Cycle" embeds (only when tasks dispatch) | Same |
| 18:00 | 1 "EOD Review" embed | 1 "EOD Review" embed (same shape) |
| Failures | "Self-Heal Escalation" embed when ensure_services escalates | Same -- self-heal still inline in run_morning/run_eod |
| Registry-side | (n/a) | NEW: `[cron-fail]` PD tasks via AR-S3d if any of the 3 cron entries fail (idempotent dedup; not Discord pings) |

**Net change**: Discord shape unchanged. Registry adds a PD-task
file-trail for failures via AR-S3d's escalation pipeline (which is
the desired upgrade -- failures stop being ephemeral).

## Migration plan (5 reversible steps)

1. **Install new registry entries.** Add 3 entries to
   automation-registry's `automations.yaml`:
   `dream-morning-summary`, `dream-eod-review`,
   `dream-work-cycle`. Restart registry; POST
   `/api/registry/reconcile`. Confirm 3 new
   `Ecosystem-Cron-dream-*` schtasks appear via
   `schtasks /Query /TN Ecosystem-Cron-dream-morning-summary`.
   *Reversible: POST /api/registry/reconcile with the entries
   removed.*
2. **Manual smoke-fire one entry.** Run `python runner.py --entry
   dream-eod-review --manual` from automation-registry's root.
   Confirm Discord ping arrives + sqlite `cron_runs` row written +
   exit 0. *Reversible: this is a no-op past the Discord ping.*
3. **Disable `Dream Auto` schtask** (do NOT delete -- keep as
   fallback): `Disable-ScheduledTask -TaskName "Dream Auto"`.
   *Reversible: `Enable-ScheduledTask -TaskName "Dream Auto"`.*
4. **Observe for one full cycle (24h).** Confirm:
   - 08:00 fire -> Morning Standup Discord embed.
   - During work hours -> Work Cycle pings only when tasks dispatch.
   - 18:00 fire -> EOD Review Discord embed.
   - No "Self-Heal Escalation" pings from this cause.
   - sqlite `cron_runs` shows the expected fires.
   *Reversible: re-enable Dream Auto + disable the new entries.*
5. **Delete `Dream Auto` permanently** after a clean week:
   `Unregister-ScheduledTask -TaskName "Dream Auto" -Confirm:$false`.
   *Reversible: re-install from this doc's `schtasks /Create` XML
   snippet (capture in the executing commit's body).*

## Rollback plan (if step 4 surfaces issues)

  1. `Enable-ScheduledTask -TaskName "Dream Auto"` (re-enable old).
  2. Set the 3 new registry entries' `enabled: false` in
     automations.yaml; POST `/api/registry/reconcile` to uninstall.
  3. Confirm Dream Auto resumes; sqlite has no new cron_runs rows
     for the disabled entries.
  4. File a PD task on `automation-registry` describing what failed
     so AR-S3e can be re-attempted with a fix.

The rollback is fully scripted -- no manual yaml surgery needed in
an emergency.

## What needs Preston's call before executing

**Single open question: the work-cycle decision (W1 / W2 / W3).**

The self-heal question is resolved -- it travels with the functions.
The other migration steps are mechanical once the work-cycle decision
lands.

Claude's recommendation: **W1** (migrate as `dream-work-cycle` cron
on weekdays 09-17). Preserves current autonomous dispatch with the
smallest behavioural delta.

## Cross-references

- Phase A AR-S3 breakdown: `docs/proposals/AR-S3-cron-backend-breakdown.md`
- Schema v1: `docs/proposals/automation-registry-schema-v1.md`
- Research: PD `fe0302b9` (registry decision + REUSE-AS-EXECUTOR).
- PD task: `221ffe14` (AR-S3e umbrella).
