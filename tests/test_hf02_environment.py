"""Deterministic guards for the HF-02-01 laboratory handoff."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "spikes" / "runtime_choice" / "requirements.lock.txt"
VERSIONS = ROOT / "spikes" / "runtime_choice" / "versions.json"
ENVIRONMENT_DOC = ROOT / "docs" / "handoffs" / "HF-02-ENVIRONMENT.md"


def _lock_packages() -> list[str]:
    return [
        line.strip()
        for line in LOCK.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def test_hf02_lock_is_exact_and_contains_required_lab_packages() -> None:
    packages = _lock_packages()

    assert len(packages) == len(set(packages))
    assert "dbos==2.31.1" in packages
    assert "psycopg==3.3.5" in packages
    assert "psycopg-binary==3.3.5" in packages
    assert "psutil==7.2.2" in packages


def test_hf02_manifest_is_waiting_access_without_serialized_secrets() -> None:
    manifest = json.loads(VERSIONS.read_text(encoding="utf-8"))

    assert manifest["ticket"] == "HF-02-01"
    assert manifest["status"] == "waiting_access"
    assert manifest["validation"]["pip_check"] == "pass"
    assert manifest["validation"]["clean_venv_reinstall"] == "pass"
    assert manifest["secret_policy"]["dsn_serialized"] is False
    assert manifest["secret_policy"]["secret_values_serialized"] is False
    assert manifest["runtime"]["database_url_env"] == "DARKFAC_HF02_DATABASE_URL"

    serialized = VERSIONS.read_text(encoding="utf-8").lower()
    assert "postgresql://" not in serialized
    assert "postgres://" not in serialized


def test_hf02_does_not_add_experiment_packages_to_default_requirements() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()

    assert "dbos==" not in requirements
    assert "psycopg==" not in requirements
    assert "spikes/runtime_choice/requirements.lock.txt" in requirements


def test_hf02_environment_handoff_describes_access_probe() -> None:
    document = ENVIRONMENT_DOC.read_text(encoding="utf-8")

    assert "Status: `waiting_access`" in document
    assert "DARKFAC_HF02_DATABASE_URL" in document
    assert "SELECT 1" in document
    assert "No firewall, DNS, TLS, Docker, PostgreSQL, or VPS mutation" in document
