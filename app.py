"""Automation Registry -- catalog of ecosystem automations.

FastAPI service that owns the source of truth for "what is automated,
when does it fire, what mechanism runs it." Catalog only -- execution
is delegated to per-mechanism backends (cron / claude_hook /
claude_loop / manual / executor / claude_check / claude_blocker_callback).

This file is the SKELETON shipped by AR-S2. It exposes only /api/health
right now; loading automations.yaml, dispatching, and the per-mechanism
backends ship in AR-S3+ sub-tasks under research fe0302b9.

References:
  - Phase A scoping: pipeline-dashboard/docs/audits/2026-05-12-automation-registry-scoping.md
  - Schema v1:       docs/proposals/automation-registry-schema-v1.md
  - PD research:     fe0302b9 (decided + framework applied)
  - REUSE-AS-EXECUTOR for automation/ at port 5045.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

log = logging.getLogger("automation-registry")

DEFAULT_PORT = 5050
APP_VERSION = "0.1.0"

ROOT = Path(__file__).resolve().parent
REGISTRY_YAML = ROOT / "automations.yaml"
REGISTRY_DB = ROOT / "data" / "registry.db"
REGISTRY_QUOTA = ROOT / "data" / "quota_state.json"

_STARTED_AT = time.time()

app = FastAPI(
    title="Automation Registry",
    version=APP_VERSION,
    description=(
        "Catalog of ecosystem automations. Skeleton (AR-S2); loading + "
        "dispatch ships in AR-S3+."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    """Liveness probe. Confirms the HTTP server is up and the registry
    YAML is reachable on disk (does not parse it yet -- that's AR-S3).
    """
    return {
        "status": "ok",
        "version": APP_VERSION,
        "uptime_seconds": int(time.time() - _STARTED_AT),
        "registry_yaml_present": REGISTRY_YAML.is_file(),
        "registry_yaml_path": str(REGISTRY_YAML),
    }


@app.get("/")
def root() -> dict:
    """Tiny landing pointer."""
    return {
        "name": "automation-registry",
        "version": APP_VERSION,
        "docs": "/docs",
        "health": "/api/health",
        "status": (
            "cron handler online -- AR-S3b (runner ships in AR-S3c); "
            "claude_routine surface online as deferred stub -- AR-S3i"
        ),
    }


# -- Operator-anchored quota state (AR-S3k phase A) ----------------

@app.get("/api/registry/quota")
def get_quota_state():
    """Return the current operator-anchored quota state.

    Shape: ``{"session": {"reset_at": ISO, "set_at": ISO}, "weekly": {...}}``.
    Either key may be absent. 404 if the file doesn't exist yet.
    """
    from fastapi.responses import JSONResponse
    import quota_state as _qs
    state = _qs.load_state(REGISTRY_QUOTA)
    if state is None:
        return JSONResponse({"error": "no quota state recorded yet"},
                            status_code=404)
    return state


@app.post("/api/registry/quota")
async def post_quota_state(request: Request):
    """Record a reset timestamp for one subscription window.

    Body: ``{"reset_at": "ISO 8601 ts", "window_kind": "session" | "weekly"}``.
    Idempotent on the unaffected kind. Returns the full stored state.
    """
    from fastapi.responses import JSONResponse
    import quota_state as _qs
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body must be valid JSON"},
                            status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"},
                            status_code=400)
    reset_at = body.get("reset_at")
    kind = body.get("window_kind")
    try:
        state = _qs.write_kind(REGISTRY_QUOTA, kind, reset_at)
    except _qs.QuotaStateError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return state


# -- Cron mechanism endpoints (AR-S3b) -----------------------------

@app.get("/api/registry/cron")
def list_cron_entries() -> dict:
    """Return the sqlite-backed list of installed cron entries."""
    import cron_handler
    return {"entries": cron_handler.list_installed(REGISTRY_DB)}


@app.post("/api/registry/reconcile")
def reconcile_endpoint() -> dict:
    """Make platform state match automations.yaml for cron entries.
    Returns the diff of installs / reinstalls / unchanged / uninstalls.
    Idempotent."""
    import cron_handler
    result = cron_handler.reconcile(REGISTRY_YAML, REGISTRY_DB)
    return result.as_dict()


@app.post("/api/registry/escalate")
def escalate_endpoint() -> dict:
    """Run one escalation pass. Returns the per-action counts.
    Idempotent -- already-escalated rows are skipped."""
    import escalator
    out = escalator.process_failed_runs(
        yaml_path=REGISTRY_YAML, db_path=REGISTRY_DB,
    )
    return out.as_dict()


# -- Desktop scheduled mechanism endpoints (AR-S3g) -----------------

@app.get("/api/registry/desktop_scheduled")
def list_desktop_scheduled() -> dict:
    """Return the sqlite-backed list of Desktop scheduled entries."""
    import desktop_scheduled_handler as dsh
    return {"entries": dsh.list_installed(REGISTRY_DB)}


@app.post("/api/registry/desktop_scheduled/reconcile")
def reconcile_desktop_scheduled() -> dict:
    """Reconcile yaml-desired vs sqlite-known state for the
    claude_desktop_scheduled mechanism. Time-of-day entries return
    pending MCP ops the operator (or L8 agent) materialises;
    state-aware entries install/update their SessionStart hooks
    directly. Idempotent."""
    import desktop_scheduled_handler as dsh
    return dsh.reconcile(REGISTRY_YAML, REGISTRY_DB).as_dict()


@app.post("/api/registry/desktop_scheduled/ack")
async def ack_desktop_scheduled(request: Request) -> dict:
    """Operator confirms a set of time-of-day MCP ops were applied
    successfully. Body: {task_ids: [name, ...]}."""
    import desktop_scheduled_handler as dsh
    body = await request.json()
    task_ids = body.get("task_ids") or []
    count = dsh.ack_applied(REGISTRY_DB, task_ids)
    return {"acknowledged": count, "task_ids": task_ids}


# -- claude_loop_continuous endpoints (AR-S3h) ----------------------

@app.get("/api/registry/loop_continuous")
def list_loop_continuous() -> dict:
    """Return the sqlite-backed view of claude_loop_continuous entries
    + their pause/resume state."""
    import loop_continuous_handler as lch
    return {"entries": lch.list_installed(REGISTRY_DB)}


@app.post("/api/registry/loop_continuous/reconcile")
def reconcile_loop_continuous() -> dict:
    """Make sqlite state match the YAML for claude_loop_continuous
    entries. Idempotent. Does NOT spawn the L8-L4 stack -- that is
    triggered separately via dispatch_iteration / the watchdog."""
    import loop_continuous_handler as lch
    return lch.reconcile(REGISTRY_YAML, REGISTRY_DB).as_dict()


@app.post("/api/registry/loop_continuous/dispatch")
async def dispatch_loop_continuous(request: Request) -> dict:
    """Run ONE iteration of an entry's L8-L4 stack. Body:
    ``{"entry_name": str}``. Returns the iteration outcome."""
    import loop_continuous_handler as lch
    body = await request.json()
    name = body.get("entry_name")
    if not name:
        return {"error": "entry_name required"}
    result = lch.dispatch_iteration(
        name, yaml_path=REGISTRY_YAML, db_path=REGISTRY_DB,
    )
    return result.as_dict()


@app.post("/api/registry/loop_continuous/pause")
async def pause_loop_continuous(request: Request) -> dict:
    """Manually pause an entry (operator can force a pause). Body:
    ``{"entry_name": str, "reason": str}``."""
    import loop_continuous_handler as lch
    body = await request.json()
    name = body.get("entry_name")
    if not name:
        return {"error": "entry_name required"}
    lch.pause_entry(REGISTRY_DB, name,
                     reason=body.get("reason") or "manual",
                     next_reset_ts=body.get("next_reset_ts"))
    return {"ok": True, "entry_name": name, "status": "paused"}


@app.post("/api/registry/loop_continuous/resume")
async def resume_loop_continuous(request: Request) -> dict:
    """Manually resume a paused entry. Body: ``{"entry_name": str}``."""
    import loop_continuous_handler as lch
    body = await request.json()
    name = body.get("entry_name")
    if not name:
        return {"error": "entry_name required"}
    lch.resume_entry(REGISTRY_DB, name)
    return {"ok": True, "entry_name": name, "status": "running"}


@app.post("/api/registry/loop_continuous/watchdog")
def watchdog_loop_continuous() -> dict:
    """One pass of the watchdog (Q-F). Resumes entries whose quota
    reset has passed; flags hung entries as crashed. Idempotent --
    safe to call from a Desktop scheduled task on a 30-min cadence."""
    import loop_continuous_handler as lch
    results = lch.watchdog_check(REGISTRY_DB)
    return {"results": [r.as_dict() for r in results]}


@app.post("/api/registry/loop_continuous/probe_quota")
def probe_quota_endpoint() -> dict:
    """Run the Q-MAIN quota probe + persist the result. Returns the
    snapshot. Uses ANTHROPIC_API_KEY from env; falls back to the
    operator-configured timer file if absent."""
    import quota_probe
    state = quota_probe.probe_quota()
    return state.as_dict()


# -- Smart auto-resume endpoints (AR-S3k) ---------------------------

@app.get("/api/registry/smart_resume")
def list_smart_resume() -> dict:
    """Return all currently-pending (and recently-fired/cancelled)
    one-shot resume timers. The Pipeline Dashboard renders this beside
    each entry so the operator can see what the registry is waiting on
    AND verify that no fixed-interval polling is happening during a
    locked-out window."""
    import smart_resume
    return {
        "pending": smart_resume.list_pending(REGISTRY_DB,
                                              status="pending"),
        "history": smart_resume.list_pending(REGISTRY_DB,
                                              status=("fired", "cancelled")),
    }


@app.get("/api/registry/smart_resume/status/{entry_name}")
def smart_resume_status(entry_name: str) -> dict:
    """Compact status for a single entry -- has_pending, fire_at,
    seconds_until_fire. Used by the Dashboard's per-entry chip."""
    import smart_resume
    return smart_resume.status(entry_name, db_path=REGISTRY_DB).as_dict()


@app.post("/api/registry/smart_resume/schedule")
async def smart_resume_schedule(request: Request) -> dict:
    """Compute fire_at = next_reset_ts + buffer from the persisted quota
    probe state and install (or replace) a one-shot resume timer.

    Body: ``{"entry_name": str,
              "buffer_seconds": int (optional, default 60)}``.

    Errors with HTTP 200 + ``{"error": "..."}`` if no probe state is on
    disk yet -- the operator should hit
    ``POST /api/registry/loop_continuous/probe_quota`` first."""
    import smart_resume
    import quota_probe
    body = await request.json()
    name = body.get("entry_name")
    if not name:
        return {"error": "entry_name required"}
    buf = int(body.get("buffer_seconds") or smart_resume.DEFAULT_BUFFER_SECONDS)
    try:
        result = smart_resume.apply_from_quota_state(
            name, db_path=REGISTRY_DB, buffer_seconds=buf,
        )
    except quota_probe.QuotaProbeError as exc:
        return {"error": str(exc)}
    return result.as_dict()


@app.post("/api/registry/smart_resume/cancel")
async def smart_resume_cancel(request: Request) -> dict:
    """Cancel any pending one-shot for ``entry_name``. Idempotent --
    returns ``cancelled: False`` if nothing was waiting."""
    import smart_resume
    body = await request.json()
    name = body.get("entry_name")
    if not name:
        return {"error": "entry_name required"}
    cancelled = smart_resume.cancel_resume(name, db_path=REGISTRY_DB)
    return {"entry_name": name, "cancelled": cancelled}


@app.post("/api/registry/smart_resume/mark_fired")
async def smart_resume_mark_fired(request: Request) -> dict:
    """Record that an installed one-shot actually fired. Called by the
    runner (or by the operator if a manual fire happened) so the
    sqlite-backed history reflects reality."""
    import smart_resume
    body = await request.json()
    name = body.get("entry_name")
    if not name:
        return {"error": "entry_name required"}
    smart_resume.mark_fired(name, db_path=REGISTRY_DB)
    return {"entry_name": name, "status": "fired"}


# -- claude_routine endpoints (AR-S3i, deferred stub) ---------------

@app.get("/api/registry/routine")
def list_routine() -> dict:
    """Return the sqlite-backed view of claude_routine entries the
    registry has seen. AR-S3i ships as a deferred stub -- rows are
    recorded with ``status: deferred`` until a real Routines client is
    wired into the reconcile pass (Q-PRESTON)."""
    import routine_handler as rh
    return {"entries": rh.list_installed(REGISTRY_DB)}


@app.post("/api/registry/routine/reconcile")
def reconcile_routine() -> dict:
    """Make sqlite state match the YAML for claude_routine entries.

    Idempotent. AR-S3i intentionally ships without a real Routines API
    client -- every desired entry lands in ``deferred`` with a matching
    ``PendingRoutineOp`` describing what the operator (or the activated
    backend) would need to apply via the ``schedule`` skill. Once
    Q-PRESTON closes with a routine-shaped use case, activation is a
    one-line change: inject a concrete ``RoutinesClient`` here."""
    import routine_handler as rh
    return rh.reconcile(REGISTRY_YAML, REGISTRY_DB).as_dict()


# -- /goal renderer (AR-S3j) ----------------------------------------

@app.post("/api/registry/render_goal")
async def render_goal_endpoint(request: Request) -> dict:
    """Render the /goal text for a given entry + task. Returns the
    substituted string + the argv that would invoke `claude -p "/goal
    ..."`. Used by the loop_continuous runtime (AR-S3h) and for
    debugging by the operator.

    Body: {entry_name: str, task: {id, title, project_id, ...}}
    """
    import goal_renderer
    import schema as _schema
    body = await request.json()
    entry_name = body.get("entry_name")
    task = body.get("task") or {}
    if not entry_name:
        return {"error": "entry_name required"}
    autos = _schema.load_automations(REGISTRY_YAML)
    match = [a for a in autos if a.name == entry_name]
    if not match:
        return {"error": f"no such entry: {entry_name!r}"}
    try:
        rendered = goal_renderer.render_goal(match[0], task)
    except goal_renderer.GoalRenderError as exc:
        return {"error": str(exc)}
    return {
        "rendered_goal": rendered,
        "invocation_argv": goal_renderer.build_goal_invocation(rendered),
    }


def main() -> int:
    """Cross-platform entry point. PORT env wins, then --port, then default."""
    parser = argparse.ArgumentParser(description="Automation Registry skeleton")
    parser.add_argument("--port", type=int, default=None,
                        help=f"Port to bind (default: {DEFAULT_PORT})")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    env_port = os.environ.get("PORT")
    if env_port:
        try:
            port = int(env_port)
        except ValueError:
            log.warning("Invalid PORT env value %r -- falling back", env_port)
            port = args.port or DEFAULT_PORT
    else:
        port = args.port or DEFAULT_PORT

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("automation-registry v%s starting on %s:%d", APP_VERSION, args.host, port)
    log.info("registry YAML: %s (present=%s)", REGISTRY_YAML, REGISTRY_YAML.is_file())

    import uvicorn  # imported here so tests can `import app` without uvicorn
    uvicorn.run(app, host=args.host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
