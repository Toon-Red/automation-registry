#!/usr/bin/env python3
"""SessionStart hook: probe automation-registry health.

Fires when a Claude Code session starts in this repo. Prints a single
one-line status to stdout so the operator knows immediately whether
the registry is reachable.

Cross-platform: stdlib only. Times out fast (~1.5s). Exits 0
unconditionally -- informational, not a gate.

Pattern copied from pipeline-dashboard/.claude/hooks/health_check.py
(PD sub-task 063d1219 / umbrella 3615dce5).
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

REGISTRY_URL = "http://127.0.0.1:5050"
HEALTH_PATH = "/api/health"
TIMEOUT = 1.5


def _fetch_json(path: str) -> dict | None:
    try:
        with urllib.request.urlopen(
            f"{REGISTRY_URL}{path}", timeout=TIMEOUT
        ) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError,
            ConnectionError, TimeoutError, OSError):
        return None


def main() -> int:
    health = _fetch_json(HEALTH_PATH)
    if health is None:
        print(
            "[automation-registry] DOWN -- "
            f"no response at {REGISTRY_URL}{HEALTH_PATH}. "
            "Start with: python app.py"
        )
        return 0

    version = health.get("version", "?")
    uptime = health.get("uptime_seconds", 0)
    yaml_present = "yaml=ok" if health.get("registry_yaml_present") else "yaml=MISSING"
    print(f"[automation-registry] UP -- v{version} | uptime={uptime}s | {yaml_present}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
