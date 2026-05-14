# Automation Registry -- Schema v2 (tier-ordered)

> Supersedes `automation-registry-schema-v1.md` (preserved for history).
> Reshape driven by Preston's 2026-05-14 directive: prefer Claude-
> native scheduling primitives over OS-level cron. Subscription cost
> verified (Pro/Max include scheduled tasks + `/loop` + Routines with
> no separate metering; Routines has a daily-run cap of 5/15 that
> consumes subscription usage, not extra cost).
>
> Operating model: **fire continuously until subscription limit is
> hit, then auto-resume when limits reset.** Cron is preserved as a
> last-resort backend for cases where Claude-native primitives don't
> fit (cloud reach to localhost, $0 firing cost, sub-hour cadence).

## Operating principle

> "Always get the actual time remaining between the next limit
> reset. And then run till the limit is hit, or near. This way you
> actually fully utilize what you can." -- Preston, 2026-05-14

The registry's job is to maximize useful work per subscription
window. Mechanisms are ordered to prefer those that consume
subscription usage directly (so the limit-reset auto-resume keeps
work flowing) over those that don't burn subscription quota
(cron is OS-level, $0, but invisible to limit-tracking and
therefore a "last resort" for cases that genuinely can't run
under a Claude-native primitive).

## Mechanism tiers

### Tier 1 -- primary (Claude-native, subscription-utilising)

These are the preferred mechanisms. Pick from this tier first.

| Mechanism | When to use |
|---|---|
| `claude_desktop_scheduled` | Time-of-day events that must fire at a specific clock time (08:00 morning summary, 18:00 EOD review). Requires Claude Desktop be open (Preston-confirmed acceptable). |
| `claude_loop_continuous` | Continuous work over the day -- runs until the subscription limit is hit, pauses, auto-resumes on reset. The work-cycle pattern lives here. Includes the auto-resume infrastructure as a first-class concern (see `AR-S3-limit-aware-resume.md`). |

### Tier 2 -- cloud (Claude-native, machine-independent)

| Mechanism | When to use |
|---|---|
| `claude_routine` | Cloud-side firing -- survives the machine being off. ONLY for tasks that can run without local-host reach (GitHub events, Slack connectors, research offload). 1-hour minimum cadence; daily-run cap of 5/15 per plan. |

### Tier 3 -- utility (lifecycle / glue mechanisms)

| Mechanism | When to use |
|---|---|
| `claude_hook` | Fires on Claude Code lifecycle events (SessionStart / PostToolUse / PreCompact). Active only inside Claude Code sessions. |
| `manual` | Catalogued for visibility / docs. Never auto-dispatched. |
| `executor` | Delegates to an existing executor app per REUSE-AS-EXECUTOR (e.g. `automation/`). |
| `claude_check` | Pre-SOD transparency reports. Never auto-pushes; reports only. |
| `claude_blocker_callback` | Real-time blocker pings with typed `unblock_condition` + auto-retry on resolve. |

### Tier 4 -- last resort

> **PREFER A TIER 1 MECHANISM FIRST. cron is the fallback when a
> Claude-native primitive doesn't fit.**

| Mechanism | When to use |
|---|---|
| `cron` | OS-native (schtasks / launchd / systemd). Use ONLY when: (a) the target needs `127.0.0.1` reach AND a Claude Code session/Desktop can't be guaranteed open, OR (b) sub-hour cadence is required AND none of the Tier 1 options apply. AR-S3a backend kept for these cases; not deprecated. |

## Entry schema (unchanged from v1)

All v1 fields are preserved verbatim. The reshape is purely in the
**recommended mechanism choice + tier ordering**, not in the entry
shape. Existing v1 documents stay valid -- the validator already
accepts the new mechanism names (added below).

### Mechanism enum updates

```yaml
mechanism: cron | claude_hook | claude_loop | manual | executor
         | claude_check | claude_blocker_callback
         | claude_desktop_scheduled        # NEW (Tier 1)
         | claude_loop_continuous          # NEW (Tier 1)
         | claude_routine                  # NEW (Tier 2)
```

Note: `claude_loop` (v1) and `claude_loop_continuous` (v2) are
DIFFERENT. v1's `claude_loop` modelled "an always-on /loop session
running on a fixed interval." v2's `claude_loop_continuous` is the
limit-aware continuous worker. v1's mechanism stays in the schema for
back-compat but is deprecated in favour of v2's variant.

### `claude_desktop_scheduled` shape

Two trigger flavours: time-of-day (cron-style) or state-machine
(SOD/EOD pattern). Preston 2026-05-13 clarification: SOD/EOD aren't
clock events -- they're state transitions. SOD always before EOD
within a day. Missed yesterday's EOD: SKIPPED, not caught up.

