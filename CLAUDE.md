# automation-registry

> Catalog of ecosystem automations with per-entry mechanism.
> AR-S2 skeleton: only `/api/health` is implemented; dispatch + per-mechanism backends ship in AR-S3+.

## Why this project exists

**Problem:** Automation is fragmented across schtasks, Claude hooks, ad-hoc Python -- no single place to see what's automated and on what mechanism.

**In scope:** Single registry for all ecosystem automations. Entries declare a mechanism (cron / claude_hook / claude_loop / manual / executor / claude_check / claude_blocker_callback). Catalog only -- dispatches via the right backend, delegates execution to the right app.

**Out of scope:** Executing the work itself (each mechanism's backend handles that -- `executor` delegates to `automation/`; `cron` runs schtasks/launchd/systemd; `claude_hook` runs in active Claude sessions). Storing schedules (Calendar). Lifecycle / work tracking (Pipeline Dashboard).

## State

- stage: `dev`
- version: `0.1.0`
- branch: `master`

## Integration boundaries

**Depends on:** Pipeline Dashboard (project records, escalation targets); Calendar (scheduled-event source for cron entries); automation/ (executor mechanism delegate).
**Serves:** Dream (UI tab consumer -- AR-S10); operators (catalog + audit + toggle surface).

## Native primitives

<!-- BEGIN PRIMITIVES -->
- `.claude/hooks/health_check.py` -- SessionStart probe.
- `.claude/settings.json` -- registers SessionStart hook.
- `.claude-plugin/plugin.json` -- Claude Code plugin manifest.
<!-- END PRIMITIVES -->

## Pointers

- Dashboard: http://127.0.0.1:5100 (the PD project record at `/api/projects/automation-registry`).
- Schema v1: [docs/proposals/automation-registry-schema-v1.md](docs/proposals/automation-registry-schema-v1.md).
- Phase A scoping: pipeline-dashboard `docs/audits/2026-05-12-automation-registry-scoping.md`.
- Research: PD `fe0302b9`.
