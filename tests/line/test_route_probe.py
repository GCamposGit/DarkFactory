from __future__ import annotations

import pytest

from core.line import route_probe


def test_route_probe_uses_normal_stage_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(route_probe.routing, "stage_mode", lambda stage: "write" if stage == "development" else "read")
    monkeypatch.setattr(
        route_probe.routing,
        "pick",
        lambda stage, host_caps, *, mode: calls.append((stage, tuple(host_caps), mode)) or ("codex", None),
    )

    assert route_probe.route_available("development") is True
    assert calls == [("development", route_probe.ROUTE_PROBE_HOST_CAPS, "write")]


def test_route_probe_is_red_when_no_route_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(route_probe.routing, "pick", lambda *_args, **_kwargs: None)
    assert route_probe.main(["review"]) == 1


def test_route_probe_rejects_unknown_stages() -> None:
    with pytest.raises(ValueError):
        route_probe.route_available("validation")
