"""USR-185 slice 1: model registry, validator invariants, subagent routing migration and id census."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.models import registry as reg

REPO_ROOT = Path(__file__).resolve().parents[1]


def _raw() -> dict:
    return json.loads(reg.DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))


def test_committed_registry_is_valid_and_resolves() -> None:
    parsed = reg.parse_registry(_raw())
    assert reg.resolve("haiku", parsed) == "claude-haiku-5-5"
    assert reg.resolve("subagent.sonnet", parsed) == "claude-sonnet-5-5"
    assert reg.resolve("opus", parsed) == "claude-opus-5-5"
    cand = [m.id for m in reg.list_candidates(parsed)]
    assert cand[:2] == ["claude-fable-5-1", "gpt-6-astra"]  # forbidden-autonomous candidates stay first
    assert {"deepseek/deepseek-v4-pro", "gemini-3.1-pro", "gemini-3.1-flash-lite-image", "gpt-image-2"} == set(cand[2:])


def test_resolve_unknown_and_forbidden() -> None:
    parsed = reg.parse_registry(_raw())
    with pytest.raises(KeyError):
        reg.resolve("nope", parsed)
    with pytest.raises(KeyError):
        reg.resolve("fable", parsed)


def test_forbidden_model_cannot_take_a_role() -> None:
    raw = copy.deepcopy(_raw())
    for model in raw["models"]:
        if model["id"] == "claude-fable-5-1":
            model["status"] = "active"
    raw["roles"]["subagent.opus"] = "claude-fable-5-1"
    with pytest.raises(ValidationError, match="forbidden"):
        reg.parse_registry(raw)


def test_same_family_reviewer_rejected() -> None:
    raw = copy.deepcopy(_raw())
    raw["review_pairs"] = [{"implementer": "development.claude", "reviewer": "subagent.opus"}]
    with pytest.raises(ValidationError, match="same family"):
        reg.parse_registry(raw)


def test_unqualified_model_cannot_be_routed() -> None:
    raw = copy.deepcopy(_raw())
    raw["roles"]["subagent.haiku"] = "claude-opus-5"  # deprecated
    with pytest.raises(ValidationError, match="qualification"):
        reg.parse_registry(raw)


def test_quota_floor_below_15_rejected() -> None:
    raw = copy.deepcopy(_raw())
    raw["quota_floor_percent"] = 14.9
    with pytest.raises(ValidationError, match="quota_floor"):
        reg.parse_registry(raw)


def test_missing_file_uses_embedded_fallback(tmp_path: Path) -> None:
    fallback = reg.load_registry(tmp_path / "absent.json")
    assert reg.resolve("subagent.haiku", fallback) == "claude-haiku-5-5"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert reg.resolve("sonnet", reg.load_registry(bad)) == "claude-sonnet-5-5"


def test_default_registry_is_cached() -> None:
    reg.clear_cache()
    assert reg.get_registry() is reg.get_registry()


def test_census_runs_and_reports_a_number() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "model_id_census.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
        check=True,
    )
    match = re.search(r"literal_model_ids_outside_allowlist=(\d+)", result.stdout)
    assert match is not None
    assert int(match.group(1)) >= 0  # informational only; enforcement is a later slice