```yaml
# Time-of-day flavour (simple cron-style trigger)
- name: example-time-of-day
  mechanism: claude_desktop_scheduled
  schedule: "0 14 * * *"    # plain 5-field cron expr
  trigger:
    kind: time              # default; equivalent to omitting trigger
  ...

# State-aware flavour for SOD/EOD
- name: dream-morning-summary
  description: SOD -- standup. Fires on session-open past midnight when SOD not yet run today.
  owner_project: dream
  target: dream.orchestrator:run_morning
  target_kind: python_callable
  mechanism: claude_desktop_scheduled
  schedule: null            # not clock-based
  trigger:
    kind: state
    fire_when: "last_sod_date != today"
    after_event: session_open        # check on every session start
    state_file: data/workflow_state.json
    on_fire_update: last_sod_date    # field to bump after successful run
  escalation: { channel: discord, on_failure: file_pd_task }
  enabled: true

- name: dream-eod-review
  description: EOD -- end-of-day review. Fires when SOD already ran today AND it is end of day.
  owner_project: dream
  target: dream.orchestrator:run_eod
  target_kind: python_callable
  mechanism: claude_desktop_scheduled
  schedule: null
  trigger:
    kind: state
    fire_when: "last_sod_date == today AND last_eod_date != today AND hour >= 17"
    after_event: session_open
    state_file: data/workflow_state.json
    on_fire_update: last_eod_date
  escalation: { channel: discord, on_failure: file_pd_task }
  enabled: true
```

**State persistence shape** -- `data/workflow_state.json`:

```json
{
  "last_sod_date": "2026-05-13",
  "last_eod_date": "2026-05-12",
  "last_sod_ts":   "2026-05-13T08:15:42-07:00",
  "last_eod_ts":   "2026-05-12T18:45:11-07:00"
}
```

The `fire_when` mini-expression is intentionally narrow (a fixed
DSL evaluated by the backend, NOT arbitrary Python). Supported
predicates v1: equality / inequality on `last_<event>_date` against
`today` / `yesterday`, `hour >=` / `<` integer, AND/OR composition.
That covers the SOD/EOD pattern + future "weekly status" variants
without opening a code-eval hole.

