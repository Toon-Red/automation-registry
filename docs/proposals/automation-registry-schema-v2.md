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

```yaml
- name: dream-morning-summary
  description: 08:00 standup -- runs run_morning.
  owner_project: dream
  target: dream.orchestrator:run_morning
  target_kind: python_callable
  mechanism: claude_desktop_scheduled
  schedule: "0 8 * * *"     # standard 5-field cron expr
  escalation: { channel: discord, on_failure: file_pd_task }
  enabled: true
```

Backend writes the entry into Claude Desktop's scheduled-tasks
registry (`~/.claude/scheduled-tasks/<name>/SKILL.md` per agent's
finding) and surfaces the run history via the same MCP server.

### `claude_loop_continuous` shape

```yaml
- name: dream-work-cycle
  description: Continuous calendar-driven work, paused on limit, resumed on reset.
  owner_project: dream
  target: dream.orchestrator:run_work_cycle
  target_kind: python_callable
  mechanism: claude_loop_continuous
  schedule: null            # NOT cron-scheduled
  goal_template: "Process the next calendar task; stop when done or quota near limit."
  limit_aware:
    pause_at_remaining_pct: 5      # pause when 5% of quota remains
    resume_on_reset: true
    idle_check: heartbeat_file
  escalation: { channel: discord, on_failure: file_pd_task }
  enabled: true
```

New optional fields (only valid for `claude_loop_continuous`):
  - `goal_template`: rendered at each iteration into `/goal <text>`.
    Source for `/goal` integration -- see `AR-S3j` task.
  - `limit_aware.pause_at_remaining_pct`: defensive pause before
    hard limit.
  - `limit_aware.resume_on_reset`: re-arm timer after reset.
  - `limit_aware.idle_check`: `heartbeat_file | process_check |
    session_count` (see `AR-S3-limit-aware-resume.md`).

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

1. `claude_desktop_scheduled` requires non-null `schedule`.
2. `claude_loop_continuous` requires `schedule: null` (it's NOT
   cron-scheduled); optional `goal_template` + `limit_aware`.
3. `claude_routine` requires non-null `schedule`; minimum cadence
   60 minutes (rejected at parse time if step is < 60 in minute
   field while hour is wildcard).
4. `target_kind: claude_prompt` only valid when `mechanism` is
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
