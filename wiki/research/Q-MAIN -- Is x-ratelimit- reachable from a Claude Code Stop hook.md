---
tags: [research, identified, infrastructure]
project: [[projects/Automation Registry]]
status: identified
updated: 2026-05-14 13:38
---

# Q-MAIN -- Is x-ratelimit-* reachable from a Claude Code Stop hook?

🔍 **Identified**  ·  `infrastructure`  ·  priority: `normal`

**Project:** [[Automation Registry]]

## Problem

=== 2026-05-14 PATH DECISION (option (a), probe deferred) ===

Empirical probe (c) could NOT be run from the registry's Claude
Code session: no ANTHROPIC_API_KEY in env (Claude Code uses
OAuth-managed subscription auth, not raw API keys); internal
response headers aren't exposed to user code.

PATH FORWARD: adopt option (a) -- user-configured timer.
  - Operator (or AC-S16's L8) notes next-session-reset from
    Claude UI Usage panel; posts to registry via
    POST /api/registry/quota (new endpoint).
  - Registry persists to data/quota_state.json; AR-S3h's loop
    runtime reads at startup and schedules timer accordingly.
  - Manual refresh once per 5h session window.
  - Preston's note baked in: weekly cap is UI-only +
    consistent (Tue afternoon); registry hardcodes a default
    weekly anchor with operator override.

Probe (c) becomes an OPTIONAL FOLLOW-ON if a raw API key
becomes available -- might collapse the manual-refresh
requirement to zero touches if headers match the UI.

AR-S3h (bd24a633) UNBLOCKED under option (a).

Full findings:
  automation-registry/docs/proposals/AR-S3-Q-MAIN-empirical-test.md

=== END PATH DECISION ===

=== 2026-05-14 FOLLOW-UP (two-timer model finding) ===

Preston Usage UI screenshot shows TWO subscription timers:
  (1) Current session -- 31% used, resets in 3hr 21min (5h rolling).
  (2) Weekly limits -- 7% used, resets Tue 9pm absolute.

Follow-up investigation: the documented anthropic-ratelimit-*-reset
headers reflect ORG-LEVEL minute-scale rate limits, NOT the Pro/Max
subscription session and weekly windows shown in the UI. The two
subscription timers are UI-only, undocumented, no programmatic
surface (no /v1/usage, /v1/account, /v1/organizations/usage). Claude
Code /status + /cost do not expose them.

AUTO-RESUME GAP: prior pseudocode used the org-level header as the
resume timer, which won't be the binding constraint for Pro/Max.
Three options surfaced for AR-S3h to choose between:

  (a) User-configured timer (operator notes 5h reset at session
      start; daemon schedules off that).
  (b) Scrape Claude Desktop UI (brittle).
  (c) Empirically verify whether anthropic-ratelimit-tokens-reset
      actually reflects the 5h session for Pro/Max-billed calls
      -- if yes, original design works as-is.

RECOMMENDATION: (c) first (zero-cost; collapses the question if
positive), fallback (a) if (c) shows the header is org-only. (b)
is last resort.

AR-S3h (bd24a633) RE-BLOCKED until Preston picks (a)/(b)/(c).
AR-S3g (Desktop scheduled, 2346412d) remains unblocked.

Sources cited by claude-code-guide: platform.claude.com docs/en/
api/rate-limits + manage-claude/rate-limits-api; support article
14552983 (Models, usage, and limits in Claude Code).

=== END FOLLOW-UP ===

=== 2026-05-14 RESOLVED ===

Anthropic rate-limit headers ARE documented:
  - anthropic-ratelimit-tokens-reset (RFC 3339)
  - anthropic-ratelimit-tokens-remaining (int)
  - anthropic-ratelimit-requests-reset (RFC 3339)
  - anthropic-ratelimit-requests-remaining (int)
  - input-tokens / output-tokens variants + priority-tier variants

Returned on every POST /v1/messages response. Cheapest probe:
POST /v1/messages with max_tokens=1 (1 in + 1 out token cost,
negligible on Pro/Max). No CLI helper exists; AR-S3h writes its
own ~20-LOC stdlib probe.

Subscription-side monthly reset is NOT exposed via headers --
that's a separate concept from rate-limit-window reset. For
limit-aware auto-resume, the API headers are the correct data.

AR-S3h (bd24a633) unblocked.

Full findings: automation-registry/docs/proposals/
AR-S3-Q-MAIN-headers-findings.md.

Source: https://platform.claude.com/docs/en/api/rate-limits.md

=== END RESOLUTION ===

Gates AR-S3h (claude_loop_continuous backend). The limit-aware auto-resume design (docs/proposals/AR-S3-limit-aware-resume.md, Q-A tentative answer (b)) wants to read x-ratelimit-remaining + x-ratelimit-reset HTTP response headers from Anthropic API calls to know when to pause and when to resume.

OPEN QUESTION: are those headers exposed to a Claude Code Stop hook, PostToolUse hook, or any other hook surface, such that a Python script can read them without proxying every API call?

If YES: AR-S3h's quota detection is straightforward -- attach a hook that scrapes the headers into a state file the loop runtime polls.

If NO: fall back to Q-A option (c) -- the loop runtime makes a tiny test API call at start of each iteration to probe the rate-limit headers via direct anthropic-sdk-python access. Cost is one extra API call per iteration; functionally equivalent but pays a small token tax.

Investigation steps:
1. Check Claude Code hook documentation for what data hooks receive.
2. If unclear, probe by writing a test Stop hook that logs the entire hook payload to a file and triggering a session end. See if rate-limit headers appear anywhere.
3. If hooks don't carry the headers, document the option (c) fallback in AR-S3-limit-aware-resume.md as the resolved answer.

DECISION OWED BEFORE: AR-S3h implementation starts. AR-S3g (Desktop scheduled) does NOT depend on this -- it can ship independently.

CROSS-REFERENCE: schema v2 doc at automation-registry/docs/proposals/automation-registry-schema-v2.md, AR-S3-limit-aware-resume.md design doc Q-MAIN section.

*Auto-generated 2026-05-14 13:38*
