---
tags: [task, done, feature]
project: [[projects/Automation Registry]]
status: done
priority: high
updated: 2026-05-14 13:38
---

# AR-S3b: Cron mechanism handler in automation-registry (YAML loader + supervisor invocation + sqlite state)

✅ **Done**  ·  `feature`  ·  priority: `high`

**Project:** [[Automation Registry]]

## Description

WHAT: Implement the cron mechanism handler inside automation-registry. On startup (and on YAML change), parse automations.yaml entries with `mechanism: cron`, validate per the v1 schema, and call the supervisor's `install_cron(...)` for each. Maintain sqlite state for each cron entry (entry_name, last_install_time, schedule, target, target_kind, enabled, last_run_id).

WHY: This is the registry's authoritative bridge between the catalog (yaml) and the platform schedule (supervisor). Without this layer the supervisor would need its own catalog -- duplicating responsibility.

HOW:
1. New module `automation_registry/cron.py` with `class CronHandler` exposing `sync_from_yaml(entries)`, `enable(name)`, `disable(name)`, `status(name)`.
2. sqlite schema in `data/registry.db`: tables `cron_entries`, `cron_runs` (the latter populated by AR-S3c runner, but schema defined here).
3. Wire CronHandler into app.py startup: on @app.on_event("startup"), parse YAML and sync. Add /api/cron endpoints: GET /api/cron/entries (list), POST /api/cron/sync (reload from YAML), GET /api/cron/entries/{name} (one entry + last run).
4. Schema validators per the v1 doc: schedule non-null for cron, target_kind in allowed set, escalation present.

DONE WHEN:
- python app.py boots, reads automations.yaml with at least one cron entry (synthetic test fixture), invokes the supervisor's install_cron stub (mocked), persists state to sqlite.
- GET /api/cron/entries returns the synced list with status from sqlite.
- pytest covers: empty yaml, valid cron entry, invalid entry (rejected with clear error), YAML reload picks up adds/removes.

DEPS: AR-S3a (supervisor cron API).

NOT IN SCOPE: the runner itself (S3c), escalation (S3d), real Dream migration (S3e), playtest (S3f).

*Auto-generated 2026-05-14 13:38*
