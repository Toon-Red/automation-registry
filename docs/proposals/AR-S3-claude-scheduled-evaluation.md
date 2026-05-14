# AR-S3 -- Claude scheduled tasks vs custom cron mechanism

> Investigation requested by Preston: "Why not utilize claude's
> scheduled and routines?" Applies the Claude-native-over-band-aid
> rule to the cron mechanism layer.

## TL;DR

Claude has **three distinct scheduling features**, not one. None of
them is a drop-in replacement for OS-cron in our use case, but ONE
(Routines) is the right backend for a future class of automations we
don't have yet. **Recommendation: Path 2 -- keep AR-S3a's cron
mechanism; add `claude_scheduled` as a parallel mechanism later when
we have a routine-shaped use case.**

## The three features

| Feature | Where it fires | Session required | Survives shutdown | Min interval |
|---|---|---|---|---|
| **`/loop`** (in-session) | Local Claude Code process | YES (current session) | No; 7-day if resumed | 1 min |
| **Desktop scheduled tasks** | Local machine | App must be open | No | 1 min |
| **Routines** | **Anthropic cloud** | **NO** | **YES** | **1 hour** |

Source: Anthropic docs at code.claude.com/docs/en/scheduled-tasks +
/routines + /desktop-scheduled-tasks (April 2026 release preview, live
+ functional). The `mcp__scheduled-tasks__*` MCP server is the
Desktop variant; the `schedule` skill is the Routines (cloud) CLI;
`/loop` is its own session-scoped thing.

## Capabilities matrix

| Capability | /loop | Desktop | Routines | OS cron (AR-S3a) |
|---|---|---|---|---|
| Schedule format | cron + NL | cron | cron + NL | cron (5-field) |
| Invokes Claude prompt | Yes | Yes | Yes | No (subprocess only) |
| Invokes arbitrary shell | Yes (in session) | Yes | Yes | Yes |
| Invokes MCP tool | Yes (in session) | Yes | Connectors only | No directly |
| Access to local files (target repo) | Yes | Yes | **NO -- fresh clone** | Yes |
| Reaches `127.0.0.1:5005` etc. | Yes | Yes | **NO -- cloud-side** | Yes |
| Token cost per fire | Session usage | Session usage | Daily cap + subscription | **$0** |
| Persists across reinstall | No | No | **YES (cloud)** | Yes (OS) |
| Sub-hour granularity | Yes (1m) | Yes (1m) | **No (60m min)** | Yes (1m) |
| Observability | Run history | Run history | Run history | sqlite `cron_runs` |

## Use-case fit for our three migration targets

`dream-morning-summary` / `dream-eod-review` / `dream-work-cycle`
all call `dream.orchestrator:run_*` which does HTTP to local
services (`127.0.0.1:5005` Dream API, `127.0.0.1:5100` PD API,
`127.0.0.1:5028` Agent Commander), reads local sqlite state, writes
local files, and posts to Discord via an env-var webhook.

**Routines disqualifiers:**
- Cloud-side -> cannot reach `127.0.0.1` on Preston's machine.
- Fresh repo clone -> no access to Dream's local sqlite, no
  `daily-state.json`, no `data/dream.db`.
- 60-min minimum -> blocks Work Cycle's hourly pattern only barely
  (hourly = 60m exactly, borderline).
- Token cost per fire -> 2-9 fires/day costs real money for what
  schtasks does free.
- **Connectors-only for non-shell calls** -> our flow uses local
  HTTP, not connector-style integrations.

**Desktop disqualifiers:**
- Requires the Claude Code Desktop app to be open. Preston runs the
  CLI; he may not always have Desktop open. Worse than schtasks,
  which fires on bare Windows.
- Same token-cost issue.

**`/loop` disqualifiers:**
- Session-scoped. Dies when Preston closes his session. Catastrophic
  for an EOD review that should fire regardless of who's at the
  keyboard.

For OUR THREE TARGETS: OS-cron wins on every dimension. Routines
would be wrong even if free.

## When Routines would be the right answer

The pattern that fits Routines:

  * No local-machine dependency (no `127.0.0.1`, no local files).
  * Operates via Anthropic-supported connectors (Slack, Linear,
    GitHub events, etc.).
  * Token cost is acceptable per fire.
  * 1-hour minimum granularity is fine.
  * Survives Preston's machine being off / reinstalled.

Use cases that COULD fit (none filed yet):

  * Daily check-in on a GitHub repo via Routines + the GitHub
    connector.
  * Weekly Linear triage that reads/writes tickets via the Linear
    connector.
  * Cloud-side notifications when an external API surfaces an event
    (e.g. a Slack channel mention triggers a routine).

When such a use case ships, the registry should catalog it under a
new mechanism `claude_scheduled` (or `claude_routine`) -- a parallel
peer of `cron`, not a replacement.

## Architectural recommendation: Path 2

**Keep AR-S3a's cron mechanism. Add `claude_scheduled` as a parallel
mechanism (later) when a routine-shaped use case arrives.**

Justification:

1. **Use-case fit**: our three immediate migration targets need
   local-machine reach. Routines cannot provide that. AR-S3a's work
   was the right shape.
2. **The registry is the catalog**: the entire point of the registry
   is mechanism-per-entry. Adding `claude_scheduled` later costs one
   new backend class in `automation_registry/` and one schema-doc
   line; the catalog layer doesn't change.
3. **No deprecation needed**: AR-S3a is a free, OS-native fast path
   that no Claude feature obsoletes for our local-service flows. It's
   not a band-aid -- it's the correct backend for this class of work.
4. **REUSE-AS-EXECUTOR pattern (from `fe0302b9` Q1)**: Routines is
   another executor we can catalog when we have a routine-shaped
   entry, exactly like we catalog `automation/` today.

## What this changes (and doesn't)

- **AR-S3a's code stands.** No deprecation. No rework.
- **AR-S3b/c/d stand.** Same.
- **AR-S3e (Dream Auto migration) proceeds with cron**, not
  Routines. The recommendation in the existing scoping doc
  (`AR-S3e-dream-auto-migration.md`) is unchanged.
- **Schema v1 gains a future mechanism enum value** (`claude_scheduled`)
  the day we ship the corresponding backend. Not a today change.

## Confidence level

The agent's investigation cited official Anthropic docs at
`code.claude.com/docs/en/scheduled-tasks`, `/routines`,
`/desktop-scheduled-tasks`. The three-feature distinction and the
runtime model are authoritative; the cost-model detail ("counts
against daily routine allowance + subscription") is from Anthropic
docs but is the kind of policy that may evolve as Routines matures
past the April 2026 preview. None of the disqualifiers above hinge
on cost policy, though -- they're capability gaps (no local-network
reach, no local-file access).

## Open question for Preston

**Is there a use case I'm not seeing that WOULD fit Routines today?**
If Preston has a cloud-only / connector-driven flow in mind that I'm
missing, Path 2 still applies (catalog it under `claude_scheduled`
when filed). But knowing the use case would let me file the AR-S4-ish
sub-task to build the `claude_scheduled` backend at the right time.

## Cross-references

- AR-S3 breakdown: `docs/proposals/AR-S3-cron-backend-breakdown.md`.
- AR-S3e scoping: `docs/proposals/AR-S3e-dream-auto-migration.md`.
- Schema v1: `docs/proposals/automation-registry-schema-v1.md`.
- Research: PD `fe0302b9`.
