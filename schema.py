"""Schema validation for automations.yaml entries (AR-S3b).

Implements the v1 schema documented at
``docs/proposals/automation-registry-schema-v1.md``. Parses the
top-level YAML into a list of validated :class:`Automation` dataclasses
or raises a :class:`SchemaError` with a clear path-prefixed message.

This module is intentionally tolerant of forward-references (e.g.
``target_kind: agent_role`` is allowed even before Agent Controller's
role registry exists -- AR-S1 schema doc Q5). It only enforces the
strict rules listed in the doc under "Validator rules".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SUPPORTED_MECHANISMS = frozenset({
    "cron", "claude_hook", "claude_loop", "manual",
    "executor", "claude_check", "claude_blocker_callback",
    # Schema v2 tier reshape (2026-05-14).
    "claude_desktop_scheduled",
    "claude_loop_continuous",
    "claude_routine",
})

SUPPORTED_TARGET_KINDS = frozenset({
    "python_callable", "shell", "http", "mcp", "agent_role",
    "claude_prompt",  # schema v2 -- routine + loop_continuous
})

SUPPORTED_CHANNELS = frozenset({
    "discord", "log_only", "toast", "none",
})

SUPPORTED_ON_FAILURE = frozenset({
    "file_pd_task", "discord_only", "log_only",
})


class SchemaError(ValueError):
    """Raised when an automations.yaml entry fails validation. The
    message is path-prefixed so callers can locate the bad field."""


@dataclass
class Escalation:
    channel: str
    on_failure: str
    pd_project: str | None = None
    webhook_id: str | None = None
    retry: dict | None = None


@dataclass
class Automation:
    name: str
    description: str
    owner_project: str
    target: str
    target_kind: str
    mechanism: str
    escalation: Escalation
    enabled: bool
    schedule: str | None = None
    unblock_condition: dict | None = None
    executor_ref: dict | None = None
    pre_dispatch_hooks: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    # Schema v2 additions.
    trigger: dict | None = None          # state-aware triggers (Desktop scheduled)
    engines: dict | None = None          # per-layer engine map (loop_continuous)
    goal_template: str | None = None     # /goal text (loop_continuous + AR-S3j)
    limit_aware: dict | None = None      # pause_at_remaining_pct etc.
    # AR-S3e: catalog-only entries (registry observes but does not manage).
    # When true the cron handler short-circuits install / reinstall /
    # uninstall for this entry -- it stays visible in listings and the
    # runs table can still record observed fires, but lifecycle management
    # belongs to whoever owns the external schedule.
    external: bool = False
    # Original dict preserved for handlers that want fields not yet
    # promoted to dataclass attributes.
    raw: dict = field(default_factory=dict)


def _routine_schedule_meets_minimum(schedule: str) -> bool:
    """Return False if the cron-style ``schedule`` clearly fires more
    often than once per hour. AR-S3i's deferred-stub validator -- per
    the v2 schema doc, claude_routine has a 60-minute minimum cadence.

    The check is conservative: it rejects only the cases we can prove
    are sub-hourly from the minute + hour fields alone (e.g.
    ``*/5 * * * *`` or ``*/30 9-17 * * 1-5``). Anything we cannot
    statically prove sub-hourly passes -- a real cloud-side reject
    will surface at install time when the backend ships. This keeps
    the validator path useful today without blocking legitimate
    schedules that simply look unusual.
    """
    if not isinstance(schedule, str):
        return True
    parts = schedule.strip().split()
    if len(parts) < 2:
        # Macro forms like '@hourly' / '@daily' are fine; '@every 5s' is
        # not cron-standard and falls through to the runtime check.
        return True
    minute_field, hour_field = parts[0], parts[1]
    # Wildcard minute with wildcard hour means "every minute".
    if minute_field == "*":
        return False
    # Step expressions in the minute field with a wildcard or range
    # hour. ``*/N`` for N < 60 fires more than once per hour.
    if minute_field.startswith("*/"):
        try:
            step = int(minute_field[2:])
        except ValueError:
            return True
        if step < 60:
            return False
    # Comma list in the minute field with two or more distinct values.
    if "," in minute_field:
        values = [v for v in minute_field.split(",") if v.strip()]
        if len(values) >= 2:
            return False
    # Range in the minute field implies at least two firings/hour.
    if "-" in minute_field and not minute_field.startswith("-"):
        return False
    return True


def _require(d: dict, key: str, path: str) -> Any:
    if key not in d:
        raise SchemaError(f"{path}: missing required field {key!r}")
    return d[key]


def _validate_escalation(raw: dict, path: str) -> Escalation:
    if not isinstance(raw, dict):
        raise SchemaError(f"{path}: escalation must be an object")
    channel = _require(raw, "channel", path)
    on_failure = _require(raw, "on_failure", path)
    if channel not in SUPPORTED_CHANNELS:
        raise SchemaError(
            f"{path}.channel: {channel!r} not in {sorted(SUPPORTED_CHANNELS)}"
        )
    if on_failure not in SUPPORTED_ON_FAILURE:
        raise SchemaError(
            f"{path}.on_failure: {on_failure!r} not in {sorted(SUPPORTED_ON_FAILURE)}"
        )
    pd_project = raw.get("pd_project")
    if on_failure == "file_pd_task" and not pd_project:
        # Doc says: defaults to owner_project if absent. Defer that
        # resolution to the caller -- they have access to it.
        pd_project = None
    return Escalation(
        channel=channel,
        on_failure=on_failure,
        pd_project=pd_project,
        webhook_id=raw.get("webhook_id"),
        retry=raw.get("retry"),
    )


def validate_entry(raw: dict, index: int) -> Automation:
    """Validate one entry from the top-level ``automations:`` list."""
    if not isinstance(raw, dict):
        raise SchemaError(
            f"automations[{index}]: entry must be an object, got "
            f"{type(raw).__name__}"
        )
    path = f"automations[{index}]"

    name = _require(raw, "name", path)
    if not isinstance(name, str) or not name:
        raise SchemaError(f"{path}.name: must be a non-empty string")
    path = f"automations[{index}]({name})"

    description = _require(raw, "description", path)
    owner_project = _require(raw, "owner_project", path)
    target = _require(raw, "target", path)
    target_kind = _require(raw, "target_kind", path)
    mechanism = _require(raw, "mechanism", path)
    enabled = _require(raw, "enabled", path)

    if target_kind not in SUPPORTED_TARGET_KINDS:
        raise SchemaError(
            f"{path}.target_kind: {target_kind!r} not in "
            f"{sorted(SUPPORTED_TARGET_KINDS)}"
        )
    if mechanism not in SUPPORTED_MECHANISMS:
        raise SchemaError(
            f"{path}.mechanism: {mechanism!r} not in "
            f"{sorted(SUPPORTED_MECHANISMS)}"
        )
    if not isinstance(enabled, bool):
        raise SchemaError(f"{path}.enabled: must be a boolean")

    schedule = raw.get("schedule")
    trigger = raw.get("trigger")
    if mechanism == "cron" and not schedule:
        raise SchemaError(
            f"{path}: mechanism=cron requires a non-null schedule"
        )
    if mechanism == "claude_hook" and not schedule:
        raise SchemaError(
            f"{path}: mechanism=claude_hook requires schedule "
            "(the hook event name)"
        )
    if mechanism == "claude_desktop_scheduled":
        # Two flavours: time-of-day (schedule non-null) or state-aware
        # (trigger.kind == 'state'). Exactly one.
        has_sched = bool(schedule)
        has_state = isinstance(trigger, dict) and trigger.get("kind") == "state"
        if has_sched and has_state:
            raise SchemaError(
                f"{path}: claude_desktop_scheduled cannot have BOTH a "
                "non-null schedule AND a state-aware trigger; pick one"
            )
        if not (has_sched or has_state):
            raise SchemaError(
                f"{path}: claude_desktop_scheduled requires either a "
                "non-null schedule (time-of-day) OR a trigger with "
                "kind: state (state-aware)"
            )
        if has_state:
            required_trigger_fields = ("fire_when", "after_event",
                                        "state_file", "on_fire_update")
            for f in required_trigger_fields:
                if not trigger.get(f):
                    raise SchemaError(
                        f"{path}.trigger: state-aware requires "
                        f"non-empty {f!r}"
                    )

    engines = raw.get("engines")
    if mechanism == "claude_loop_continuous":
        if schedule is not None:
            raise SchemaError(
                f"{path}: claude_loop_continuous must have "
                "schedule: null (not cron-scheduled)"
            )
        if not isinstance(engines, dict):
            raise SchemaError(
                f"{path}: claude_loop_continuous requires engines map "
                "(L4..L8 -> engine id)"
            )

    if mechanism == "claude_routine":
        # Tier 2 cloud-side routine. Per the v2 schema doc:
        #   - non-null schedule required (cloud routines are cron-driven).
        #   - 60-minute minimum cadence: the Anthropic Routines surface
        #     refuses sub-hourly schedules. Reject at parse time so
        #     operators don't ship a routine that the cloud will
        #     silently reject downstream.
        #   - target_kind must be claude_prompt -- the routine body is a
        #     prompt string forwarded verbatim by routine_handler. Any
        #     other target_kind would silently misroute the entry's
        #     ``target`` field (e.g. a shell command rendered as a
        #     prompt), so reject at parse time.
        if not schedule:
            raise SchemaError(
                f"{path}: mechanism=claude_routine requires a non-null "
                "schedule (cloud routines are cron-driven; 1h minimum)"
            )
        if not _routine_schedule_meets_minimum(schedule):
            raise SchemaError(
                f"{path}.schedule: claude_routine requires >= 60-minute "
                f"cadence (got {schedule!r}); cloud Routines reject "
                "sub-hourly schedules"
            )
        if target_kind != "claude_prompt":
            raise SchemaError(
                f"{path}.target_kind: mechanism=claude_routine requires "
                f"target_kind='claude_prompt' (got {target_kind!r}); the "
                "routine body is a prompt string, not a python callable / "
                "shell argv"
            )

    # Rule #5 of the v2 schema doc: target_kind=claude_prompt is only
    # valid when the mechanism is one of the prompt-consuming Tier 1/2
    # surfaces. Anything else would silently render the prompt as a
    # python callable / shell argv, which is a deeply misleading bug.
    if target_kind == "claude_prompt" and mechanism not in (
        "claude_routine", "claude_loop_continuous",
    ):
        raise SchemaError(
            f"{path}.target_kind: 'claude_prompt' only valid for "
            "mechanism in {claude_routine, claude_loop_continuous}; "
            f"got mechanism={mechanism!r}"
        )

    unblock_condition = raw.get("unblock_condition")
    if mechanism == "claude_blocker_callback":
        if not isinstance(unblock_condition, dict):
            raise SchemaError(
                f"{path}: mechanism=claude_blocker_callback requires "
                "unblock_condition object"
            )
        kind = unblock_condition.get("kind")
        if kind not in ("time", "event", "external"):
            raise SchemaError(
                f"{path}.unblock_condition.kind: must be "
                "time | event | external"
            )

    executor_ref = raw.get("executor_ref")
    if mechanism == "executor":
        if not isinstance(executor_ref, dict):
            raise SchemaError(
                f"{path}: mechanism=executor requires executor_ref object"
            )
        if not executor_ref.get("app"):
            raise SchemaError(
                f"{path}.executor_ref.app: required for mechanism=executor"
            )

    escalation = _validate_escalation(_require(raw, "escalation", path),
                                       f"{path}.escalation")
    if escalation.on_failure == "file_pd_task" and not escalation.pd_project:
        # Resolve default per the schema doc.
        escalation = Escalation(
            channel=escalation.channel,
            on_failure=escalation.on_failure,
            pd_project=owner_project,
            webhook_id=escalation.webhook_id,
            retry=escalation.retry,
        )

    pre_hooks = raw.get("pre_dispatch_hooks", [])
    if not isinstance(pre_hooks, list):
        raise SchemaError(f"{path}.pre_dispatch_hooks: must be a list")
    tags = raw.get("tags", [])
    if not isinstance(tags, list):
        raise SchemaError(f"{path}.tags: must be a list")

    external = raw.get("external", False)
    if not isinstance(external, bool):
        raise SchemaError(f"{path}.external: must be a boolean")

    return Automation(
        name=name,
        description=description,
        owner_project=owner_project,
        target=target,
        target_kind=target_kind,
        mechanism=mechanism,
        schedule=schedule,
        unblock_condition=unblock_condition,
        executor_ref=executor_ref,
        pre_dispatch_hooks=list(pre_hooks),
        tags=list(tags),
        trigger=trigger if isinstance(trigger, dict) else None,
        engines=engines if isinstance(engines, dict) else None,
        goal_template=raw.get("goal_template"),
        limit_aware=raw.get("limit_aware"),
        external=external,
        escalation=escalation,
        enabled=bool(enabled),
        raw=raw,
    )


def load_automations(yaml_path: Path | str) -> list[Automation]:
    """Parse and validate the full automations.yaml file.

    Returns a list of :class:`Automation` (possibly empty). Raises
    :class:`SchemaError` on any validation failure. Empty file or
    ``automations: []`` returns an empty list without error.
    """
    import yaml
    text = Path(yaml_path).read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise SchemaError(
            f"{yaml_path}: top-level YAML must be a mapping, got "
            f"{type(data).__name__}"
        )
    sv = data.get("schema_version")
    if sv is not None and sv != 1:
        raise SchemaError(
            f"{yaml_path}: schema_version {sv} not supported (this build "
            "speaks v1)"
        )
    raw_entries = data.get("automations") or []
    if not isinstance(raw_entries, list):
        raise SchemaError(
            f"{yaml_path}: 'automations' must be a list, got "
            f"{type(raw_entries).__name__}"
        )
    seen_names: set[str] = set()
    out: list[Automation] = []
    for i, raw in enumerate(raw_entries):
        entry = validate_entry(raw, i)
        if entry.name in seen_names:
            raise SchemaError(
                f"automations[{i}]({entry.name}): duplicate name; each "
                "entry must have a unique 'name'"
            )
        seen_names.add(entry.name)
        out.append(entry)
    return out
