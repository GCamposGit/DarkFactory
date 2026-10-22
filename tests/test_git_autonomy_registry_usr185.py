"""USR-185 slice 12: ``deliver_branch(kind="registry")`` allowlist, no ticket completion, chore(models) title."""

from __future__ import annotations

from pathlib import Path

from core.demands.store import DemandsStore
from core.git.autonomy import GitAutonomyManager, registry_allowlist, registry_disallowed
from core.roadmap.models import DeliveryStatus
from tests.test_git_delivery import FakeGh, _git, _seed_ticket, env  # noqa: F401 - env is a fixture

REG = ".factory/config/model_registry.json"


def _write(local: Path, rel: str, text: str = "{}\n") -> None:
    target = local / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_allowlist_contains_registry_ledger_and_docgen_targets() -> None:
    allowed = registry_allowlist()
    assert REG in allowed and ".factory/models/lifecycle_ledger.jsonl" in allowed
    assert "docs/MODEL_SELECTION_GUIDE.md" in allowed
    assert registry_disallowed([REG, "core/x.py", ".factory/demands/demands.json"]) == [
        ".factory/demands/demands.json", "core/x.py",
    ]


def test_registry_delivery_merges_without_completing_ticket(env) -> None:  # noqa: F811
    local, _, gh = env
    _seed_ticket(local, "USR-185")
    _git(local, "add", "-A")
    _git(local, "commit", "--quiet", "-m", "chore: seed")
    _git(local, "push", "--quiet", "origin", "main")
    _write(local, REG)
    rep = GitAutonomyManager(local).deliver_branch("USR-185", "chore(models): promover x", cwd=local, gh_runner=gh,
                                                   kind="registry")
    assert rep.ok and rep.action == "merged", rep.message
    create = next(c for c in gh.calls if c[:2] == ["pr", "create"])
    assert create[create.index("--title") + 1] == "chore(models): promover x"
    assert any(c[:2] == ["pr", "edit"] and "--add-label" in c and "registry" in c for c in gh.calls)
    assert any(c[:2] == ["pr", "checks"] for c in gh.calls)  # same CI gate as implementation
    assert _git(local, "log", "-1", "--format=%s") != ""
    ticket = DemandsStore(local / ".factory" / "demands" / "demands.json").get_ticket("USR-185")
    assert ticket.status != DeliveryStatus.COMPLETED and not ticket.delivery_evidence


def test_registry_delivery_title_gets_prefix_and_no_ticket_suffix(env) -> None:  # noqa: F811
    local, _, gh = env
    _write(local, REG)
    rep = GitAutonomyManager(local).deliver_branch("USR-185", "rollback y", cwd=local, gh_runner=gh, kind="registry")
    assert rep.ok, rep.message
    create = next(c for c in gh.calls if c[:2] == ["pr", "create"])
    assert create[create.index("--title") + 1] == "chore(models): rollback y"
    assert "[USR-185]" not in " ".join(create)


def test_registry_delivery_refuses_file_outside_allowlist(env) -> None:  # noqa: F811
    local, _, gh = env
    _write(local, REG)
    _write(local, "core/evil.py", "x = 1\n")
    rep = GitAutonomyManager(local).deliver_branch("USR-185", "x", cwd=local, gh_runner=gh, kind="registry")
    assert not rep.ok and rep.action == "error" and "core/evil.py" in rep.message
    assert not any(c[:2] == ["pr", "create"] for c in gh.calls)
