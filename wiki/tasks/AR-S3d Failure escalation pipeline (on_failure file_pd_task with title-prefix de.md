---
tags: [task, done, feature]
project: [[projects/Automation Registry]]
status: done
priority: high
updated: 2026-05-14 13:38
---

# AR-S3d: Failure escalation pipeline (on_failure: file_pd_task with title-prefix dedup)

✅ **Done**  ·  `feature`  ·  priority: `high`

**Project:** [[Automation Registry]]

## Description

WHAT: Implement the escalation pipeline. After AR-S3c records a failed run in sqlite, the escalation pipeline reads the entry's `escalation` block and acts: for `on_failure: file_pd_task`, POST to PD via MCP `create_task` with title prefix `Playtest failure:` or `Automation failure:`. Idempotent dedup: if an open task with the same title prefix + same entry name exists, append a new line to that task's description rather than file a duplicate.

WHY: Process improvement #2 (Preston-decided 2026-05-12). Without this, failed automations are silent -- the registry just has a sqlite row nobody reads.

HOW:
1. New module `automation_registry/escalation.py` with `class EscalationPipeline` exposing `handle_run_end(run_id)`.
2. Called from the runner (AR-S3c) at the end of each non-zero run -- one line: `EscalationPipeline().handle_run_end(run_id)`. Alternatively a daemon poller on cron_runs; AR-S3c's choice.
3. Channels:
   - `discord`: post to webhook (use Dream's existing discord helper as the pattern; do NOT import dream/ directly -- duplicate the 30-LOC helper since it's small).
   - `log_only`: write to logger.
   - `toast`: deferred (out of scope).
   - `none`: no-op.
4. on_failure rules:
   - `file_pd_task`: POST to http://127.0.0.1:5100/api/projects/{pd_project}/tasks. Body: title with the prefix + entry name; description with WHAT (entry name + schedule), WHY (link to run_id), HOW (stderr excerpt), DONE WHEN (fresh successful run). Idempotency: GET open tasks for the project, find any with matching title prefix + entry name, and update its description rather than create.
   - `discord_only`: ping channel; don't file PD.
   - `log_only`: logger only.
5. retry block (per schema): if `retry.max_attempts` > 1, the escalation can re-invoke the runner up to N times with backoff_s between. (Defer the re-invoke to AR-S3d only if straightforward -- otherwise mark as a follow-on.)

DONE WHEN:
- Synthetic failing entry triggers file_pd_task; PD shows the new task.
- Re-running the same failure does NOT file a duplicate -- appends to the existing task's description.
- pytest covers the dedup path (mocked PD MCP client) + each channel.

DEPS: AR-S3c (runner records the failure).

*Auto-generated 2026-05-14 13:38*
