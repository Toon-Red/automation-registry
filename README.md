# automation-registry

Catalog of ecosystem automations -- first-class automation entities
with a per-entry **mechanism** (`cron` / `claude_hook` / `claude_loop`
/ `manual` / `executor` / `claude_check` / `claude_blocker_callback`).

Single source of truth for "what is automated, when does it fire, what
mechanism runs it." The registry **catalogs** -- it does not execute
the work itself. Each mechanism delegates to the right backend:

| Mechanism | Backend |
|---|---|
| `cron` | OS-level schedule via schtasks / launchd / systemd-timer (`scripts/service_supervisor.py` cron extension). |
| `claude_hook` | A fragment in the target repo's `.claude/settings.json`. Active only inside open Claude Code sessions. |
| `claude_loop` | An always-on Claude Code session running `/loop`. Lifecycle managed by the registry. |
| `manual` | Listed for visibility -- never auto-dispatched. |
| `executor` | Delegates to an existing executor app -- inaugural target: `automation/` at :5045 (calendar-driven Ruflow swarm trigger). Per Preston's 2026-05-12 REUSE-AS-EXECUTOR framework. |
| `claude_check` | Pre-SOD transparency reports (e.g. origin-sync). Never auto-pushes; reports only. |
| `claude_blocker_callback` | Real-time blocker pings with typed `unblock_condition` (time / event / external) + auto-retry. |

## Why this app exists

Today, automation lives in many uncoordinated places:

- Windows Task Scheduler entries (`Dream Auto` schtask)
- Claude Code hooks (`.claude/hooks/*` across repos)
- CI workflows
- Ad-hoc Python scripts

No single surface answers "what's automated and on what mechanism." This
app is that surface.

## Status

**AR-S2 -- skeleton.** Only `/api/health` is implemented. Loading
`automations.yaml`, dispatch, and per-mechanism backends ship in AR-S3
onward.

## Run

```bash
python app.py                  # binds 127.0.0.1:5050
python app.py --port 5050      # explicit port
PORT=5050 python app.py        # env override
pytest                         # tests
```

## References

- **Research:** PD `fe0302b9` -- decision + REUSE-AS-EXECUTOR framework.
- **Phase A scoping:** `pipeline-dashboard/docs/audits/2026-05-12-automation-registry-scoping.md`.
- **Schema v1:** [docs/proposals/automation-registry-schema-v1.md](docs/proposals/automation-registry-schema-v1.md).
- **Sibling executor:** [automation](https://github.com/Toon-Red/automation) -- calendar-driven Ruflow swarm trigger. Catalogued here under `mechanism: executor`. Its narrow scope is preserved per the framework.
