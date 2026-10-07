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


def test_real_hub_compose_renders_and_pins_every_placeholder() -> None:
    source = (REPO_ROOT / cr.HUB_COMPOSE_PATH).read_text(encoding="utf-8")
    rendered = render_darkhub_compose(source, SHA)
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
