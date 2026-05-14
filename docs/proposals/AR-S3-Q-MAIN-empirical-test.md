# AR-S3 -- Q-MAIN empirical test (could not run this session)

> Preston authorised the test (c) probe: one tiny ``POST /v1/messages``
> with ``max_tokens: 1`` to capture ``anthropic-ratelimit-tokens-reset``
> and compare to the UI's "Resets in 3 hr 21 min" countdown.
>
> **Could not execute this session. Surfacing why and what to do next.**

## Why the test could not run from this session

  - ``ANTHROPIC_API_KEY`` is **absent** from this Claude Code
    session's environment. So is ``ANTHROPIC_AUTH_TOKEN``.
  - Claude Code uses OAuth-managed subscription auth
    (``CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH=1``,
    ``CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST=1``) -- not a raw API key.
  - Internal response headers from Claude Code's own subscription-
    backed calls aren't exposed to user code in this session.
  - ``/status`` and ``/cost`` slash commands don't surface the
    rate-limit headers either (confirmed by the prior claude-code-
    guide investigation).

So the probe requires either (i) a raw API key explicitly placed in
the environment, or (ii) the operator runs ``curl`` themselves with
their own key and pastes the response.

## What the probe would look like (for Preston to run if desired)

```bash
curl -sD - https://api.anthropic.com/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-3-5-haiku-20241022","max_tokens":1,"messages":[{"role":"user","content":"."}]}'
```

Capture from the response:

  - Headers: ``anthropic-ratelimit-tokens-reset``,
    ``anthropic-ratelimit-tokens-remaining``,
    ``anthropic-ratelimit-requests-reset``,
    ``anthropic-ratelimit-requests-remaining``.
  - Body's ``usage`` object: actual ``input_tokens`` +
    ``output_tokens`` (will likely be 5-15 in + 1 out, not 1+1, due
    to Anthropic's model framing).

Convert ``anthropic-ratelimit-tokens-reset`` (RFC 3339) to delta-
from-now. Compare to the UI's "Resets in <X>" countdown.

  - **MATCH** -> the documented headers ARE the Pro/Max session
    window. AR-S3h's original design works. Unblock.
  - **DIFFER** -> documented headers reflect org-level minute-
    scale limits only. Fall back to option (a) below.

## Recommended path forward without the probe

Since the probe requires user-side execution, defer it and pick the
robust fallback: **option (a) -- user-configured timer.**

  - Operator (or AC-S16's L8 agent on first L8-L4 cycle of the day)
    notes the next-session-reset timestamp from the Claude UI's
    Usage panel. Posts it to the registry via a new endpoint:
    ``POST /api/registry/quota`` with body
    ``{session_reset_at: "2026-05-14T20:30:00-07:00",
       weekly_reset_at: "2026-05-19T21:00:00-07:00"}``.
  - Registry persists to ``data/quota_state.json``.
  - AR-S3h's loop runtime reads that file at startup, schedules
    timers, no API probe needed.
  - Manual refresh once per session-window (every 5h).

This is OPTION (a) from the prior framing. It's not as elegant as
(c)'s "free auto-detection" if (c) works, but it sidesteps the
auth-key requirement entirely and is testable without API access.

## Preston's weekly-timer constraint baked in

Preston noted: "weekly limit is UI-only and consistent (Tuesday
afternoon/late)." For the weekly cap, the registry can use a
hardcoded weekly anchor with operator override:

  - Default: every Tuesday at 21:00 local (configurable via
    ``data/quota_state.json`` ``weekly_reset_at`` field if the cycle
    drifts).
  - The 5h session timer is the binding constraint during work;
    weekly is informational / longer-term budgeting.

This shape works regardless of whether the API probe ever resolves.

## Recommendation

  1. **Adopt option (a) as the path** -- user-configured timer,
     persisted to ``data/quota_state.json``, refreshed once per
     session-window. AR-S3h proceeds with this design today.
  2. **Keep the (c) probe as a follow-on** for if/when a raw API
     key becomes available -- might auto-collapse the manual-
     refresh requirement to zero touches if the documented headers
     turn out to match the session window.
  3. Research ``e8d95bf1`` updated: status moves from "blocked
     pending probe" to "decided -- option (a); (c) deferred as
     optional follow-on."

## Action items

  - Research ``e8d95bf1`` amended (alongside this doc).
  - AR-S3h ``bd24a633`` description updated -- option (a) is the
    design; status moves from ``blocked`` to ``todo``. New
    sub-step: ``POST /api/registry/quota`` endpoint that accepts
    operator-supplied reset timestamps.
  - Auto-resume design doc (``AR-S3-limit-aware-resume.md``)
    updated to reflect (a) as the canonical detection path.

## Cross-references

  - Q-MAIN findings doc (full headers list): `AR-S3-Q-MAIN-headers-findings.md`.
  - Auto-resume design: `AR-S3-limit-aware-resume.md`.
  - Two-timer model finding: same Q-MAIN findings doc, bottom section.
  - Research item: PD `e8d95bf1`.
  - Sub-task: AR-S3h `bd24a633`.
