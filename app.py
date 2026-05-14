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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

log = logging.getLogger("automation-registry")

DEFAULT_PORT = 5050
APP_VERSION = "0.1.0"

ROOT = Path(__file__).resolve().parent
REGISTRY_YAML = ROOT / "automations.yaml"
REGISTRY_DB = ROOT / "data" / "registry.db"

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
        "status": "cron handler online -- AR-S3b (runner ships in AR-S3c)",
    }


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
