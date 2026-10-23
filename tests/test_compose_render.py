"""Shared DarkHub compose render + GitHub fetch at an exact SHA (USR-144).

No network: the ``gh`` runner is a fake.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, List, Sequence

import pytest

from core.orchestrator import compose_render as cr
from core.orchestrator.compose_render import (
    ComposeRenderError,
    ComposeSourceError,
    fetch_compose_at_sha,
    render_darkhub_compose,
    repository_from_url,
)

SHA = "a" * 40
REPO_ROOT = Path(__file__).resolve().parent.parent


def _runner(stdout: str = "", returncode: int = 0, calls: List[Any] | None = None) -> Any:
    def run(argv: Sequence[str], timeout: float) -> "subprocess.CompletedProcess[str]":
        if calls is not None:
            calls.append((list(argv), timeout))
        return subprocess.CompletedProcess(list(argv), returncode, stdout, "")

    return run


@pytest.fixture(autouse=True)
def _no_ambient_registry_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cr, "_windows_user_env", lambda _name: "")


def test_real_hub_compose_renders_and_pins_every_placeholder() -> None:
    source = (REPO_ROOT / cr.HUB_COMPOSE_PATH).read_text(encoding="utf-8")
    rendered = render_darkhub_compose(source, SHA, "build")
    assert "${DARKFAC_GIT_SHA}" not in rendered
    assert rendered.count(SHA) == 3


def test_redeploy_script_uses_the_shared_render() -> None:
    import scripts.dokploy_redeploy as script

    source = (REPO_ROOT / cr.HUB_COMPOSE_PATH).read_text(encoding="utf-8")
    assert script.render_darkhub_compose(source, SHA) == render_darkhub_compose(source, SHA)
    with pytest.raises(script.DokployUsageError, match="full origin/main SHA"):
        script.render_darkhub_compose(source, "short")
    with pytest.raises(script.DokployUsageError, match="pinned Git build"):
        script.render_darkhub_compose("services: {}\n", SHA)


def test_render_rejects_short_sha_and_missing_contract() -> None:
    with pytest.raises(ComposeRenderError, match="full origin/main SHA"):
        render_darkhub_compose("x", "abc")
    with pytest.raises(ComposeRenderError, match="pinned Git build"):
        render_darkhub_compose("services: {}\n", SHA)


def test_fetch_reads_the_file_at_the_exact_sha_through_gh_api() -> None:
    calls: List[Any] = []
    text = fetch_compose_at_sha("Org/Repo", SHA, runner=_runner("services: {}\n", calls=calls))
    assert text == "services: {}\n"
    argv, timeout = calls[0]
    assert argv[:2] == ["gh", "api"]
    assert argv[-1] == f"repos/Org/Repo/contents/{cr.HUB_COMPOSE_PATH}?ref={SHA}"
    assert "Accept: application/vnd.github.raw" in argv
    assert timeout > 0


@pytest.mark.parametrize(
    "runner",
    [
        _runner("", returncode=1),  # 404 / unauthenticated
        _runner("", returncode=0),  # empty body
        _runner("  \n", returncode=0),
    ],
)
def test_fetch_fails_closed_on_gh_error_or_empty_file(runner: Any) -> None:
    with pytest.raises(ComposeSourceError):
        fetch_compose_at_sha("Org/Repo", SHA, runner=runner)


def test_fetch_fails_closed_when_gh_is_missing_or_times_out() -> None:
    def missing(argv: Sequence[str], timeout: float) -> Any:
        raise FileNotFoundError("gh")

    def slow(argv: Sequence[str], timeout: float) -> Any:
        raise subprocess.TimeoutExpired(list(argv), timeout)

    for runner in (missing, slow):
        with pytest.raises(ComposeSourceError):
            fetch_compose_at_sha("Org/Repo", SHA, runner=runner)


@pytest.mark.parametrize(
    "repository,sha,path",
    [
        ("not-a-repo", SHA, cr.HUB_COMPOSE_PATH),
        ("Org/Repo", "main", cr.HUB_COMPOSE_PATH),
        ("Org/Repo", SHA[:-1], cr.HUB_COMPOSE_PATH),
        ("Org/Repo", SHA, "../secrets.env"),
        ("Org/Repo", SHA, "/etc/passwd"),
        ("Org/Repo", SHA, "a?ref=main"),
    ],
)
def test_fetch_rejects_malformed_arguments_without_running_gh(repository: str, sha: str, path: str) -> None:
    calls: List[Any] = []
    with pytest.raises(ComposeSourceError):
        fetch_compose_at_sha(repository, sha, path, runner=_runner("x", calls=calls))
    assert calls == []


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/GCamposGit/DarkFactory.git", "GCamposGit/DarkFactory"),
        ("https://github.com/GCamposGit/DarkFactory", "GCamposGit/DarkFactory"),
        ("https://tok@github.com/Org/Repo.git", "Org/Repo"),
        ("git@github.com:Org/Repo.git", "Org/Repo"),
        ("https://gitlab.com/Org/Repo.git", None),
        ("", None),
        (None, None),
    ],
)
def test_repository_from_url(url: Any, expected: Any) -> None:
    assert repository_from_url(url) == expected


def test_ghcr_is_default_and_build_requires_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cr.IMAGE_SOURCE_ENV, raising=False)
    source = (REPO_ROOT / cr.HUB_COMPOSE_PATH).read_text(encoding="utf-8")
    assert cr.resolve_hub_image_source({}) == "ghcr"
    assert cr.resolve_hub_image_source() == "ghcr"
    assert cr.resolve_hub_image_source({"DARKHUB_IMAGE_SOURCE": "build"}) == "build"
    default = render_darkhub_compose(source, SHA)
    assert default == render_darkhub_compose(source, SHA, "ghcr")
    assert "build:" not in default and "pull_policy: build" not in default
    legacy = render_darkhub_compose(source, SHA, "build")
    assert "pull_policy: build" in legacy
    assert "container_name: darkhub-cloud-247" in legacy


def test_registry_token_falls_back_to_windows_user_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GHCR_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    monkeypatch.setattr(cr.sys, "platform", "win32")
    monkeypatch.setattr(cr, "_windows_user_env", lambda name: "reg-token" if name == "GITHUB_PAT" else "")
    assert cr._ghcr_registry_token() == "reg-token"
    monkeypatch.setenv("GHCR_TOKEN", "proc-token")
    assert cr._ghcr_registry_token() == "proc-token"


def test_registry_token_no_windows_lookup_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GHCR_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    monkeypatch.setattr(cr.sys, "platform", "linux")

    def boom(name: str) -> str:
        raise AssertionError("registry must not be read off Windows")

    monkeypatch.setattr(cr, "_windows_user_env", boom)
    assert cr._ghcr_registry_token() == ""


def test_ghcr_mode_emits_image_start_first_without_container_name() -> None:
    source = (REPO_ROOT / cr.HUB_COMPOSE_PATH).read_text(encoding="utf-8")
    rendered = render_darkhub_compose(source, SHA, "ghcr")
    assert f"image: ghcr.io/gcamposgit/darkhub:{SHA}" in rendered
    assert "pull_policy: always" in rendered
    assert "order: start-first" in rendered
    assert "container_name:" not in rendered
    assert "build:" not in rendered and "no_cache" not in rendered
    assert "hostname: darkhub-cloud-247" in rendered
    assert "${DARKFAC_GIT_SHA}" not in rendered
    assert rendered.count(SHA) == 2


def test_image_source_env_fails_closed_on_unknown_value() -> None:
    assert cr.resolve_hub_image_source({"DARKHUB_IMAGE_SOURCE": "GHCR"}) == "ghcr"
    with pytest.raises(ComposeRenderError):
        cr.resolve_hub_image_source({"DARKHUB_IMAGE_SOURCE": "registry"})


def test_wait_for_ghcr_image_polls_until_tag_exists() -> None:
    statuses = iter([404, 404, 200])
    clock = {"t": 0.0}
    cr.wait_for_ghcr_image(
        SHA, probe=lambda repo, tag: next(statuses), interval=5, timeout=60,
        sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s), clock_fn=lambda: clock["t"],
    )
    assert clock["t"] == 10.0


def test_wait_for_ghcr_image_times_out_without_fallback() -> None:
    clock = {"t": 0.0}
    with pytest.raises(cr.GhcrImageUnavailableError, match="no fallback to build"):
        cr.wait_for_ghcr_image(
            SHA, probe=lambda repo, tag: 404, interval=5, timeout=12,
            sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s), clock_fn=lambda: clock["t"],
        )


@pytest.mark.parametrize("status", [401, 403])
def test_wait_for_ghcr_image_rejects_unauthorized_probe_without_token(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.delenv("GHCR_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    calls: list[int] = []

    with pytest.raises(cr.GhcrImageUnavailableError, match="GHCR_TOKEN.*read:packages"):
        cr.wait_for_ghcr_image(
            SHA, probe=lambda _repo, _tag: calls.append(status) or status,
            timeout=900, interval=15, sleep_fn=lambda _seconds: pytest.fail("must refuse immediately"),
        )
    assert calls == [status]


def test_wait_for_ghcr_image_polls_auth_errors_when_token_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GHCR_TOKEN", "test-token")
    statuses = iter([401, 403, 404])
    clock = {"t": 0.0}

    with pytest.raises(cr.GhcrImageUnavailableError, match="last: HTTP 404") as exc_info:
        cr.wait_for_ghcr_image(
            SHA, probe=lambda _repo, _tag: next(statuses), interval=5, timeout=10,
            sleep_fn=lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
            clock_fn=lambda: clock["t"],
        )
    assert "test-token" not in str(exc_info.value)


def test_cli_ghcr_wait_is_thin_wrapper_over_shared_waiter(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import dokploy_redeploy

    calls: list[tuple[str, object]] = []

    def shared(sha: str, **kwargs: object) -> None:
        calls.append((sha, kwargs))
        raise cr.GhcrImageUnavailableError("GHCR image missing")

    monkeypatch.setattr(dokploy_redeploy, "_shared_wait_for_ghcr_image", shared)
    with pytest.raises(dokploy_redeploy.DokployUsageError, match="GHCR image missing"):
        dokploy_redeploy.wait_for_ghcr_tag(
            SHA, probe=lambda _repo, _tag: 404, timeout=123, interval=7,
        )
    assert calls[0][0] == SHA
    kwargs = calls[0][1]
    assert kwargs["timeout"] == 123 and kwargs["interval"] == 7
    assert kwargs["probe"] is not None


def test_cli_ghcr_wait_preserves_900_second_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import dokploy_redeploy

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        dokploy_redeploy,
        "_shared_wait_for_ghcr_image",
        lambda sha, **kwargs: calls.append(kwargs),
    )

    probe = lambda _repo, _tag: 200
    sleeper = lambda _seconds: None
    clock = lambda: 0.0
    dokploy_redeploy.wait_for_ghcr_tag(SHA, probe=probe, sleep_fn=sleeper, clock_fn=clock)

    assert calls[0]["timeout"] == 900.0
    assert calls[0]["probe"] is probe
    assert calls[0]["sleep_fn"] is sleeper
    assert calls[0]["clock_fn"] is clock


def test_adapter_refuses_deploy_when_ghcr_image_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.orchestrator.deployment_adapter import DokployDeploymentAdapter

    source = (REPO_ROOT / cr.HUB_COMPOSE_PATH).read_text(encoding="utf-8")
    monkeypatch.setenv("DARKHUB_IMAGE_SOURCE", "ghcr")

    def _missing(sha: str) -> None:
        raise cr.GhcrImageUnavailableError("missing")

    adapter = DokployDeploymentAdapter(compose_fetcher=lambda r, v, p: source, ghcr_waiter=_missing)
    rendered, err = adapter._render_compose_at_sha({"compose_repo": "o/r"}, SHA)
    assert rendered is None and err == "release_identity_ghcr_image_unpublished"

    adapter = DokployDeploymentAdapter(compose_fetcher=lambda r, v, p: source, ghcr_waiter=lambda s: None)
    rendered, err = adapter._render_compose_at_sha({"compose_repo": "o/r"}, SHA)
    assert err is None and f"darkhub:{SHA}" in rendered
