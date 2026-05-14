"""AR-S3j: tests for goal_renderer.

Fully mocked subprocess -- no real `claude` invocation.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import goal_renderer
import schema as _schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _entry(template="Process task {task.id} ({task.title})", **kw):
    fields = dict(
        name="dream-work-cycle",
        description="L8-L4 cycle",
        owner_project="dream",
        target="dream.orchestrator:run_work_cycle",
        target_kind="python_callable",
        mechanism="claude_loop_continuous",
        schedule=None,
        unblock_condition=None,
        executor_ref=None,
        pre_dispatch_hooks=[],
        tags=[],
        trigger=None,
        engines={"L4": "ollama-local", "L5": "claude-haiku",
                  "L6": "claude-sonnet", "L7": "claude-opus",
                  "L8": "claude-opus"},
        goal_template=template,
        limit_aware=None,
        escalation=_schema.Escalation(
            channel="discord", on_failure="file_pd_task",
            pd_project="dream",
        ),
        enabled=True,
        raw={},
    )
    fields.update(kw)
    return _schema.Automation(**fields)


def _task(**kw):
    base = {
        "id": "abc12345",
        "title": "Fix login bug",
        "project_id": "auth-service",
        "priority": "high",
        "category": "bug",
        "complexity": "M",
        "description": "Repro steps...",
        "status": "todo",
    }
    base.update(kw)
    return base


NOW = datetime(2026, 5, 14, 9, 30)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class TestRenderGoal:
    def test_happy_path(self):
        out = goal_renderer.render_goal(_entry(), _task(), now=NOW)
        assert out == "Process task abc12345 (Fix login bug)"

    def test_all_task_placeholders(self):
        template = (
            "task.id={task.id} title={task.title} project={task.project} "
            "priority={task.priority} category={task.category} "
            "complexity={task.complexity} status={task.status}"
        )
        out = goal_renderer.render_goal(
            _entry(template=template), _task(), now=NOW,
        )
        assert "task.id=abc12345" in out
        assert "project=auth-service" in out  # alias resolves to project_id
        assert "priority=high" in out

    def test_entry_and_now_placeholders(self):
        template = "{entry.name} on {now.date} hour={now.hour}"
        out = goal_renderer.render_goal(
            _entry(template=template), _task(), now=NOW,
        )
        assert out == "dream-work-cycle on 2026-05-14 hour=9"

    def test_unknown_placeholder_rejected(self):
        with pytest.raises(goal_renderer.GoalRenderError,
                            match="unknown placeholder"):
            goal_renderer.render_goal(
                _entry(template="{task.secret}"), _task(), now=NOW,
            )

    def test_missing_field_renders_empty(self):
        # task.complexity omitted from task dict
        task = {k: v for k, v in _task().items() if k != "complexity"}
        out = goal_renderer.render_goal(
            _entry(template="[{task.complexity}]"), task, now=NOW,
        )
        assert out == "[]"

    def test_no_template_rejected(self):
        with pytest.raises(goal_renderer.GoalRenderError,
                            match="goal_template"):
            goal_renderer.render_goal(_entry(template=""), _task())

    def test_deterministic_same_inputs(self):
        # Same entry + same task + same now -> same output.
        a = goal_renderer.render_goal(_entry(), _task(), now=NOW)
        b = goal_renderer.render_goal(_entry(), _task(), now=NOW)
        assert a == b

    def test_introspection_attempt_rejected(self):
        # __class__ matches the regex pattern (underscores allowed),
        # but the whitelist rejects it. Confirms no Python-attribute
        # escape.
        with pytest.raises(goal_renderer.GoalRenderError,
                            match="unknown placeholder"):
            goal_renderer.render_goal(
                _entry(template="{task.__class__}"), _task(), now=NOW,
            )


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------

class TestInvocation:
    def test_build_invocation_argv(self):
        argv = goal_renderer.build_goal_invocation("Do the thing")
        assert argv == ["claude", "-p", "/goal Do the thing"]

    def test_build_invocation_custom_bin(self):
        argv = goal_renderer.build_goal_invocation(
            "X", claude_bin="/custom/claude",
        )
        assert argv[0] == "/custom/claude"

    def test_build_invocation_empty_rejected(self):
        with pytest.raises(goal_renderer.GoalRenderError):
            goal_renderer.build_goal_invocation("")

    def test_set_goal_invokes_runner(self):
        called = {}

        def fake_runner(argv, **kwargs):
            called["argv"] = argv
            called["kwargs"] = kwargs
            return subprocess.CompletedProcess(args=argv, returncode=0,
                                                 stdout="", stderr="")

        proc = goal_renderer.set_goal("Hello", runner=fake_runner)
        assert proc.returncode == 0
        assert called["argv"] == ["claude", "-p", "/goal Hello"]
        assert called["kwargs"]["capture_output"] is True
        assert called["kwargs"]["timeout"] == 30


# ---------------------------------------------------------------------------
# Combined render + set
# ---------------------------------------------------------------------------

class TestRenderAndSet:
    def test_happy_path(self):
        recorded = []

        def fake_runner(argv, **kwargs):
            recorded.append(argv)
            return subprocess.CompletedProcess(args=argv, returncode=0,
                                                 stdout="", stderr="")

        rendered, proc = goal_renderer.render_and_set(
            _entry(), _task(), now=NOW, runner=fake_runner,
        )
        assert rendered == "Process task abc12345 (Fix login bug)"
        assert proc.returncode == 0
        assert recorded[0] == ["claude", "-p",
                                 "/goal Process task abc12345 (Fix login bug)"]


# ---------------------------------------------------------------------------
# API endpoint
# ---------------------------------------------------------------------------

_YAML_FIXTURE = """\
schema_version: 1
automations:
  - name: dream-work-cycle
    description: L8-L4 cycle
    owner_project: dream
    target: dream.orchestrator:run_work_cycle
    target_kind: python_callable
    mechanism: claude_loop_continuous
    schedule: null
    engines:
      L4: ollama-local
      L5: claude-haiku
      L6: claude-sonnet
      L7: claude-opus
      L8: claude-opus
    goal_template: "Process task {task.id}"
    escalation:
      channel: discord
      on_failure: file_pd_task
    enabled: true
"""


class TestApiEndpoint:
    def test_render_goal_endpoint(self, tmp_path, monkeypatch):
        import app as registry_app
        yaml_path = tmp_path / "automations.yaml"
        yaml_path.write_text(_YAML_FIXTURE, encoding="utf-8")
        monkeypatch.setattr(registry_app, "REGISTRY_YAML", yaml_path)
        client = TestClient(registry_app.app)
        r = client.post("/api/registry/render_goal", json={
            "entry_name": "dream-work-cycle",
            "task": {"id": "xyz", "title": "test"},
        })
        assert r.status_code == 200
        body = r.json()
        assert body["rendered_goal"] == "Process task xyz"
        assert body["invocation_argv"] == [
            "claude", "-p", "/goal Process task xyz",
        ]

    def test_render_goal_endpoint_unknown_entry(self, tmp_path, monkeypatch):
        import app as registry_app
        yaml_path = tmp_path / "automations.yaml"
        yaml_path.write_text(_YAML_FIXTURE, encoding="utf-8")
        monkeypatch.setattr(registry_app, "REGISTRY_YAML", yaml_path)
        client = TestClient(registry_app.app)
        r = client.post("/api/registry/render_goal", json={
            "entry_name": "nope",
            "task": {},
        })
        assert "error" in r.json()
