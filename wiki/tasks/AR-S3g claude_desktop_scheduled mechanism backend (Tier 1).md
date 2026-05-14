---
tags: [task, done, feature]
project: [[projects/Automation Registry]]
status: done
priority: high
updated: 2026-05-14 13:38
---

# AR-S3g: claude_desktop_scheduled mechanism backend (Tier 1)

✅ **Done**  ·  `feature`  ·  priority: `high`

**Project:** [[Automation Registry]]

## Description

WHAT: Implement the `claude_desktop_scheduled` mechanism backend. Two trigger flavours (schema v2, revised 2026-05-13):

(1) TIME-OF-DAY flavour -- traditional cron-style schedule.
(2) STATE-AWARE flavour -- SOD/EOD pattern. Trigger fires when a `fire_when` predicate over `data/workflow_state.json` is true; checked on `after_event: session_open` (Claude Code SessionStart-equivalent).

Per Preston 2026-05-13: SOD/EOD are state transitions, not clock events. SOD always before EOD within a day. Missed yesterday's EOD: SKIPPED, not caught up.

WHY: Tier 1 of schema v2. Time-of-day events use Claude-native Desktop scheduled tasks rather than OS cron. State-aware events use SessionStart-boundary checks against workflow_state.json. Subscription includes both with no extra cost. Requires Claude Desktop be open -- Preston-confirmed.

HOW:
1. New module `automation_registry/mechanisms/desktop_scheduled.py` with reconcile/install/uninstall/status matching cron_handler shape (AR-S3b pattern).
2. Time-of-day flavour: translate entry into Claude Desktop scheduled-task payload via mcp__scheduled-tasks__create_scheduled_task.
3. State-aware flavour: register a SessionStart hook in the target repo's `.claude/settings.json` that consults workflow_state.json + evaluates `fire_when` DSL + invokes the runner if true. State-machine DSL is narrow (equality/inequality on last_<event>_date vs today/yesterday, hour >= / < int, AND/OR), evaluated by the backend -- NOT arbitrary Python eval.
4. Schema validator: time-of-day needs non-null schedule; state-aware needs trigger.kind=state with fire_when + after_event + state_file + on_fire_update.
5. Tests: validator paths, mocked MCP client for create/update, mocked SessionStart-hook invocation, idempotent reconcile, state-machine DSL evaluator unit tests.

DONE WHEN:
- Schema v2 accepts `claude_desktop_scheduled` with both flavours.
- Backend implements the cron_handler interface for both.
- POST /api/registry/reconcile syncs Desktop scheduled tasks alongside cron entries.
- pytest covers happy paths + state DSL + idempotency.

DEPS: AR-S3b (reconcile pattern). Independent of Q-MAIN.

NOT IN SCOPE: claude_loop_continuous (AR-S3h); claude_routine (AR-S3i); the actual Dream Auto migration (AR-S3e blocks on this).

*Auto-generated 2026-05-14 13:38*
