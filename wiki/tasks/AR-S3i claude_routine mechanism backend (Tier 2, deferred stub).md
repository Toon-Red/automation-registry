---
tags: [task, todo, feature]
project: [[projects/Automation Registry]]
status: todo
priority: low
updated: 2026-05-14 13:38
---

# AR-S3i: claude_routine mechanism backend (Tier 2, deferred stub)

⬜ **Todo**  ·  `feature`  ·  priority: `low`

**Project:** [[Automation Registry]]

## Description

WHAT: Implement the `claude_routine` mechanism backend -- writes entries into Anthropic-cloud-side Routines via the `schedule` skill / Routines API. Tier 2 (cloud).

WHY: For tasks that can run without local-host reach (GitHub events via connectors, Slack-driven workflows, cross-machine durability). Currently NO use case filed -- this task is the documented stub so when one shows up the backend lands quickly.

DEFERRED: no implementation today; just the spec + a placeholder validator path. Per the v2 schema doc: claude_routine requires non-null schedule (1h minimum), respects the Pro 5/day / Max 15/day cap (consumes subscription usage, not extra cost), uses target_kind: claude_prompt for the body.

HOW (when activated):
1. Schema v2 already accepts the mechanism enum value -- validator path can ship now.
2. Backend creates the Routine via the schedule skill (cloud-side).
3. Idempotent reconcile pattern matches AR-S3b.
4. Tests: validator + mocked Routines client.

DONE WHEN: a real cloud-side use case is filed AND the backend ships + tests pass.

DEPS: AR-S3b (reconcile pattern).

OPEN: needs Preston to surface a routine-shaped use case (Q-PRESTON in the schedule eval doc).

*Auto-generated 2026-05-14 13:38*
