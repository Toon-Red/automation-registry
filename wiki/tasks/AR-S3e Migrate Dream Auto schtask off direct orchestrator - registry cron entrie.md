---
tags: [task, todo, infra]
project: [[projects/Automation Registry]]
status: todo
priority: high
updated: 2026-05-14 13:38
---

# AR-S3e: Migrate Dream Auto schtask off direct orchestrator -> registry cron entries

⬜ **Todo**  ·  `infra`  ·  priority: `high`

**Project:** [[Automation Registry]]

## Description

WHAT: Retire the current `Dream Auto` Windows schtask. Install registry entries that capture its three behaviours under Tier 1 mechanisms (schema v2, revised 2026-05-13):

  - dream-morning-summary -> claude_desktop_scheduled STATE-AWARE (fires on session-open past midnight when SOD not yet run today) -> dream.orchestrator:run_morning
  - dream-eod-review      -> claude_desktop_scheduled STATE-AWARE (fires when SOD ran today AND hour>=17 AND EOD not yet run today) -> dream.orchestrator:run_eod
  - dream-work-cycle      -> claude_loop_continuous = L8-L4 hierarchy runtime (NOT "a Claude in a loop") -> dream.orchestrator:run_work_cycle as the work-item source

The architectural shifts:
  1. SOD/EOD are state transitions, not clock events. Missed yesterday's EOD: SKIPPED, not caught up. State file: data/workflow_state.json.
  2. Work-cycle is the L8-L4 hierarchy operating continuously, not Claude in a fixed-cadence loop. Engines configured per layer.

Self-heal travels inline with run_morning + run_eod (no separate entry).

WHY: Inaugural Tier 1 production migration. Preston 2026-05-14: "run till the limit is hit, or near. This way you actually fully utilize what you can." Plus 2026-05-13 clarification: SOD/EOD are state machines, the loop is the L-hierarchy.

NUANCE FROM AR-S3g IMPLEMENTATION (2026-05-14):
  - Claude Desktop scheduled tasks have NO delete primitive; "uninstall" = update with enabled=false. Migration step 3 ("Disable Dream Auto schtask") follows the same disable-not-delete pattern.
  - The MCP `mcp__scheduled-tasks__*` is SESSION-SCOPED -- the registry process at :5050 cannot invoke it directly. Time-of-day entries land via POST /api/registry/desktop_scheduled/reconcile (returns plan) + operator (or AC-S16 L8 agent) materialises via MCP + POST /api/registry/desktop_scheduled/ack. State-aware entries DO write hooks directly (no MCP needed).
  - For the SOD/EOD entries (state-aware), filesystem hook installation is direct -- no operator-in-the-loop step. For dream-work-cycle (claude_loop_continuous), AR-S3h's runtime is what manages dispatch; no MCP apply needed.
  - Watchdog pattern: a claude_desktop_scheduled (Tier 1) supervises the claude_loop_continuous runtime (Tier 1) -- self-hosting per AR-S3-limit-aware-resume.md Q-F. AR-S3g's backend provides the watchdog primitive.
  - Claude Desktop must be open for scheduled fires; missed fires run on next app launch. Behavioural difference from schtasks which fires regardless of app state. Acceptable per Preston 2026-05-13.

DEPS:
  AR-S3g (2346412d) -- DONE -- claude_desktop_scheduled backend (state-aware flavour)
  AR-S3h (bd24a633) -- BLOCKED -- claude_loop_continuous backend (L8-L4 runtime); gated on subscription-timer gap call
  AR-S3j (e0003968) -- TODO -- /goal integration into loop_continuous
  AC-S16 (4057ba23) -- TODO -- L8 PM component

HOW (5-step reversible plan, see docs/proposals/AR-S3e-dream-auto-migration.md):
  1. Install three Tier 1 entries in automations.yaml. POST /api/registry/reconcile + /api/registry/desktop_scheduled/reconcile.
  2. Smoke-fire one entry manually. For state-aware: open a Claude Code session at automation-registry/, confirm the SessionStart hook fires and updates workflow_state.json. For loop_continuous: launch the L8-L4 stack manually and observe one iteration.
  3. Disable Dream Auto schtask (Disable-ScheduledTask; NOT delete; reversible).
  4. Observe 24h cycle. Confirm Discord shape unchanged, state transitions correct, loop_continuous survives limit reset.
  5. Delete Dream Auto schtask permanently after a clean week.

DONE WHEN:
  - Three Tier 1 entries installed + reconciled.
  - One full SOD/work/EOD cycle observed via the registry path.
  - workflow_state.json correctly tracks last_sod_date / last_eod_date.
  - Dream Auto disabled (and after clean week, deleted).
  - dream/orchestrator.py header notes the migration source-of-truth shift.

NOT IN SCOPE: deleting Dream Auto permanently (separate follow-on); AR-S3f cron playtest (now fallback proof, not blocker).

*Auto-generated 2026-05-14 13:38*
