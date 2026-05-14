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
})

SUPPORTED_TARGET_KINDS = frozenset({
    "python_callable", "shell", "http", "mcp", "agent_role",
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
    # Original dict preserved for handlers that want fields not yet
    # promoted to dataclass attributes.
    raw: dict = field(default_factory=dict)


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
    if mechanism == "cron" and not schedule:
        raise SchemaError(
            f"{path}: mechanism=cron requires a non-null schedule"
        )
    if mechanism == "claude_hook" and not schedule:
        raise SchemaError(
            f"{path}: mechanism=claude_hook requires schedule "
            "(the hook event name)"
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
