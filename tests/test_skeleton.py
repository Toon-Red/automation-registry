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
    # The status string evolves per shipped sub-task; require only that
    # one of the known phase tokens appears.
    status = body["status"].lower()
    assert any(tok in status for tok in ("skeleton", "ar-s3"))


def test_automations_yaml_parses_against_v1_schema() -> None:
    """The YAML must be valid v1 and contain only entries the validator
    accepts. Originally enforced ``automations == []`` at the skeleton
    phase; relaxed in AR-S3e once the dream-auto catalog-only entry
    landed."""
    yaml_path = Path(registry_app.__file__).resolve().parent / "automations.yaml"
    assert yaml_path.is_file()
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert isinstance(data["automations"], list)
    # Every entry passes the validator (round-trips via load_automations).
    import schema as _schema
    out = _schema.load_automations(yaml_path)
    assert len(out) == len(data["automations"])
