# AR-S3 -- Q-MAIN: Anthropic rate-limit headers (findings)

> Resolves the reframed Q-MAIN from
> `AR-S3-limit-aware-resume.md`. Source: official Anthropic docs at
> https://platform.claude.com/docs/en/api/rate-limits.md.

## Headers (documented)

`POST /v1/messages` responses include the following headers. All
present on every successful response.

| Header | Meaning | Format |
|---|---|---|
| `anthropic-ratelimit-requests-reset` | Wall-clock time when the request bucket fully refills | RFC 3339 (e.g. `2026-05-14T18:30:00Z`) |
| `anthropic-ratelimit-tokens-reset` | Wall-clock time when the token bucket fully refills | RFC 3339 |
| `anthropic-ratelimit-input-tokens-reset` | Input-token bucket refill time | RFC 3339 |
| `anthropic-ratelimit-output-tokens-reset` | Output-token bucket refill time | RFC 3339 |
| `anthropic-ratelimit-requests-remaining` | Requests left in the current window | integer |
| `anthropic-ratelimit-tokens-remaining` | Tokens left (rounded to nearest 1000) | integer |
| `anthropic-ratelimit-input-tokens-remaining` | Input tokens left | integer |
| `anthropic-ratelimit-output-tokens-remaining` | Output tokens left | integer |
| `anthropic-priority-input-tokens-*` / `anthropic-priority-output-tokens-*` | Priority Tier variants (if eligible) | RFC 3339 / integer |

## Cheapest probe at session start (not documented; empirical)

No documented "free probe" endpoint:

  - `GET /v1/organizations/rate_limits` exists but requires an
    **Admin API key** + reports org-level limits only. Not useful
    for a session-start read in this app's context.
  - `GET /v1/models` is not mentioned in the rate-limits doc as
    returning the headers. **Empirical test required** before
    relying on it.
  - **Default recommendation**: tiny `POST /v1/messages` with
    `max_tokens: 1` and a 1-character prompt. Cost: 1 input token
    + 1 output token. Returns the full header set. Negligible
    cost, fully documented behaviour.

If empirical testing finds `GET /v1/models` does include the
headers, switch to it for absolute-zero token cost. AR-S3h's
probe-implementation step should run that empirical test first.

## Subscription-side reset

NOT exposed via API headers. The rate-limit headers reflect the
**rolling rate-limit window** (token-bucket algorithm) -- not the
subscription's monthly billing cycle or Pro's 5-hour window UI
display. Two different concepts:

  - **API headers**: when can I send more tokens / requests RIGHT
    NOW? Used for limit-aware auto-resume.
  - **UI "usage reset"**: when does my subscription-budget
    counter visible in the web UI reset? Plan-cycle concept; not
    a programmatic surface.

For `claude_loop_continuous`, the **API headers are the right
data**. Preston's confidence that "the data is available" is
correct -- it's just sourced from API responses, not the UI's
billing panel.

## Claude Code CLI helper

No documented `claude usage` / `claude --quota` command. No MCP
tool surfaces this. We write the probe ourselves. The probe is
small (~20 lines of stdlib `urllib` + RFC 3339 parsing).

## What AR-S3h does at session start

```python
# Pseudo-code, lands in AR-S3h
def read_quota_state(api_key: str) -> QuotaState:
    body = b'{"model":"claude-3-5-haiku-latest","max_tokens":1,"messages":[{"role":"user","content":"_"}]}'
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        headers = dict(r.headers)
    return QuotaState(
        tokens_reset=parse_rfc3339(headers["anthropic-ratelimit-tokens-reset"]),
        tokens_remaining=int(headers["anthropic-ratelimit-tokens-remaining"]),
        requests_reset=parse_rfc3339(headers["anthropic-ratelimit-requests-reset"]),
        requests_remaining=int(headers["anthropic-ratelimit-requests-remaining"]),
        checked_at=datetime.now(timezone.utc),
    )
```

This call costs 1 in + 1 out token (effectively $0 for a Pro/Max
subscription). The loop runtime persists `QuotaState` to
`data/quota_state.json` and schedules a timer for the earlier of
`tokens_reset` or `requests_reset` (which one is the actual
bottleneck depends on the workload).

## Q-MAIN resolution

  - Headers: **documented, list above**.
  - Format: **RFC 3339**.
  - Cheapest probe: **`POST /v1/messages` with `max_tokens: 1`**
    (empirical fallback test for `/v1/models` is a small AR-S3h
    sub-step).
  - Subscription reset: **separate concept; not API-exposed; not
    needed for limit-aware resume**.
  - CLI helper: **none; write our own**.

AR-S3h (`bd24a633`) unblocked.

## References

- Anthropic docs: https://platform.claude.com/docs/en/api/rate-limits.md
- Auto-resume design: `AR-S3-limit-aware-resume.md`.
- Research item: PD `e8d95bf1` (Q-MAIN) -- updated alongside this
  commit with the same findings.
- Sub-task gated on this: AR-S3h `bd24a633`.
