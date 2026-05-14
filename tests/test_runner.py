"""AR-S3c tests for runner.py.

Fully mocked subprocess + repo-resolver -- no real python -c spawns,
no real PD queries.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import state as _state
import runner


_GOOD_YAML = """\
schema_version: 1
automations:
  - name: dream-eod
    description: EOD review.
    owner_project: dream
    target: orchestrator:run_eod
    target_kind: python_callable
    mechanism: cron
    schedule: "0 17 * * *"
    escalation:
      channel: discord
      on_failure: file_pd_task
    enabled: true
"""


@pytest.fixture
def yaml_path(tmp_path):
    p = tmp_path / "automations.yaml"
    p.write_text(_GOOD_YAML, encoding="utf-8")
    return p


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "registry.db"
    runner._ensure_runs_schema(p)
    return p


@pytest.fixture
def lock_dir(tmp_path):
    d = tmp_path / "locks"
    d.mkdir()
    return d


@pytest.fixture
def repo_dir(tmp_path):
    d = tmp_path / "dream"
    d.mkdir()
    return d


def _resolver_to(repo_dir):
    return lambda owner: repo_dir


# ---------------------------------------------------------------------------
# Happy + failure path
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_success_writes_succeeded_row(self, yaml_path, db_path,
                                            lock_dir, repo_dir):
        def fake_exec(target):
            return runner._ExecResult(exit_code=0, stdout="hello", stderr="")

        rc = runner.run_entry(
            "dream-eod",
            yaml_path=yaml_path, db_path=db_path, lock_dir=lock_dir,
            execute=fake_exec, repo_path_resolver=_resolver_to(repo_dir),
        )
        assert rc == 0
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT status, exit_code, stdout_excerpt, source "
                "FROM cron_runs"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "succeeded"
        assert rows[0][1] == 0
        assert rows[0][2] == "hello"
        assert rows[0][3] == "schedule"

    def test_failure_writes_failed_row(self, yaml_path, db_path,
                                        lock_dir, repo_dir):
        def fake_exec(target):
            return runner._ExecResult(exit_code=1, stdout="",
                                       stderr="boom")

        rc = runner.run_entry(
            "dream-eod",
            yaml_path=yaml_path, db_path=db_path, lock_dir=lock_dir,
            execute=fake_exec, repo_path_resolver=_resolver_to(repo_dir),
        )
        assert rc == 1
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT status, exit_code, stderr_excerpt FROM cron_runs"
            ).fetchone()
        assert row[0] == "failed"
        assert row[1] == 1
        assert "boom" in row[2]

    def test_manual_flag_tags_source(self, yaml_path, db_path,
                                       lock_dir, repo_dir):
        def fake_exec(target):
            return runner._ExecResult(exit_code=0, stdout="", stderr="")

        runner.run_entry(
            "dream-eod",
            yaml_path=yaml_path, db_path=db_path, lock_dir=lock_dir,
            execute=fake_exec, source="manual",
            repo_path_resolver=_resolver_to(repo_dir),
        )
        with sqlite3.connect(str(db_path)) as conn:
            source = conn.execute("SELECT source FROM cron_runs").fetchone()[0]
        assert source == "manual"


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_no_state_written(self, yaml_path, db_path,
                                         lock_dir, repo_dir):
        called = {"n": 0}

        def fake_exec(target):
            called["n"] += 1
            return runner._ExecResult(exit_code=0, stdout="", stderr="")

        rc = runner.run_entry(
            "dream-eod", dry_run=True,
            yaml_path=yaml_path, db_path=db_path, lock_dir=lock_dir,
            execute=fake_exec, repo_path_resolver=_resolver_to(repo_dir),
        )
        assert rc == 0
        assert called["n"] == 0
        with sqlite3.connect(str(db_path)) as conn:
            n = conn.execute("SELECT count(*) FROM cron_runs").fetchone()[0]
        assert n == 0


# ---------------------------------------------------------------------------
# Errors: missing entry, missing repo
# ---------------------------------------------------------------------------

class TestErrorPaths:
    def test_missing_entry_returns_2(self, yaml_path, db_path,
                                       lock_dir, repo_dir):
        rc = runner.run_entry(
            "no-such-entry",
            yaml_path=yaml_path, db_path=db_path, lock_dir=lock_dir,
            repo_path_resolver=_resolver_to(repo_dir),
        )
        assert rc == 2

    def test_missing_repo_path_raises(self, yaml_path, db_path,
                                        lock_dir, tmp_path):
        bogus = tmp_path / "does-not-exist"
        with pytest.raises(FileNotFoundError, match="resolved repo path"):
            runner.run_entry(
                "dream-eod",
                yaml_path=yaml_path, db_path=db_path, lock_dir=lock_dir,
                repo_path_resolver=lambda owner: bogus,
            )


# ---------------------------------------------------------------------------
# Advisory lock
# ---------------------------------------------------------------------------

class TestLock:
    def test_held_lock_makes_second_runner_noop(self, yaml_path, db_path,
                                                  lock_dir, repo_dir):
        # Patch _lock_file so the first attempt holds, second attempt fails.
        attempts = {"n": 0}

        def fake_lock(handle):
            attempts["n"] += 1
            return attempts["n"] == 1  # only the first succeeds

        executed = {"n": 0}

        def fake_exec(target):
            executed["n"] += 1
            return runner._ExecResult(exit_code=0, stdout="", stderr="")

        with patch.object(runner, "_lock_file", fake_lock):
            rc1 = runner.run_entry(
                "dream-eod",
                yaml_path=yaml_path, db_path=db_path, lock_dir=lock_dir,
                execute=fake_exec,
                repo_path_resolver=_resolver_to(repo_dir),
            )
            rc2 = runner.run_entry(
                "dream-eod",
                yaml_path=yaml_path, db_path=db_path, lock_dir=lock_dir,
                execute=fake_exec,
                repo_path_resolver=_resolver_to(repo_dir),
            )
        assert rc1 == 0 and rc2 == 0
        assert executed["n"] == 1  # second run skipped due to lock


# ---------------------------------------------------------------------------
# Target invocation construction
# ---------------------------------------------------------------------------

class TestTargetInvocation:
    def test_python_callable_module_colon_fn(self, repo_dir):
        entry = SimpleNamespace(
            name="x", owner_project="dream",
            target="orchestrator:run_eod",
            target_kind="python_callable",
        )
        t = runner._build_target_invocation(
            entry, repo_path_resolver=_resolver_to(repo_dir),
        )
        assert t.cwd == repo_dir
        # argv[0] is python, argv[1] is "-c", argv[2] is the import+call
        assert t.argv[1] == "-c"
        assert "from orchestrator import run_eod" in t.argv[2]
        assert "run_eod()" in t.argv[2]

    def test_python_callable_dotted(self, repo_dir):
        entry = SimpleNamespace(
            name="x", owner_project="dream",
            target="dream.orchestrator.run_eod",
            target_kind="python_callable",
        )
        t = runner._build_target_invocation(
            entry, repo_path_resolver=_resolver_to(repo_dir),
        )
        assert "from dream.orchestrator import run_eod" in t.argv[2]

    def test_shell_target(self, repo_dir):
        entry = SimpleNamespace(
            name="x", owner_project="dream",
            target="echo hello",
            target_kind="shell",
        )
        t = runner._build_target_invocation(
            entry, repo_path_resolver=_resolver_to(repo_dir),
        )
        assert t.argv == ["echo", "hello"]
        assert t.cwd == repo_dir

    def test_mcp_target_kind_not_implemented(self, repo_dir):
        entry = SimpleNamespace(
            name="x", owner_project="dream",
            target="server:tool", target_kind="mcp",
        )
        with pytest.raises(NotImplementedError, match="target_kind=mcp"):
            runner._build_target_invocation(
                entry, repo_path_resolver=_resolver_to(repo_dir),
            )

    def test_agent_role_target_kind_not_implemented(self, repo_dir):
        entry = SimpleNamespace(
            name="x", owner_project="ac",
            target="coder", target_kind="agent_role",
        )
        with pytest.raises(NotImplementedError, match="AC-S10"):
            runner._build_target_invocation(
                entry, repo_path_resolver=_resolver_to(repo_dir),
            )


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

class TestCLI:
    def test_main_requires_entry(self):
        with pytest.raises(SystemExit):
            runner.main(["--dry-run"])

    def test_main_dispatches_dry_run(self, yaml_path, db_path,
                                       lock_dir, repo_dir, monkeypatch):
        monkeypatch.setattr(runner, "REGISTRY_YAML", yaml_path)
        monkeypatch.setattr(runner, "REGISTRY_DB", db_path)
        monkeypatch.setattr(runner, "LOCK_DIR", lock_dir)
        monkeypatch.setattr(runner, "_resolve_repo_path",
                             lambda owner, **kw: repo_dir)
        rc = runner.main(["--entry", "dream-eod", "--dry-run"])
        assert rc == 0
