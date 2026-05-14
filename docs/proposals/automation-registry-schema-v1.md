# Automation Registry -- Schema v1 (AR-S1)

> Single source of truth for `automations.yaml`. Portable to the new
> `automation-registry/` repo when it's created. Closes sub-task
> **AR-S1** under research `fe0302b9`.

## Why this lives in pipeline-dashboard for now

The new `automation-registry/` app does not exist yet (per the Phase
A scoping doc, that's sub-task AR-S2). The schema needs to land
somewhere stable BEFORE the repo is scaffolded so AR-S2 has a
contract to build to. This file moves to the new repo verbatim when
AR-S2 ships.

## Schema overview

Top-level shape of `automations.yaml`:

```yaml
schema_version: 1
automations:
  - <Automation entry>
  - <Automation entry>
  ...
```

Each `Automation entry` has the following fields. Required fields
are marked R; optional fields are O.

| Field | Type | R/O | Meaning |
|---|---|---|---|
| `name` | string (kebab-case) | R | Globally unique entry id. Used in logs, escalations, status APIs. |
| `description` | string | R | One-line WHAT. Surfaced in Dream UI tab and operator pings. |
| `owner_project` | string | R | PD project_id that owns this automation. Failures escalate to this project by default. |
| `target` | string | R | What runs. Interpretation depends on `target_kind`. |
| `target_kind` | enum | R | `python_callable` \| `shell` \| `http` \| `mcp` \| `agent_role` |
| `mechanism` | enum | R | `cron` \| `claude_hook` \| `claude_loop` \| `manual` \| `executor` \| `claude_check` \| `claude_blocker_callback` |
| `schedule` | string \| null | depends | Cron expr for `cron`; hook event name for `claude_hook`; interval for `claude_loop`; null for `manual` / `claude_blocker_callback`. |
| `unblock_condition` | object | depends | Required when `mechanism: claude_blocker_callback`. See below. |
| `executor_ref` | object | depends | Required when `mechanism: executor`. See below. |
| `pre_dispatch_hooks` | list[string] | O | Named hooks (e.g., `self_heal`, `ecosystem_playtest`) the registry runs before dispatching. |
| `escalation` | object | R | Where failures and operator pings go. See below. |
| `enabled` | bool | R | If false, registry catalogs but does not dispatch. |
| `tags` | list[string] | O | Free-form labels for UI filtering (e.g., `daily`, `ceremony`, `quality-gate`). |

### `target_kind` semantics

| `target_kind` | `target` value | Dispatcher behavior |
|---|---|---|
| `python_callable` | `module.function` or `module:function` | Import + call. Used when the registry process is in the same Python runtime as the target (Dream's own functions). |
| `shell` | absolute path or repo-relative path to script | `subprocess.Popen` with the target as argv[0]. |
| `http` | absolute URL | HTTP POST (default) or method per `target_http.method`. |
| `mcp` | `server_name:tool_name` | Invoke MCP tool via the registry's MCP client. |
| `agent_role` | role name (e.g., `coder`, `qa`, `playtester`) | Spawns an Agent Controller role at the level declared in the role template. Bridges to AC-S1..S15. |

### `mechanism` semantics

| `mechanism` | What dispatcher does |
|---|---|
| `cron` | OS-level schedule via `schtasks` / `launchd` / `systemd-timer`. Consumes the `scripts/service_supervisor.py` cron extension (PD task fa01b290, absorbed into AR-S3). |
| `claude_hook` | Writes / syncs a fragment into the target repo's `.claude/settings.json`. Active only while a Claude Code session is open. |
| `claude_loop` | Spawns an always-on Claude Code session running `/loop`. Lifecycle managed by the registry. The session itself becomes a tracked entity in the registry. |
| `manual` | Listed in the registry for visibility / docs. Never auto-dispatched. |
| `executor` | Delegates to an existing executor app (per Preston's 2026-05-12 REUSE-AS-EXECUTOR framework). Inaugural target: `automation/` (calendar -> Ruflow swarm). The registry catalogs the executor and surfaces its status; the executor itself owns dispatch. |
| `claude_check` | Pre-SOD transparency report. Fires at SessionStart-equivalent. Output is a Discord report (never an auto-push). Backed by the new push-only-tested-and-ready rule. |
| `claude_blocker_callback` | Real-time blocker ping with typed `unblock_condition` and auto-retry. |

### `unblock_condition` shape (required for `claude_blocker_callback`)

```yaml
unblock_condition:
  kind: time | event | external   # required
  value: <iso8601 | event_name | null>
  # time:     value is an ISO8601 timestamp; registry auto-retries at/after that time.
  # event:    value is an event name; registry subscribes and auto-retries on emit.
  # external: value is null; registry waits for an operator action (Discord reaction,
  #           registry UI button, or a `human` engine handoff per AC-S14).
```

### `executor_ref` shape (required for `mechanism: executor`)

```yaml
executor_ref:
  app: automation          # owner app name (PD project_id)
  endpoint: /api/health    # status endpoint registry polls
  trigger_endpoint: /api/trigger  # optional; manual dispatch passthrough
```

### `escalation` shape

```yaml
escalation:
  channel: discord | log_only | toast | none   # required
  webhook_id: <optional discord webhook override>
  on_failure: file_pd_task | discord_only | log_only   # required
  pd_project: <project_id>                # required if on_failure == file_pd_task; defaults to owner_project
  retry: { max_attempts: 3, backoff_s: 60 } # optional
```

The `on_failure: file_pd_task` rule (Preston decision 2026-05-12)
files a PD task with title prefix `Playtest failure:` (or
`Automation failure:` for non-playtest entries), idempotent by
title-prefix dedup -- if an open task with the same prefix exists,
the dispatcher appends a new line to its description rather than
filing a duplicate.

## Example entries (one per mechanism)

```yaml
schema_version: 1

automations:

  # 1. cron -- migrated from `Dream Auto` schtask
  - name: dream-eod-review
    description: Aggregates the day's work, posts review to Discord, seeds tomorrow's calendar.
    owner_project: dream
    target: orchestrator:run_eod
    target_kind: python_callable
    mechanism: cron
    schedule: "0 18 * * *"
    pre_dispatch_hooks: [self_heal, ecosystem_playtest]
    escalation:
      channel: discord
      on_failure: file_pd_task
      pd_project: dream
      retry: { max_attempts: 3, backoff_s: 60 }
    enabled: true
    tags: [daily, ceremony]

  # 2. claude_hook -- PD's SessionStart health probe (already shipped as part of umbrella 3615dce5)
  - name: pd-session-start-health
    description: Health probe of all ecosystem services at the top of every Claude Code session.
    owner_project: pipeline-dashboard
    target: .claude/hooks/health_check.py
    target_kind: shell
    mechanism: claude_hook
    schedule: SessionStart
    escalation:
      channel: log_only
      on_failure: log_only
    enabled: true
    tags: [session-lifecycle]

  # 3. claude_loop -- Dream's autonomous work cycle as an always-on /loop
  - name: dream-work-cycle-loop
    description: Mid-day work cycle -- check priority queue, spawn agents, file results. Runs continuously between SOD and EOD.
    owner_project: dream
    target: orchestrator:run_work_cycle
    target_kind: python_callable
    mechanism: claude_loop
    schedule: "30m"
    pre_dispatch_hooks: [self_heal]
    escalation:
      channel: discord
      on_failure: discord_only
    enabled: true
    tags: [work-hours, agent-orchestration]

  # 4. manual -- self-heal sweep; listed for visibility, never auto-dispatched
  - name: dream-self-heal
    description: Operator-invoked recovery sweep across all ecosystem services.
    owner_project: dream
    target: self_heal:ensure_services
    target_kind: python_callable
    mechanism: manual
    schedule: null
    escalation:
      channel: discord
      on_failure: discord_only
    enabled: true
    tags: [operator-tool]

  # 5. executor -- delegates to the existing `automation/` app per REUSE-AS-EXECUTOR
  - name: automation-calendar-swarm
    description: Calendar-driven Ruflow swarm trigger. Catalogued here; dispatch owned by the `automation` app at :5045.
    owner_project: automation
    target: automation                  # purely informational; executor owns the action
    target_kind: http                   # the dispatch surface is HTTP, but the registry doesn't fire it
    mechanism: executor
    schedule: null                      # `automation/` owns its own poll loop
    executor_ref:
      app: automation
      endpoint: /api/health
      trigger_endpoint: /api/trigger    # manual dispatch passthrough from registry UI
    escalation:
      channel: discord
      on_failure: file_pd_task
      pd_project: automation
    enabled: true
    tags: [calendar-driven, swarm]

  # 6. claude_check -- SOD origin-sync transparency report
  - name: ecosystem-origin-sync-check
    description: SOD report -- for each ecosystem repo, lists dirty files + unpushed commits with reason hints. Never auto-pushes.
    owner_project: automation-registry
    target: scripts/origin_sync_check.py
    target_kind: shell
    mechanism: claude_check
    schedule: SessionStart
    escalation:
      channel: discord
      on_failure: file_pd_task
      pd_project: pipeline-dashboard
    enabled: true
    tags: [sod, transparency, discipline]

  # 7. claude_blocker_callback -- AI hits Claude usage limit; auto-retry at reset; bonus example
  - name: ai-overnight-resume
    description: When an agent reports a Claude usage-limit block, ping Discord + auto-retry at the limit-reset boundary.
    owner_project: automation-registry
    target: agent-controller:resume-blocked-session
    target_kind: agent_role             # AC role; resolves through AC-S10 multi-engine driver
    mechanism: claude_blocker_callback
    schedule: null
    unblock_condition:
      kind: time
      value: "2026-05-13T20:00:00-07:00"   # written by the dispatcher when the block fires
    escalation:
      channel: discord
      on_failure: discord_only
    enabled: true
    tags: [agent, blocker, overnight]
```

## Validator rules (informal -- formalize in AR-S1 loader)

A loader implementing this schema must reject entries that:

1. Use `mechanism: cron` without a non-null `schedule`.
2. Use `mechanism: claude_hook` whose `schedule` isn't a known
   Claude Code hook event (`SessionStart` / `PostToolUse` /
   `PreCompact` / etc.).
3. Use `mechanism: claude_blocker_callback` without an
   `unblock_condition` object containing a valid `kind`.
4. Use `mechanism: executor` without an `executor_ref` object.
5. Use `target_kind: agent_role` without a corresponding role in
   the Agent Controller's role registry (forward reference -- AC-S1
   has to ship before this validation can be hard-enforced; loader
   should soft-warn until then).
6. Have `escalation.on_failure: file_pd_task` without `pd_project`
   resolvable to an existing PD project.
7. Have a `name` that duplicates another entry's.

## Open items deferred to AR-S2+

- The on-disk location of `automations.yaml` (registry repo root
  vs `data/`).
- Whether the registry watches the file for changes (inotify /
  ReadDirectoryChangesW) or requires a `/api/reload` POST.
- How the loader's soft-warnings on forward references (e.g.,
  unknown agent roles) get surfaced in the UI.

## Cross-references

- Research `fe0302b9` -- registry decision + REUSE-AS-EXECUTOR
  framework application.
- Phase A scoping `docs/audits/2026-05-12-automation-registry-scoping.md`
  -- 11+1 sub-task breakdown; mechanism designs.
- Research `c1779970` (Agent Controller) -- AC-S14 (human engine)
  resolves `unblock_condition.kind: external`; AC-S15 (real-time
  observability) is the natural backing for executor-status surfaces
  in the Dream UI tab.
- PD task `fa01b290` -- absorbed into AR-S3 (cron mechanism backend).
- PD task `e15cdbad` -- automation/ wiki update declaring narrow
  scope, links to registry as catalog (AR-S0a).

## Done-when (AR-S1)

- [x] Schema v1 documented in markdown + YAML examples.
- [x] One concrete entry per mechanism (7 entries including the
      bonus blocker case).
- [x] Validator rules written down (informal -- formal Pydantic /
      dataclass loader is AR-S2 territory).
- [x] Committed and pushed.

AR-S1 marked done in PD by completing task in the AC umbrella.
