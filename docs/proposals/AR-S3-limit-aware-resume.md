# AR-S3 -- limit-aware auto-resume infrastructure

> First-class design for the runtime that backs the
> `claude_loop_continuous` mechanism. Per Preston 2026-05-14: at
> session start, check next limit reset; set a timer; on reset, if
> Claude is idle, resume; if working, do nothing.

## What this is

A daemon-shaped runtime that:

1. **Plans the work envelope.** Reads the current quota state, the
   next reset boundary, and the configured `pause_at_remaining_pct`
   safety margin. Computes a "burn window" -- how long it can run
   before backing off.
2. **Runs the loop.** Inside the burn window, repeatedly picks the
   next work item (calendar task, PD priority queue, etc.), invokes
   the target with a freshly-rendered `/goal`, awaits completion.
3. **Pauses on quota.** When the safety margin is hit OR the API
   reports rate-limit / quota errors, the loop pauses cleanly: it
   finishes the current target if mid-flight, records state, and
   sleeps until reset.
4. **Resumes on reset.** When reset time arrives, the resume check
   runs: is Claude idle (no active session doing real work)? If
   idle, kick off the next iteration. If working, do nothing this
   tick -- check again on the next heartbeat.

## Open design questions (Q-A through Q-G)

These are the calls the AR-S3h implementation task makes. Most
have a tentative answer; Preston's call confirms or overrides.

**Q-A: How does the loop detect "the limit just reset"?**

Three candidates:

  a. Poll a known Anthropic API endpoint that returns remaining
     quota + reset timestamp.
  b. Parse the response headers of a Claude API call. Anthropic's
     `x-ratelimit-*` headers carry remaining tokens + reset.
  c. Schedule a wakeup at the predicted reset time and verify by
     attempting a tiny test call.

  Tentative: **(b)** -- piggyback on real calls. No extra polling
  cost. The Stop-hook protocol Claude Code uses already exposes the
  raw response; we wrap it. If the test call still 429s, retry with
  exponential backoff.

  **Needs verification: does Claude Code surface the
  `x-ratelimit-*` headers anywhere a hook can read?** If not, fall
  back to (c).

**Q-B: How does the loop detect "is Claude currently busy"?**

Three candidates:

  a. Heartbeat file: each active Claude Code session touches
     `~/.claude/heartbeat-<pid>` every N seconds; resume-checker
     looks for any recent heartbeat.
  b. Process check: enumerate `claude` / `pythonw` processes whose
     command line includes a session marker.
  c. Session count via Claude Code's session-list API (if exposed).

  Tentative: **(a)** heartbeat file -- already a pattern in our
  `automation/` app and `dream/`. Cross-platform, no privileged
  process enumeration, integrates with the existing rhythm.

**Q-C: Where does the timer live?**

  a. Inside `claude_loop_continuous`'s own runtime (a background
     thread within the loop process).
  b. A separate daemon (e.g. `automation-registry/resume_watcher.py`).
  c. Part of Dream's tab UI (a JS-side timer).

  Tentative: **(a)** -- the timer is part of the loop runtime. The
  loop process is already what should know when to back off; making
  it own the resume is the simplest. If the loop process dies, the
  outer registry restarts it via Claude Desktop scheduled-task
  watchdog (Tier 1 mechanism wraps Tier 1 mechanism -- self-hosting).

**Q-D: What state persists across pause/resume?**

  a. Just the active work item ID (calendar task) -- everything else
     re-derived from PD on resume.
  b. Active work item + intermediate progress markers + last
     successful subprocess output.

  Tentative: **(a)** -- thin state. PD is the durable record; the
  loop just needs to know "where I was when I paused." If a
  half-finished task is mid-flight at pause, it's marked
  `in_progress, paused_at=<ts>` so PD can render that state.

**Q-E: Hard limit vs soft limit -- what's the safety margin?**

  Tentative: pause at **5% remaining**. Anthropic's hard limit will
  reject calls; we don't want to be 100% efficient on the limit and
  end with a failed mid-flight call. 5% gives one extra task buffer
  for typical task sizes.

  Configurable per entry via `limit_aware.pause_at_remaining_pct`.

**Q-F: What if the loop crashes mid-pause?**

  Recovery: the outer Claude Desktop scheduled task (Tier 1) acts
  as a watchdog. A `claude_loop_continuous` entry has a paired
  Desktop scheduled task that fires every 30 min during work hours;
  it checks if the loop process is alive, and if not, restarts it.
  The restart picks up state from sqlite.

  This is self-hosting: a Tier 1 watchdog supervises a Tier 1
  worker.

**Q-G: How does the loop pick work items?**

  Tentative source-of-truth precedence:

  1. Today's Calendar entries with `status=ready`.
  2. PD priority queue (high-priority tasks).
  3. Idle behaviour: log + sleep one cycle.

  This is where `/goal` integration lands (AR-S3j) -- each picked
  work item becomes the `/goal` text for the iteration.

## Subscription quota math

For a Max plan with daily reset at 04:00 PT:

  * 24h cycle = 86400 seconds.
  * Burn window = (reset_ts - now) - safety margin.
  * Pause at `remaining_pct=5%` of the day's quota OR when
    `x-ratelimit-remaining` falls below the per-task estimate.

For Pro: same math, smaller quota -> shorter burn window.

The loop should LOG the planned burn window at start of each
session (the operator sees "next pause expected at HH:MM" and
"target reset HH:MM").

## Open question for Preston

**Q-MAIN: Is `x-ratelimit-*` accessible inside a Claude Code Stop
hook?** If yes, Q-A's (b) is the implementation. If no, we need a
side-channel (manual ping of the API at start of each iteration to
read headers). The AR-S3h implementation task can't ship without
this answer. Filing a research item alongside this doc to
investigate.

## Cross-references

- Schema v2: `automation-registry-schema-v2.md`.
- AR-S3e reshape: `AR-S3e-dream-auto-migration.md` (revised).
- `/goal` integration: filed as AR-S3j.
- Subscription cost evaluation:
  `AR-S3-claude-scheduled-evaluation.md`.
