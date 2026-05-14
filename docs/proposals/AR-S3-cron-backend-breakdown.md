# AR-S3 -- cron mechanism backend: sub-sub-task breakdown

> Scoping pass for the cron backend. Original AR-S3 was rated L in
> the Phase A scoping doc; broken here into **6 sub-sub-tasks** (3 S
> + 3 M) so each can land with its own commit and playtest evidence.
> No code in this doc -- structure + open questions only.

## Inputs (re-read)

- **Phase A scoping** (pipeline-dashboard `docs/audits/2026-05-12-
  automation-registry-scoping.md`): AR-S3 = "cron mechanism (extends
  pipeline-dashboard fa01b290)" L.
- **Schema v1** (`docs/proposals/automation-registry-schema-v1.md`):
  cron entries have `schedule: "<cron-expr>"`,
  `target_kind: python_callable | shell | http | mcp | agent_role`,
  `pre_dispatch_hooks: []`, `escalation: {channel, on_failure, ...}`,
  `enabled: bool`.
- **Existing supervisor** (`pipeline-dashboard/scripts/`,
  ~824 LOC total):
  - `service_supervisor.py` -- public API + backend selection.
  - `_supervisor_windows.py` -- schtasks XML with `LogonTrigger`,
    naming via `Ecosystem-Autostart-<id>`.
  - `_supervisor_macos.py` -- launchd plist with `RunAtLoad: true`.
  - `_supervisor_systemd.py` -- systemd user unit with
    `Restart=on-failure`.
  - All backends export `install(service)`, `uninstall(id)`,
    `status(id)`, `start/stop/restart`.

## Trace: what happens when a cron entry fires

1. Registry boots, parses `automations.yaml`, finds an entry with
   `mechanism: cron`, e.g. `dream-eod-review` (schedule `0 17 * * *`,
   target `dream.orchestrator:run_eod`).
2. Registry routes to the **cron handler**. The handler calls the
   per-platform supervisor's new `install_cron(...)` method with the
   schedule + the command line that re-enters the registry runner
   for this entry.
3. Platform schedules the job natively (Windows CalendarTrigger /
   launchd StartCalendarInterval / systemd OnCalendar).
4. When fire-time arrives, the platform invokes the **registry
   runner**: `python -m automation_registry.runner --entry
   dream-eod-review`.
5. The runner: writes a `run_started` row to sqlite, acquires an
   advisory lock for the entry (per Q4 hybrid), invokes the target
   (Python callable / shell / http / mcp / agent_role), captures
   exit + output, writes a `run_ended` row, releases lock.
6. On non-zero exit OR exception: the **escalation pipeline** fires
   per the entry's `escalation` block. If `on_failure: file_pd_task`,
   the pipeline POSTs to PD MCP `create_task` with idempotent
   title-prefix dedup.

## Sub-sub-task breakdown

| Id | Title | Cx | Repo | Deps |
|---|---|---|---|---|
| AR-S3a | Per-platform `install_cron` / `uninstall_cron` / `status_cron` on the 3 supervisor backends | S | pipeline-dashboard | -- |
| AR-S3b | Cron mechanism handler in automation-registry (reads YAML, calls supervisor, tracks sqlite state) | M | automation-registry | AR-S3a |
| AR-S3c | Registry runner CLI -- `python -m automation_registry.runner --entry <name>` -- entry point the platform schedule invokes | S | automation-registry | AR-S3b |
| AR-S3d | Failure escalation pipeline -- `on_failure: file_pd_task` with title-prefix dedup via PD MCP | S | automation-registry | AR-S3c |
| AR-S3e | Migration: retire `Dream Auto` schtask; install `dream-eod-review` + `dream-morning-summary` as registry cron entries | M | automation-registry + dream | AR-S3d |
| AR-S3f | End-to-end playtest with evidence capture (synthetic 1-min cron entry; observe full lifecycle; force-fail to confirm escalation) | M | automation-registry | AR-S3e |

Total: **3 S + 3 M = 6 sub-sub-tasks**. Each lands with its own
commit. AR-S3a is purely additive (new functions on existing
backends; no existing-call sites change). AR-S3c is the inflection
point -- once it lands the registry can spawn jobs and read their
results.

