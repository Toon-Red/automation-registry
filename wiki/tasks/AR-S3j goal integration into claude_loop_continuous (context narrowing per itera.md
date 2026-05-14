---
tags: [task, done, feature]
project: [[projects/Automation Registry]]
status: done
priority: high
updated: 2026-05-14 13:38
---

# AR-S3j: /goal integration into claude_loop_continuous (context narrowing per iteration)

✅ **Done**  ·  `feature`  ·  priority: `high`

**Project:** [[Automation Registry]]

## Description

WHAT: Wire Claude Code's built-in `/goal` slash command into the `claude_loop_continuous` runtime. At the start of each L8-L4 iteration, the current work item (surfaced by L8 from PD + calendar) renders into the entry's `goal_template`; the runtime fires `/goal <text>` in the appropriate layer's session. The Stop hook auto-evaluates per turn; when the goal is met, the iteration returns and L8 surfaces the next item.

WHY: Preston 2026-05-14: "we should be using /goal if we're not already." Aligns with the wiki-driven context narrowing principle -- the agent only needs the context relevant for what it's doing right now.

L8-L4 hierarchy context (per AC c1779970 2026-05-13 amendment + correction):
  L4 = gruntwork (coder, qa, playtester, grader-high-compute)
  L5 = managers + guides for L4s
  L6 = ruflow queen
  L7 = Dispatch
  L8 = PM (Preston oversight)

`/goal` is set per iteration, sourced from the current calendar task that L8 has surfaced. L8 makes the work-item decision; the layer below (likely L7 or directly L6) consumes the rendered /goal.

HOW (per claude-code-guide investigation):
1. `/goal` is built-in, session-scoped, Stop-hook-evaluated (Haiku judges completion per turn). Not persisted to disk between separate sessions.
2. `goal_template` field on claude_loop_continuous entries (schema v2). Example: "Process the next calendar task; stop when done or quota near limit."
3. Each iteration: L8 surfaces the work item; runtime renders the template; runtime invokes `claude -p "/goal <rendered>"` in the receiving layer's session.
4. Goal clears when met OR when `/clear` runs OR when the iteration explicitly clears.
5. Layer choice for which session receives the /goal: configurable; default L7 (Dispatch).

DONE WHEN:
- claude_loop_continuous entries declare goal_template.
- A synthetic loop_continuous run surfaces a work item via L8, renders template, fires /goal at the configured layer.
- A test verifies the slash command is dispatched with the rendered text + the receiving layer is correct.
- Wiki-driven context narrowing pattern is documented (/goal narrows turn-by-turn within an iteration; wikis narrow per-task context across iterations).

DEPS: AR-S3h (the L8-L4 runtime); AC-S16 (L8 surfaces work items).

NOT IN SCOPE: /context / /compact / /clear customisation; wiki-loading mechanism (separate sub-task).

*Auto-generated 2026-05-14 13:38*
