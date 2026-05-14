"""Cron mechanism handler (AR-S3b).

Bridges the catalog (automations.yaml) to the platform schedule
(``scripts.service_supervisor.install_cron`` from pipeline-dashboard).

Responsibilities:

  * Filter catalog entries to ``mechanism: cron``.
  * Build a :class:`CronJob` per entry; reconcile against sqlite to
    decide install / no-op / re-install / uninstall.
  * Call the supervisor's cron API for each delta.
  * Idempotent -- safe to call ``reconcile()`` repeatedly.

The supervisor module lives in the pipeline-dashboard repo. Its path
is configurable so tests can inject a mock and so the registry can
deploy without a fixed assumption about the workspace layout.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import schema as _schema  # local import; flat layout
import state as _state

log = logging.getLogger("automation-registry.cron")

DEFAULT_PD_PATH = (
    Path(__file__).resolve().parent.parent / "pipeline-dashboard"
)


def _load_supervisor(pd_path: Path | None = None):
    """Import scripts.service_supervisor from the pipeline-dashboard
    repo. Path is configurable (env ``PD_REPO_PATH``, then arg, then
    default sibling-of-this-repo)."""
    pd = pd_path or Path(
        os.environ.get("PD_REPO_PATH") or str(DEFAULT_PD_PATH)
    )
    pd = pd.resolve()
    if str(pd) not in sys.path:
        sys.path.insert(0, str(pd))
    # Import fresh so the module-search sees the new sys.path entry.
    from scripts import service_supervisor as svc  # type: ignore
    return svc


@dataclass
class ReconcileResult:
    installed: list[str]
    reinstalled: list[str]
    unchanged: list[str]
    uninstalled: list[str]
    skipped: list[str]  # disabled entries that weren't installed before

    def as_dict(self) -> dict[str, Any]:
        return {
            "installed": list(self.installed),
            "reinstalled": list(self.reinstalled),
            "unchanged": list(self.unchanged),
            "uninstalled": list(self.uninstalled),
            "skipped": list(self.skipped),
        }


def _build_cron_job(svc_mod, automation: _schema.Automation,
                     working_dir: str) -> Any:
    """Construct supervisor.CronJob from a validated Automation entry."""
    return svc_mod.CronJob(
        name=automation.name,
        schedule=automation.schedule or "",
        command=automation.target,
        working_dir=working_dir,
        description=automation.description,
    )


def reconcile(
    yaml_path: Path | str,
    db_path: Path | str,
    *,
    supervisor=None,
    pd_path: Path | None = None,
    working_dir_resolver: Callable[[_schema.Automation], str] | None = None,
) -> ReconcileResult:
    """Make platform state match the YAML catalog for cron entries.

    Steps:

    1. Ensure sqlite schema exists.
    2. Load + validate the YAML.
    3. For each cron entry:
         - If not in sqlite and enabled -> install + record.
         - If in sqlite with same fingerprint and enabled -> no-op.
         - If in sqlite with changed fingerprint -> reinstall + update.
         - If in sqlite and now disabled -> uninstall + delete row.
    4. For each sqlite row whose name is no longer in the YAML cron
       set -> uninstall + delete row.

    ``working_dir_resolver`` defaults to "the registry repo root" --
    callers can override (e.g., to resolve to the owner_project's
    repo path via PD lookup). The runner CLI (AR-S3c) is what
    actually consumes the command at fire time, so the working_dir
    here is the runner's cwd, not the target's.
    """
    _state.ensure_schema(db_path)
    svc_mod = supervisor or _load_supervisor(pd_path)
    automations = _schema.load_automations(yaml_path)
    cron_entries = [a for a in automations if a.mechanism == "cron"]

    desired_names = {a.name for a in cron_entries}
    existing_rows = {r.name: r for r in _state.list_entries(db_path)}

    if working_dir_resolver is None:
        registry_root = Path(__file__).resolve().parent
        def working_dir_resolver(_a):  # type: ignore
            return str(registry_root)

    result = ReconcileResult([], [], [], [], [])

    # -- Pass 1: process every desired entry ------------------------
    for auto in cron_entries:
        wd = working_dir_resolver(auto)
        if not auto.enabled:
            if auto.name in existing_rows:
                _uninstall_one(svc_mod, db_path, auto.name)
                result.uninstalled.append(auto.name)
            else:
                result.skipped.append(auto.name)
            continue

        job = _build_cron_job(svc_mod, auto, wd)
        new_fingerprint = (auto.schedule, auto.target, wd,
                            auto.description, True)
        existing = existing_rows.get(auto.name)
        if existing and existing.install_fingerprint == new_fingerprint:
            result.unchanged.append(auto.name)
            continue

        svc_mod.install_cron(job)
        _state.upsert_entry(
            db_path,
            name=auto.name,
            schedule=auto.schedule or "",
            command=auto.target,
            working_dir=wd,
            description=auto.description,
            enabled=True,
        )
        if existing:
            result.reinstalled.append(auto.name)
        else:
            result.installed.append(auto.name)

    # -- Pass 2: prune rows whose entries left the YAML -------------
    for name in list(existing_rows.keys()):
        if name not in desired_names:
            _uninstall_one(svc_mod, db_path, name)
            result.uninstalled.append(name)

    log.info("reconcile complete: %s", result.as_dict())
    return result


def _uninstall_one(svc_mod, db_path: Path | str, name: str) -> None:
    try:
        svc_mod.uninstall_cron(name)
    except Exception as exc:  # pragma: no cover -- backend error path
        log.warning("uninstall_cron(%s) raised %s -- removing row anyway",
                    name, exc)
    _state.delete_entry(db_path, name)


def list_installed(db_path: Path | str) -> list[dict[str, Any]]:
    """Return the current sqlite view of installed cron entries."""
    rows = _state.list_entries(db_path)
    return [
        {
            "name": r.name,
            "schedule": r.schedule,
            "command": r.command,
            "working_dir": r.working_dir,
            "description": r.description,
            "enabled": r.enabled,
            "last_install_ts": r.last_install_ts,
            "last_modified_ts": r.last_modified_ts,
            "status": r.status,
        }
        for r in rows
    ]
