---
tags: [playtest, automation-registry, AR-S3, cron]
project: [[projects/Automation Registry]]
task: AR-S3f
date: 2026-05-14
status: passed
---

# AR-S3f — Cron mechanism end-to-end playtest (synthetic-cron-smoke-test)

> **Status:** PASS · **Run window:** 2026-05-14 13:11 → 13:38 UTC
> **Operator:** prestonrobertlee@gmail.com
> **Evidence dir:** [`data/playtest-evidence/AR-S3f-cron-smoke/`](../../data/playtest-evidence/AR-S3f-cron-smoke/)

This page documents the AR-S3 closing playtest: a synthetic 1-fire-per-2-minute
cron entry was installed against the real Windows scheduler, exercised through
both the success and failure paths, and removed. The point was not to prove the
cron _runs_ (AR-S3b already shipped that) but to prove that AR-S3a..S3e
**compose** — that one fire produces one sqlite row, one Discord-or-PD escalation
per failure, and exactly one PD task across N consecutive failures via the
title-prefix dedup from AR-S3d.

## Scope

Closes AR-S3 ("the L original"). All AR-S3 sub-sub-tasks (S3a..S3e) had passing
unit tests but no end-to-end evidence. This playtest is the shipped-evidence
gate AR-S3f exists to satisfy.

## What was tested

| AR-S3 sub-sub-task | Composed into this playtest as |
| --- | --- |
| S3a — per-platform `install_cron` | Verified by `schtasks /Query` after reconcile (Windows) |
| S3b — YAML loader + supervisor reconcile + sqlite | Reconcile installed entry; cron_runs got 16 rows |
| S3c — runner CLI (`runner.py --entry <name>`) | All 14 schedule-source rows came through the runner, not bare target |
| S3d — escalation pipeline w/ title-prefix dedup | 3 failures → 1 PD task (`25f17726`) with appended re-fire notes |
| S3e — Dream Auto migration scoping (no migration yet) | Confirmed the runner argv path generalises before any real migration depends on it |

## Synthetic entry used (added then removed)

```yaml
- name: synthetic-cron-smoke-test
  description: AR-S3f smoke test (added + removed in one session).
  owner_project: automation-registry
  mechanism: cron
  schedule: "*/2 * * * *"   # Windows schtasks minimum granularity is 1 min;
                            # */2 paces the playtest at a comfortable cadence.
  target_kind: shell
  # Success phase:
  target: 'python -c "print(\"ok\")"'
  # Failure phase (mid-playtest swap):
  # target: 'python -c "\"import sys; sys.exit(1)"'
  escalation:
    channel: pd
    on_failure: file_pd_task
  enabled: true
```

The failure-phase target is deliberately malformed (unterminated string
literal) so the worker exits non-zero on every fire — which is exactly what
the dedup gate wants to see.

## Procedure

1. Append synthetic entry to `automations.yaml` (success-phase target).
2. `POST /api/registry/reconcile` → expect `{"installed":["synthetic-cron-smoke-test"],...}`.
3. Confirm Windows side: `schtasks /Query /TN \Ecosystem-Cron-synthetic-cron-smoke-test`.
4. Wait ~26 minutes; observe 13 succeeded rows in `cron_runs`.
5. Swap `target:` to the failing variant; reconcile (reinstall).
6. Let it fire 3× and run the escalator twice (covering "first failure → file"
   and "third failure → append-note, no new task").
7. Snapshot sqlite, PD task, and PD task-count.
8. Remove entry from `automations.yaml`; reconcile → expect uninstall.
9. Confirm Windows side: `schtasks /Query ... → ERROR: cannot find file`.

## Results — verification matrix

| Verification point (from task DONE WHEN) | Status | Evidence file |
| --- | --- | --- |
| sqlite: ≥ 2 rows with exit 0 | **PASS** (13 rows) | `03-cron_runs-full-snapshot.json` |
| sqlite: ≥ 2 rows with exit 1 | **PASS** (3 rows) | `03-cron_runs-full-snapshot.json` |
| PD: ONE task filed (not N) — dedup verified | **PASS** (1 task, 3 failures) | `06-pd-cron-fail-task-count.json` |
| Discord pings on each failure | **DEFERRED to AR-S6** | (see below) |

### Why Discord verification is deferred

The synthetic entry escalates via `on_failure: file_pd_task`, which is the
PD-task channel and the live channel AR-S3d implements. The Discord-only
handler (`discord_only`) is still a stub that logs and skips — wiring it to
a real webhook is in scope for AR-S6, not AR-S3. Filing _and_ Discord on the
same entry is supported by the schema (both can be enabled per channel) but
nothing exercises the wire today. AR-S3 closes on the PD-task path; the
Discord acceptance row migrates to the AR-S6 playtest.

## Findings shipped from this run

1. **cron_handler regression caught.** The cron supervisor was being handed
   `automation.target` directly, bypassing `runner.py`. That works (the OS
   still runs the command) but produces zero `cron_runs` rows and zero
   escalations — silent observability loss. Fix in `cron_handler._build_cron_job`
   surfaces `runner.py --entry <name>` instead. Locked in by
   `tests/test_cron_handler.py::TestReconcile::test_install_surfaces_runner_argv_not_bare_target`.
2. **Dedup proves out across runner restarts.** Run 16 came in on schedule
   _after_ the escalator had already run once with runs 14+15. The escalator's
   second pass returned `appended: ["synthetic-cron-smoke-test"]` and
   `filed: []` — i.e. it found the existing `[cron-fail]` task, appended a
   re-fire note, and did not file a duplicate. Authoritative count via
   `GET /api/projects/automation-registry/tasks` filtered to `[cron-fail]`
   prefix returned **1**.
3. **Cleanup is symmetric.** Removing the YAML entry and reconciling triggered
   `uninstall_cron`; the Windows side confirmed the schtask is gone
   (`schtasks /Query` returns exit 1). No orphaned platform schedules.

## Cleanup

- Synthetic entry removed from `automations.yaml` (file is now `automations: []`).
- Windows schtask `\Ecosystem-Cron-synthetic-cron-smoke-test` uninstalled.
- The `[cron-fail] synthetic-cron-smoke-test` task on PD (`25f17726`) remains as
  the audit trail of the failure phase; its `auto_complete: true` flag means it
  will close on the next successful run of the entry — which won't happen,
  since the entry is removed. The task is closed by hand as part of this
  playtest's cleanup (it is not a real production failure).

## Cross-references

- Evidence directory README: [`data/playtest-evidence/AR-S3f-cron-smoke/README.md`](../../data/playtest-evidence/AR-S3f-cron-smoke/README.md)
- Task: [[AR-S3f End-to-end playtest of cron mechanism with evidence capture]]
- Escalation pipeline: AR-S3d (`b33d6d61`)
- Runner CLI: AR-S3c (`30be7871`)
- Cron handler: AR-S3b (`6f7569d9`)

*Generated 2026-05-14 manually as the AR-S3 closing artifact.*
