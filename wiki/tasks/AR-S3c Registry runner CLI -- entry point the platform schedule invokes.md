---
tags: [task, done, feature]
project: [[projects/Automation Registry]]
status: done
priority: high
updated: 2026-05-14 13:38
---

# AR-S3c: Registry runner CLI -- entry point the platform schedule invokes

✅ **Done**  ·  `feature`  ·  priority: `high`

**Project:** [[Automation Registry]]

## Description

WHAT: Implement `python -m automation_registry.runner --entry <name>` -- the CLI the platform-scheduled job actually invokes when it fires. The runner: looks up the entry in sqlite + automations.yaml, acquires an advisory file lock, writes a `run_started` row to `cron_runs`, dispatches to the target via subprocess (per Q-B decision -- target runs in its own process with its own cwd), captures stdout/stderr/exit_code, writes `run_ended` row, releases lock.

WHY: This is what the platform schedule literally calls. Without it, AR-S3a's install_cron has nothing meaningful to invoke at fire time.

HOW:
1. New module `automation_registry/runner.py` with `argparse` CLI: `--entry <name>` (required).
2. target_kind dispatch:
   - `python_callable`: subprocess `python -c "from <module> import <fn>; <fn>()"` with `cwd` resolved to the target's owning repo (lookup via PD).
   - `shell`: subprocess of the literal target with cwd = owner_project's repo path.
   - `http`: urllib POST to the URL.
   - `mcp`: invoke the named MCP tool via the registry's MCP client (stub OK at AR-S3c; real wiring at AR-S3e if needed).
   - `agent_role`: deferred -- raise `NotImplementedError` with a pointer to AC-S10 multi-engine driver.
3. Advisory file lock at `data/locks/<entry>.lock` using `portalocker` (cross-platform) or stdlib `fcntl`/`msvcrt` fallback. Per Q4 hybrid: lock is per-entry, not global.
4. sqlite writes:
   - run_id, entry_name, started_at, ended_at, exit_code, stdout_excerpt (first 8KB), stderr_excerpt (first 8KB).
5. Exit codes propagate to platform schedule (non-zero -> platform "last result" reflects failure).

DONE WHEN:
- `python -m automation_registry.runner --entry <synthetic-noop-entry>` writes 2 sqlite rows + returns exit 0.
- Forcing the target to exit 1 writes the failure exit_code and stderr excerpt.
- Concurrent invocation with same --entry is rejected by the lock (second invocation exits with a clear "already running" message + non-zero).
- pytest covers all 4 implemented target_kinds + lock behavior + sqlite schema.

DEPS: AR-S3b (cron handler / sqlite schema).

NOT IN SCOPE: escalation (S3d -- the runner just records the failure; the escalation pipeline reads sqlite and acts).

*Auto-generated 2026-05-14 13:38*