Backend writes time-of-day entries to Claude Desktop's
scheduled-tasks registry (`~/.claude/scheduled-tasks/<name>/SKILL.md`
per agent's finding). State-aware entries register a SessionStart-
boundary hook (NOT a scheduled task) that consults `workflow_state.json`
on every Claude Code session open and fires the target only when the
`fire_when` predicate is true. The Claude Desktop scheduled-task MCP
surface still lists the entry; the run history is recorded there +
in the registry's sqlite.

### `claude_loop_continuous` shape

**NOT "a Claude instance in a loop."** This mechanism runs the
**L8-L4 hierarchy** as a continuously-operating stack as long as
there is calendar work AND subscription credit. The L-layer
definitions live in research `c1779970` (agent-controller, AC) --
see the 2026-05-13 CORRECTION + AMENDMENT blocks for the canonical
role spec. The CORRECTION block places grader at L4 (high-compute);
any 'L5 grader' or 'guiding + grading' framing earlier in that
research item is superseded. Summary:

  - L4 -- gruntwork: coder, qa, playtester, AND grader. Grader is
          a HIGH-COMPUTE L4 (uses a stronger engine like Claude;
          standard L4s can run on local Ollama). Engine choice is
          PER-ROLE within the layer, not uniform per-layer.
  - L5 -- managers + guides. Every L4 has an L5 manager (same as
          any worker has a manager). L5 guides L4s, answers their
          questions, handles dialog/coordination above gruntwork.
          The grader-L4 has its own L5 manager same as any other L4.
  - L6 -- queen (ruflow IS L6, not just prior art)
  - L7 -- Dispatch (Preston talks to Dispatch directly for
          operational work)
  - L8 -- Project Manager (talks to Preston for oversight;
          generates SOD/EOD output; is its own AC component --
          tracked as AC-S16)

Engines are configurable PER ROLE within each layer (not just per
layer). A registry entry running `claude_loop_continuous` declares
engines so the stack can be mixed-model -- e.g. cheap local Ollama
for most L4 gruntwork, but Claude for the high-compute L4 grader,
and frontier models at L7/L8. The `engines` map below shows the
default per-layer engine; per-role overrides live in the AC
templates/settings.json (AC-S2 / AC-S9).

```yaml
- name: dream-work-cycle
  description: L8-L4 hierarchy operating continuously; paused on limit, resumed on reset.
  owner_project: dream
  target: dream.orchestrator:run_work_cycle    # the source of work items
  target_kind: python_callable
  mechanism: claude_loop_continuous
  schedule: null                # NOT cron-scheduled

  # Per-layer engine config -- maps to AC's multi-engine driver
  # (AC-S10). Each layer can use a different model or provider.
  engines:
    L4: ollama-qwen2.5-coder    # gruntwork, runs locally, $0
    L5: claude-haiku            # managers + guides for L4s
    L6: claude-sonnet           # queen (ruflow)
    L7: claude-opus             # Dispatch (Preston interface)
    L8: claude-opus             # PM (Preston PM interface)

  goal_template: "Process the next calendar task; stop when done or quota near limit."

  limit_aware:
    pause_at_remaining_pct: 5      # pause when 5% of quota remains
    resume_on_reset: true
    idle_check: heartbeat_file

  escalation: { channel: discord, on_failure: file_pd_task }
  enabled: true
```

Required + optional fields (only valid for `claude_loop_continuous`):
  - `engines` (required): map of layer name (`L4`..`L8`) to engine
    id. Engine ids resolve through AC's multi-engine driver
    (AC-S10). A missing layer falls back to a default (TBD --
    needs an AC-side decision; likely `claude-sonnet`).
  - `goal_template` (optional): rendered at each iteration into
    `/goal <text>` (AR-S3j integration).
  - `limit_aware.pause_at_remaining_pct` (optional): defensive
    pause before hard limit. Default 5%.
  - `limit_aware.resume_on_reset` (optional): re-arm timer after
    reset. Default true.
  - `limit_aware.idle_check` (optional): `heartbeat_file |
    process_check | session_count` (see
    `AR-S3-limit-aware-resume.md`). Default `heartbeat_file`.

The runtime spawns the L8-L4 stack as a managed unit; L8's
conversation surface with Preston (Discord pings + Dream UI tab,
TBD per AC-S16) is independent of the registry's escalation channel
-- escalation is for `claude_loop_continuous` runtime failures,
not for L8's normal PM-style chatter.

### `claude_routine` shape

```yaml
- name: github-pr-triage
  description: Cloud routine reviewing PRs via the GitHub connector.
  owner_project: pipeline-dashboard
  target: review-prs
  target_kind: claude_prompt
  mechanism: claude_routine
  schedule: "0 9 * * 1-5"   # 1h minimum interval
  escalation: { channel: discord, on_failure: discord_only }
  enabled: true
```

New `target_kind: claude_prompt` -- the target is the prompt body
or a slash-command invocation; the runner forwards it to the
routine's cloud session. No local-host reach.

## Validator rules added in v2

1. `claude_desktop_scheduled` requires EITHER non-null `schedule`
   (time-of-day flavour) OR a `trigger` block with `kind: state`
   (state-aware flavour); never both.
2. `trigger.kind: state` requires `fire_when` (DSL string),
   `after_event` (string, typically `session_open`),
   `state_file` (path), `on_fire_update` (state-file field name).
3. `claude_loop_continuous` requires `schedule: null` (NOT
   cron-scheduled) AND `engines` map covering L4 + L5 + L6 + L7 +
   L8 (missing layer falls back to default at run time but the
   validator soft-warns); optional `goal_template` + `limit_aware`.
4. `claude_routine` requires non-null `schedule`; minimum cadence
   60 minutes (rejected at parse time if step is < 60 in minute
   field while hour is wildcard).
5. `target_kind: claude_prompt` only valid when `mechanism` is
   `claude_routine` or `claude_loop_continuous`.

## Migration policy

  - **AR-S3a's OS-cron backend stays.** It's not a band-aid; it's the
    correct Tier 4 fallback.
  - **AR-S3e (Dream Auto migration) reshapes** to use Tier 1
    mechanisms for the three migration targets:
      * `dream-morning-summary`  -> `claude_desktop_scheduled` 08:00
      * `dream-eod-review`       -> `claude_desktop_scheduled` 18:00
      * `dream-work-cycle`       -> `claude_loop_continuous`
        (NOT 0 9-17 * * 1-5 cron as proposed in v1).
  - The cron implementation (AR-S3a..S3d) becomes the proof-of-pipe
    + fallback. Claude-native mechanisms ship next.

## Cross-references

- v1 doc (preserved): `automation-registry-schema-v1.md`.
- Auto-resume design: `AR-S3-limit-aware-resume.md` (filed alongside).
- Subscription cost evaluation: `AR-S3-claude-scheduled-evaluation.md`.
- AR-S3e reshape: `AR-S3e-dream-auto-migration.md` (revised
  separately).
- Phase A scoping (umbrella): pipeline-dashboard `docs/audits/
  2026-05-12-automation-registry-scoping.md`.
- Research: PD `fe0302b9` (registry).
