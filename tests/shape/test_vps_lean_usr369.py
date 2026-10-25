"""USR-369: lean VPS. Memory budget, no build on the VPS, GHCR images, no n8n."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

import yaml

from core.orchestrator.compose_render import render_darkhub_compose
from scripts import dokploy_redeploy as dr

ROOT = Path(__file__).resolve().parents[2]
DOKPLOY = ROOT / "deploy" / "dokploy"
BUDGET_MIB = 3200  # 3.2 GB
THIRD_PARTY_PINNED = {"darkfac-pg-tailnet"}  # alpine/socat:<exact version>, not an app image


def _mib(value: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmMgG])[bB]?", str(value).strip())
    assert m, f"unparsable memory value {value!r}"
    n, unit = float(m.group(1)), m.group(2).lower()
    return n * {"k": 1 / 1024, "m": 1, "g": 1024}[unit]


def _limit(svc: Dict[str, Any]) -> str:
    return svc.get("mem_limit") or svc.get("deploy", {}).get("resources", {}).get("limits", {}).get("memory") or ""


def _services() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for path in sorted(DOKPLOY.glob("docker-compose*.yml")):
        text = path.read_text(encoding="utf-8")
        if path.name.endswith("hub.yml"):
            text = render_darkhub_compose(text, "a" * 40, "ghcr")
        for name, svc in yaml.safe_load(text)["services"].items():
            assert name not in out
            out[name] = svc
    return out


def test_every_service_has_an_explicit_memory_limit() -> None:
    for name, svc in _services().items():
        assert _limit(svc), f"{name} has no memory limit"


def test_sum_of_memory_limits_is_within_3_2_gb() -> None:
    total = sum(_mib(_limit(svc)) for svc in _services().values())
    assert total <= BUDGET_MIB, f"sum of limits {total} MiB exceeds {BUDGET_MIB} MiB"


def test_no_service_builds_on_the_vps() -> None:
    for name, svc in _services().items():
        assert "build" not in svc, f"{name} builds on the VPS"
        assert svc.get("pull_policy") != "build"


def test_app_services_use_ghcr_images_tagged_by_sha() -> None:
    for name, svc in _services().items():
        image = svc["image"]
        if name in THIRD_PARTY_PINNED:
            assert re.search(r":\d+\.\d+\.\d+", image)
            continue
        assert image.startswith("ghcr.io/gcamposgit/"), f"{name}: {image}"
        assert not image.endswith((":latest", ":main")), f"{name}: {image}"
        tag = image.split("darkhub:" if "darkhub:" in image else "darkfac-cloud:", 1)[1]
        assert tag.startswith("a" * 40) or tag.startswith("${DARKFAC_GIT_SHA"), f"{name}: {image}"


def test_n8n_is_gone_from_the_deployed_stack() -> None:
    assert not list(DOKPLOY.glob("*n8n*"))
    assert not [n for n in _services() if "n8n" in n]


def test_default_redeploy_skips_leftover_n8n_but_only_can_reach_it() -> None:
    svcs = [
        dr.Service(name=n, kind="compose", service_id=n, status="done")
        for n in ("darkfac-cloud", "Darkhub", "darkfac-n8n", "n8n")
    ]
    assert [s.name for s in dr.select_services(svcs, None)] == ["darkfac-cloud", "Darkhub"]
    assert [s.name for s in dr.select_services(svcs, ["darkfac-n8n"])] == ["darkfac-n8n"]


def test_redeploy_script_builds_nothing_in_parallel() -> None:
    assert dr.MAX_CONCURRENT_BUILDS == 1
    text = (ROOT / "scripts" / "dokploy_redeploy.py").read_text(encoding="utf-8")
    # check-main keeps one watchdog thread (not a build), so threading.Thread is not banned.
    for banned in ("ThreadPoolExecutor", "ProcessPoolExecutor", "asyncio.gather"):
        assert banned not in text


def test_frota_manual_documents_restore_and_ghcr_cloud_image() -> None:
    text = (ROOT / "docs" / "manual" / "frota.md").read_text(encoding="utf-8")
    assert "Imagem do cloud no GHCR" in text
    assert "n8n" in text