## AR-S3a -- expanded (the foundation)

Per-platform additions:

- **Windows** (`_supervisor_windows.py`): new `build_cron_task_xml(...)`
  builder that emits `CalendarTrigger` with a `<StartBoundary>` + a
  `<ScheduleByMonth/Week/Day>` block (or the simpler explicit
  `<StartBoundary>` + `<Repetition>` pattern). New `install_cron(name,
  cron_expr, command, cwd)` / `uninstall_cron(name)` / `status_cron`.
  Task naming: `Ecosystem-Cron-<name>` to avoid collision with the
  existing `Ecosystem-Autostart-<id>`.
- **macOS** (`_supervisor_macos.py`): launchd plist with
  `StartCalendarInterval` (dict of Minute/Hour/Day/Month/Weekday from
  cron expr). New `install_cron(...)` etc. Naming:
  `com.toonred.ecosystem.cron.<name>`.
- **systemd** (`_supervisor_systemd.py`): a `.service` unit + a
  `.timer` unit. Timer's `OnCalendar=` is the standard systemd
  syntax derived from cron expr. Naming:
  `ecosystem-cron-<name>.service` + `.timer`.

Cross-cutting (in `service_supervisor.py`): add a `CronJob`
dataclass (mirror of `Service` shape but with `schedule` +
`callback_command` fields) plus public `install_cron(job)` /
`uninstall_cron(name)` / `status_cron(name)` that dispatch to the
selected backend. Add cron-expr-to-platform-format helpers (cron
expr is the canonical input form; each backend translates).

DONE WHEN:
- All 3 backends pass a new pytest suite that validates the
  generated platform-config strings (no actual install required for
  the unit tests).
- One integration test on the current host platform that installs
  + queries + uninstalls a no-op cron entry firing every minute.

## Open architectural questions (none gate AR-S3a)

**Q-A: Where does the registry runner live -- inside
automation-registry as a `python -m` entry, or a standalone CLI
shim?** Recommendation: `python -m automation_registry.runner`. Means
the registry repo must be `pip install -e .`-installed on the host
machine, OR the platform schedule invokes `python -c "import sys; sys.path.insert(0, '<path>'); ..."` directly. AR-S3c picks the
final answer.

**Q-B: How does the runner reach the target's repo (e.g.,
`dream.orchestrator:run_eod`)?** Each target's owning repo has to be
importable from the runner's process. Two paths: (i) the runner
invokes the target's repo as a subprocess (`python -m
dream.orchestrator eod` from `cwd=<dream-repo>`); (ii) the runner
imports across repos (requires sys.path arrangement). Recommendation
(deferred to AR-S3c): SUBPROCESS for cleanliness -- each target runs
in its own process with its own working dir + interpreter env.

**Q-C: Task-name collision risk.** Existing services use
`Ecosystem-Autostart-<id>`; cron entries would use
`Ecosystem-Cron-<name>` on Windows. Verify the prefix split makes
`status_cron` and `status` non-overlapping queries. Trivial in
AR-S3a's unit tests.

## Recommended first sub-sub-task

**AR-S3a** -- per-platform `install_cron` additions. Purely additive
to the existing backends, no behavioral changes to current at-logon
services. Unit tests don't require root / admin. Foundation for
every other AR-S3x.

## After AR-S3a

AR-S3b (cron handler in registry) -> AR-S3c (registry runner CLI)
-> AR-S3d (escalation pipeline) -> AR-S3e (Dream Auto migration)
-> AR-S3f (end-to-end playtest with evidence).

## Cross-references

- Research `fe0302b9` -- registry decision + framework.
- Schema v1 (this repo): `docs/proposals/automation-registry-schema-v1.md`.
- Phase A scoping: pipeline-dashboard
  `docs/audits/2026-05-12-automation-registry-scoping.md`.
- Existing supervisor: `pipeline-dashboard/scripts/service_supervisor.py`
  + per-platform modules.
- PD task `fa01b290` -- "Extend service_supervisor with cron
  mechanism" -- absorbed by AR-S3a.
