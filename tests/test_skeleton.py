"""Skeleton tests for automation-registry (AR-S2).

Verifies:
  * The app module imports without side effects.
  * /api/health returns 200 with the expected schema.
  * / returns the landing pointer with status hint.
  * automations.yaml is present and parseable (empty list at AR-S2).
"""
from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

import app as registry_app


client = TestClient(registry_app.app)


def test_app_imports() -> None:
    assert registry_app.APP_VERSION == "0.1.0"
    assert registry_app.DEFAULT_PORT == 5050


def test_health_endpoint() -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == registry_app.APP_VERSION
    assert isinstance(body["uptime_seconds"], int)
    assert body["registry_yaml_present"] is True
    assert body["registry_yaml_path"].endswith("automations.yaml")


def test_root_endpoint() -> None:
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "automation-registry"
    assert "skeleton" in body["status"].lower()


def test_automations_yaml_parses_and_is_empty_at_skeleton() -> None:
    yaml_path = Path(registry_app.__file__).resolve().parent / "automations.yaml"
    assert yaml_path.is_file()
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["automations"] == []
