# AR-S3f cron-smoke playtest evidence

Run: 2026-05-14 (UTC 13:11 -- 13:38)

This directory captures the four verification points from
[wiki/playtests/AR-S3f-2026-05-14-cron-smoke.md](../../../wiki/playtests/AR-S3f-2026-05-14-cron-smoke.md).

| File | What it proves |
| --- | --- |
| `01-reconcile-install.json` | Synthetic entry installed; one schtask entry created (the `installed: [...]` list is non-empty). |
| `02-schtasks-query-installed.txt` | Windows reported `\Ecosystem-Cron-synthetic-cron-smoke-test` present and Ready -- the OS layer agrees. |
| `registry-server.log` | Registry server boot + reconcile log. |
| `03-cron_runs-full-snapshot.json` | Full `cron_runs` table for the entry. 13 `succeeded` + 3 `failed` rows. Source split `schedule` vs `manual` shows the schtask actually fired AND the runner is operator-invocable. |
| `04-pd-task-25f17726.json` | The single PD task filed by the escalator. Body documents run 14 (first failure); the `\n---\n` append-note documents runs 15 & 16 -- proving the dedup path overwrites description, not creating a new task. |
| `05-second-escalator-pass-noop.json` | The escalator was rerun while a third failure was in flight; result still shows `filed: []` -- no duplicate task. |
| `06-pd-cron-fail-task-count.json` | Authoritative dedup proof: GET `/api/projects/automation-registry/tasks` filtered to `[cron-fail]` prefix returns exactly 1 task across all failures. |

## Verification matrix (per task DONE WHEN)

| Verification point | Status | Evidence |
| --- | --- | --- |
| sqlite: >=2 rows with exit 0 | PASS (13 rows) | `03-cron_runs-full-snapshot.json` summary block |
| sqlite: >=2 rows with exit 1 | PASS (3 rows) | same |
| PD: ONE task filed, not N | PASS (1 task, 3 failures) | `06-pd-cron-fail-task-count.json` |
| Discord pings | DEFERRED | Discord channel wiring is not in AR-S3d's scope -- escalator currently logs `discord_only` and skips. The synthetic entry uses `on_failure: file_pd_task`, so the PD path is the live escalation channel exercised here. Discord embed capture moves to AR-S6 (when the discord_only handler ships). |

## Cleanup

Synthetic entry removed from `automations.yaml` post-playtest. A
follow-up reconcile call uninstalled `\Ecosystem-Cron-synthetic-cron-smoke-test`
from the platform schedule. See the wiki page for the full run log.
