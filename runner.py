"""Automation registry runner (AR-S3c).

This is the script the platform-scheduled task literally invokes when
its cron schedule fires. Self-contained CLI; not a server.

Invocation forms (both work; pick one in the schedule definition):

    python runner.py --entry <name>
    python -m runner --entry <name>

CLI:
    --entry <name>    Required. The registry entry name to fire.
    --dry-run         Log what would run; don't spawn or write state.
    --manual          Mark this run as operator-initiated (tagged in
                      sqlite ``cron_runs.source`` -- helps the EOD
                      report distinguish scheduled vs manual fires).
    --pd-path <path>  Override pipeline-dashboard repo path (env
                      PD_REPO_PATH wins over this).

Behaviour:

  1. Look up the entry in automations.yaml (validated via schema.py).
  2. Acquire an advisory file lock at ``data/locks/<entry>.lock``. If
     held by another runner instance, exit 0 silently with a log line
     (per Q4 hybrid recovery model: cron entries are idempotent by
     construction; the lock just protects the brief
     platform-fire-to-target-completion window).
  3. Write a ``cron_runs`` row with status=``running``, started_at=now.
  4. Dispatch to the target via subprocess (Q-B: subprocess for target
     repo isolation -- the target's deps don't pollute the registry's
     interpreter, and target crashes can't take down the runner).
  5. Capture exit_code + stdout/stderr excerpts.
  6. Update the row to ``succeeded`` or ``failed``.
  7. Release the lock + exit with the same code as the target.

  NOT this sub-task: firing the escalation pipeline on failure. AR-S3d
  reads the failed-run rows and acts. For now the runner just records
  cleanly.

target_kind dispatch (per schema v1):

  - ``python_callable``: subprocess of ``python -c "from <module>
    import <fn>; <fn>()"`` with ``cwd`` = owner_project's repo path
    (resolved via PD).
  - ``shell``: subprocess of the literal target string with cwd =
    owner_project's repo path.
  - ``http``: urllib POST to the URL (stdlib only; no requests dep).
  - ``mcp``: stub -- raises NotImplementedError with a pointer to a
    follow-on task (real MCP wiring is its own sub-task; not gated
    on AR-S3c shipping).
  - ``agent_role``: stub -- raises NotImplementedError pointing at
    AC-S10 (multi-engine driver).

Cross-platform notes:

  - Windows: ``subprocess.CREATE_NO_WINDOW`` is applied so a runner
    invoked from schtasks doesn't flash a console window.
  - Windows file locking uses ``msvcrt.locking``; POSIX uses ``fcntl``.
    Wrapped in a context manager that picks the right one at import.
  - All paths quoted via list-form subprocess args. No shell strings.
  - Same Python interpreter (``sys.executable``) is used to spawn
    python_callable targets so venv state propagates.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import schema as _schema
import state as _state


log = logging.getLogger("automation-registry.runner")

ROOT = Path(__file__).resolve().parent
REGISTRY_YAML = ROOT / "automations.yaml"
REGISTRY_DB = ROOT / "data" / "registry.db"
LOCK_DIR = ROOT / "data" / "locks"

EXCERPT_BYTES = 8192       # truncate stdout/stderr captures
SUBPROCESS_TIMEOUT = 3600  # 1h ceiling -- targets exceeding this are killed

# Default base for resolving owner_project -> repo path. Matches
# pipeline-dashboard/scripts/service_supervisor.py:_base_projects_dir.
DEFAULT_BASE_PROJECTS_DIR = Path.home() / "Desktop" / "code"


# ---------------------------------------------------------------------------
# Cross-platform advisory file lock
# ---------------------------------------------------------------------------

if sys.platform == "win32":  # pragma: no cover -- platform branch
    import msvcrt

    def _lock_file(handle) -> bool:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock_file(handle) -> None:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

    SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW
else:  # pragma: no cover -- platform branch
    import fcntl

    def _lock_file(handle) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock_file(handle) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

    SUBPROCESS_FLAGS = 0


@contextmanager
def _advisory_lock(lock_path: Path) -> Iterator[bool]:
    """Acquire a non-blocking advisory lock. Yields True if held, False
    if another process has it. Releases automatically on exit."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    try:
        ok = _lock_file(handle)
        yield ok
    finally:
        try:
            _unlock_file(handle)
        finally:
            handle.close()


# ---------------------------------------------------------------------------
# sqlite extension: source column + start/end helpers
# ---------------------------------------------------------------------------

