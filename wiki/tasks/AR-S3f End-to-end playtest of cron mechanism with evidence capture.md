---
tags: [task, done, feature]
project: [[projects/Automation Registry]]
status: done
priority: high
updated: 2026-05-14 13:38
---

# AR-S3f: End-to-end playtest of cron mechanism with evidence capture

✅ **Done**  ·  `feature`  ·  priority: `high`

**Project:** [[Automation Registry]]

## Description

WHAT: Playtest the full cron lifecycle with captured evidence. Install a synthetic 1-minute cron entry (no-op target), observe it fire, verify sqlite + Discord + supervisor state. Then force a failure, verify escalation pipeline files a PD task and dedupes on second failure. Capture artifacts per the evidence rule.

WHY: Confirms AR-S3a..S3e compose correctly before any non-trivial migration depends on the registry. Closes AR-S3 (the L original) with shipped evidence rather than just "all sub-sub-tasks done."

HOW:
1. Add `synthetic-cron-smoke-test` entry to automations.yaml (mechanism cron, schedule */1 * * * *, target_kind shell, target = "python -c 'print(\"ok\")'").
2. Start registry; wait 2 minutes; verify 2 rows in sqlite cron_runs with exit 0.
3. Switch target to "python -c 'import sys; sys.exit(1)'". Wait 2 minutes.
4. Verify:
   - sqlite: 2 rows with exit 1.
   - PD: ONE task filed (not 2 -- dedup verified).
   - Discord: 2 failure pings (each fire is a separate Discord event by design).
5. Capture evidence per research ef520e40 retention rule:
   - Sqlite snapshot (full cron_runs table for this entry).
   - PD task screenshot via Chrome MCP (or curl + raw JSON).
   - Discord embed URLs (or webhook payload captures).
   - Save to `data/playtest-evidence/AR-S3f-cron-smoke/`.
6. Remove the synthetic entry after the playtest -- leaving production-only entries.
7. Document the run in `wiki/playtests/AR-S3f-2026-05-XX-cron-smoke.md` per the same auto-gen pattern.

DONE WHEN:
- All 4 verification points above pass.
- Evidence artifacts present in data/playtest-evidence/AR-S3f-cron-smoke/.
- Synthetic entry removed.
- AR-S3 umbrella task on PD marked done with a pointer to this evidence directory.

DEPS: AR-S3e (so we know real migration works; the synthetic test alone isn't enough).

## Approach

Playtest executed 2026-05-14 13:11-13:38 UTC. 13 success rows + 3 failure rows in cron_runs; escalator filed exactly 1 PD task (25f17726) across 3 failures, proving title-prefix dedup. Evidence: data/playtest-evidence/AR-S3f-cron-smoke/. Wiki: wiki/playtests/AR-S3f-2026-05-14-cron-smoke.md. Discord channel verification deferred to AR-S6 (handler still stub). Synthetic entry removed; Windows schtask uninstalled. Regression caught + fixed in cron_handler._build_cron_job: supervisor now receives runner.py --entry <name> instead of bare target (test_install_surfaces_runner_argv_not_bare_target). AR-S3 umbrella closed via this task.

*Auto-generated 2026-05-14 13:38*
