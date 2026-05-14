---
tags: [task, todo, feature]
project: [[projects/Automation Registry]]
status: todo
priority: high
updated: 2026-05-14 13:38
---

# AR-S3h: claude_loop_continuous mechanism backend + limit-aware auto-resume (Tier 1)

⬜ **Todo**  ·  `feature`  ·  priority: `high`

**Project:** [[Automation Registry]]

## Description

WHAT: Implement the `claude_loop_continuous` mechanism. **This is NOT "a Claude instance in a loop." It runs the L8-L4 hierarchy (per AC research c1779970 2026-05-13 amendment + correction) as a continuously-operating stack as long as there is calendar work AND subscription credit.**

Hierarchy (Preston verbatim, corrected 2026-05-13 -- grader is L4 not L5):
  - L4 = gruntwork: coder, qa, playtester, AND grader. Grader is a HIGH-COMPUTE L4 (uses Claude or similar); standard L4s can run on local Ollama. Engine choice is PER-ROLE within the layer.
  - L5 = managers + guides for L4s. Every L4 has an L5 manager (including the grader-L4).
  - L6 = queen (ruflow IS L6)
  - L7 = Dispatch (Preston's operational interface)
  - L8 = Project Manager (Preston's oversight interface) -- AC-S16

WHY: Tier 1 of schema v2 reshape. Backs Preston 2026-05-14: "run till the limit is hit, or near. This way you actually fully utilize what you can." Engine config is PER-ROLE within each layer (not just per-layer) -- mixed-model stack is the norm.

Q-MAIN STATUS (2026-05-14, partial resolution):
  RESOLVED: API rate-limit headers ARE documented at platform.claude.com/docs/en/api/rate-limits.
    - anthropic-ratelimit-tokens-reset (RFC 3339)
    - anthropic-ratelimit-tokens-remaining (int)
    - anthropic-ratelimit-requests-reset (RFC 3339)
    - anthropic-ratelimit-requests-remaining (int)
    - + input/output/priority variants
    Cheapest probe: POST /v1/messages with max_tokens=1.
    No CLI helper; write ~20 LOC stdlib probe.
  GAP: those headers reflect ORG-LEVEL minute-scale limits, NOT Pro/Max session/weekly subscription windows shown in the UI. The two UI timers (5h session + weekly) are undocumented; no /v1/usage / /v1/account / /v1/organizations/usage; Claude Code /status + /cost don't expose them.

  Awaiting Preston's (a)/(b)/(c) call:
    (a) User-configured timer (operator notes 5h reset at session start).
    (b) Scrape Claude Desktop UI for the two timers (brittle).
    (c) Empirically test whether anthropic-ratelimit-tokens-reset actually reflects the 5h session for Pro/Max-billed calls. If yes, original design works. If no, fall back to (a).

  Recommendation: (c) first (zero-cost test). Findings doc:
    automation-registry/docs/proposals/AR-S3-Q-MAIN-headers-findings.md

HOW (per AR-S3-limit-aware-resume.md, REVISED 2026-05-13):
1. Backend module reconcile/install/uninstall/status matching cron_handler shape.
2. Spawns the L8-L4 stack as a managed unit. L8 keeps its own conversation surface with Preston (Discord + Dream UI per AC-S16). Other layers operate within the stack.
3. Detection model: at session start, make a tiny harmless Anthropic API call; read x-ratelimit-* headers; persist to data/quota_state.json. Schedule timer for next reset (subject to (a)/(b)/(c) choice for subscription-window detection).
4. When timer fires: idle check (heartbeat-file pattern per Q-B). Resume if idle; no-op if busy.
5. State on pause/resume (Q-D thin state): active work item ID, paused_at ts. PD is durable record.
6. Safety margin (Q-E): pause at limit_aware.pause_at_remaining_pct (default 5%).
7. Crash recovery (Q-F): paired Claude Desktop scheduled task as watchdog (self-hosting Tier 1 atop Tier 1 -- AR-S3g provides this).
8. Work-item selection (Q-G): today's calendar entries with status=ready, then PD priority queue, then idle.
9. Per-iteration /goal: invoke goal_renderer.render_and_set (AR-S3j) with the entry + the chosen work item BEFORE running the iteration body. The renderer + setter are SHIPPED -- import from goal_renderer; no further design needed here.

DONE WHEN:
- Preston picks (a)/(b)/(c) on subscription timer gap; design updated accordingly.
- L8-L4 stack spawns as a managed unit with per-role engine config.
- A synthetic loop_continuous entry survives a forced rate-limit (mocked 429), pauses cleanly, and resumes on mocked reset.
- Limit-aware + engines fields in schema v2 validated (DONE in AR-S3g schema work).
- Per-iteration /goal rendering wired via goal_renderer (DONE in AR-S3j; just consume here).
- Tests cover pause/resume + heartbeat idle check + watchdog restart.

DEPS:
- AR-S3b (reconcile pattern) -- DONE
- AR-S3g (Desktop scheduled, watchdog uses it) -- DONE
- AR-S3j (/goal integration per iteration) -- DONE
- AC-S10 (multi-engine driver -- resolves engine ids) -- TODO on AC project
- AC-S16 (L8 PM component) -- TODO on AC project

GATED ON: Preston's (a)/(b)/(c) call (subscription timer gap).

NOT IN SCOPE: the actual Dream Auto migration (AR-S3e blocks on this); L8 PM implementation (AC-S16).

*Auto-generated 2026-05-14 13:38*