def _ensure_runs_schema(db_path: Path) -> None:
    """Add the runtime-only columns the runner needs. Safe to call on
    a DB created by ``state.ensure_schema`` -- idempotent ALTER guards."""
    _state.ensure_schema(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(cron_runs)")}
        if "status" not in cols:
            conn.execute("ALTER TABLE cron_runs ADD COLUMN status TEXT "
                          "NOT NULL DEFAULT 'running'")
        if "source" not in cols:
            conn.execute("ALTER TABLE cron_runs ADD COLUMN source TEXT "
                          "NOT NULL DEFAULT 'schedule'")
        conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record_run_start(db_path: Path, entry_name: str, source: str) -> int:
    """Insert a `running` row; return its run_id."""
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO cron_runs
               (entry_name, started_at, status, source)
               VALUES (?, ?, 'running', ?)""",
            (entry_name, _now(), source),
        )
        run_id = cur.lastrowid
        conn.commit()
    return run_id


def _record_run_end(db_path: Path, run_id: int, *, status: str,
                     exit_code: int | None,
                     stdout_excerpt: str, stderr_excerpt: str) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """UPDATE cron_runs
               SET ended_at=?, status=?, exit_code=?,
                   stdout_excerpt=?, stderr_excerpt=?
               WHERE run_id=?""",
            (_now(), status, exit_code,
             stdout_excerpt[:EXCERPT_BYTES],
             stderr_excerpt[:EXCERPT_BYTES],
             run_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Owner-project repo resolution
# ---------------------------------------------------------------------------

@dataclass
class _ResolvedTarget:
    cwd: Path
    argv: list[str]
    description: str   # human-readable for logs


def _resolve_repo_path(owner_project: str, *,
                        base_dir: Path | None = None) -> Path:
    """Locate the owner_project's repo. Resolution order:

      1. PD API: GET /api/projects/<id> -> `local_repo_path`.
      2. Env override: REGISTRY_BASE_PROJECTS_DIR / <owner_project>.
      3. Default: ~/Desktop/code/<owner_project>.

    Falls back through silently; the final candidate is what we use.
    Caller surfaces FileNotFoundError if the chosen path is absent.
    """
    pd_path = _query_pd_repo_path(owner_project)
    if pd_path is not None:
        return pd_path

    base = base_dir or Path(
        os.environ.get("REGISTRY_BASE_PROJECTS_DIR")
        or str(DEFAULT_BASE_PROJECTS_DIR)
    )
    return Path(base) / owner_project


def _query_pd_repo_path(owner_project: str) -> Path | None:
    """Best-effort PD lookup. Returns None on any failure (including PD
    being down). Tests stub this entirely."""
    pd_url = os.environ.get("PD_URL", "http://127.0.0.1:5100")
    try:
        with urllib.request.urlopen(
            f"{pd_url}/api/projects/{owner_project}", timeout=2.0,
        ) as r:
            data = json.loads(r.read())
        local = data.get("local_repo_path")
        if not local:
            return None
        p = Path(local)
        if p.is_absolute():
            return p
        # PD stores relative; resolve against BASE_PROJECTS_DIR.
        base = Path(
            os.environ.get("REGISTRY_BASE_PROJECTS_DIR")
            or str(DEFAULT_BASE_PROJECTS_DIR)
        )
        return base / local
    except (urllib.error.URLError, urllib.error.HTTPError,
            ConnectionError, TimeoutError, OSError, ValueError):
        return None


def _build_target_invocation(entry: _schema.Automation,
                              *, repo_path_resolver: Callable[[str], Path]
                              ) -> _ResolvedTarget:
    kind = entry.target_kind
    if kind in ("python_callable", "shell"):
        cwd = repo_path_resolver(entry.owner_project)
        if not cwd.is_dir():
            raise FileNotFoundError(
                f"owner_project {entry.owner_project!r}: resolved repo "
                f"path {cwd} does not exist"
            )
        if kind == "python_callable":
            # Accept "module:function" or "module.function"; normalise
            # to (module, function).
            target = entry.target
            if ":" in target:
                module, function = target.rsplit(":", 1)
            else:
                module, _, function = target.rpartition(".")
                if not module:
                    raise ValueError(
                        f"python_callable target {target!r}: must be "
                        "'module:function' or 'module.function'"
                    )
            code = f"from {module} import {function}; {function}()"
            argv = [sys.executable, "-c", code]
            desc = f"python -c \"from {module} import {function}; {function}()\""
            return _ResolvedTarget(cwd=cwd, argv=argv, description=desc)
        # shell
        # Tokenise the target with shlex on POSIX, simple split on Windows.
        import shlex
        if sys.platform == "win32":
            argv = entry.target.split()
        else:
            argv = shlex.split(entry.target)
        return _ResolvedTarget(cwd=cwd, argv=argv, description=entry.target)
    if kind == "http":
        # Synthesise a marker argv -- _execute treats http specially.
        return _ResolvedTarget(cwd=ROOT,
                                argv=["__http__", entry.target],
                                description=f"HTTP POST {entry.target}")
    if kind == "mcp":
        raise NotImplementedError(
            f"target_kind=mcp not yet wired -- follow-on sub-task "
            "after AR-S3 ships"
        )
    if kind == "agent_role":
        raise NotImplementedError(
            f"target_kind=agent_role requires AC-S10 (multi-engine "
            "driver) to ship first"
        )
    raise ValueError(f"unsupported target_kind {kind!r}")


# ---------------------------------------------------------------------------
# Subprocess invocation
# ---------------------------------------------------------------------------

@dataclass
class _ExecResult:
    exit_code: int
    stdout: str
    stderr: str


def _execute(target: _ResolvedTarget,
              *, run=subprocess.run) -> _ExecResult:
    """Run the target via subprocess (or HTTP for http kind)."""
    if target.argv and target.argv[0] == "__http__":
        url = target.argv[1]
        try:
            req = urllib.request.Request(url, method="POST")
            with urllib.request.urlopen(req, timeout=SUBPROCESS_TIMEOUT) as r:
                body = r.read().decode("utf-8", errors="replace")
            return _ExecResult(exit_code=0, stdout=body, stderr="")
        except (urllib.error.HTTPError, urllib.error.URLError,
                ConnectionError, TimeoutError, OSError) as exc:
            return _ExecResult(exit_code=1, stdout="", stderr=str(exc))

    proc = run(
        target.argv,
        cwd=str(target.cwd),
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        creationflags=SUBPROCESS_FLAGS,
    )
    return _ExecResult(
        exit_code=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def run_entry(
    entry_name: str,
    *,
    yaml_path: Path = REGISTRY_YAML,
    db_path: Path = REGISTRY_DB,
    lock_dir: Path = LOCK_DIR,
    dry_run: bool = False,
    source: str = "schedule",
    execute: Callable[[_ResolvedTarget], _ExecResult] = _execute,
    repo_path_resolver: Callable[[str], Path] | None = None,
) -> int:
    """Programmatic entry point used by the CLI + tests. Returns the
    process exit code that the platform-scheduled task should see."""
    automations = _schema.load_automations(yaml_path)
    match = [a for a in automations if a.name == entry_name]
    if not match:
        log.error("no such entry: %r", entry_name)
        return 2
    entry = match[0]

    resolver = repo_path_resolver or _resolve_repo_path
    target = _build_target_invocation(entry, repo_path_resolver=resolver)
    log.info("entry=%s target=%s cwd=%s", entry.name, target.description,
              target.cwd)

    if dry_run:
        log.info("--dry-run: not executing, not writing state")
        return 0

    lock_path = lock_dir / f"{entry_name}.lock"
    with _advisory_lock(lock_path) as held:
        if not held:
            log.info("entry %s: another runner instance holds the lock; "
                      "exiting 0", entry_name)
            return 0

        _ensure_runs_schema(db_path)
        run_id = _record_run_start(db_path, entry_name, source)
        try:
            result = execute(target)
        except Exception as exc:
            log.exception("runner caught exception invoking target")
            _record_run_end(db_path, run_id, status="failed",
                             exit_code=None, stdout_excerpt="",
                             stderr_excerpt=f"runner exception: {exc}")
            return 1

        status = "succeeded" if result.exit_code == 0 else "failed"
        _record_run_end(db_path, run_id, status=status,
                         exit_code=result.exit_code,
                         stdout_excerpt=result.stdout,
                         stderr_excerpt=result.stderr)
        log.info("entry=%s status=%s exit=%d", entry_name, status,
                  result.exit_code)
        return result.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="automation-registry-runner",
        description="Fire a registry cron entry. Invoked by platform schedules.",
    )
    parser.add_argument("--entry", required=True,
                        help="Registry entry name to fire.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log + resolve; don't spawn or write state.")
    parser.add_argument("--manual", action="store_true",
                        help="Tag this run as operator-initiated.")
    parser.add_argument("--pd-path", default=None,
                        help="Pipeline-dashboard repo path override.")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if args.pd_path:
        os.environ.setdefault("PD_REPO_PATH", args.pd_path)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Re-resolve module-level constants so test monkey-patches take
    # effect when main() is invoked programmatically.
    import sys as _sys
    mod = _sys.modules[__name__]
    return run_entry(
        entry_name=args.entry,
        yaml_path=mod.REGISTRY_YAML,
        db_path=mod.REGISTRY_DB,
        lock_dir=mod.LOCK_DIR,
        dry_run=args.dry_run,
        source="manual" if args.manual else "schedule",
    )


if __name__ == "__main__":
    sys.exit(main())
