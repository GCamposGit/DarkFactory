from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from core.learning.observations import (
    consolidate_by_cause,
    load_observations,
    record_intervention_outcome,
    record_observation,
)
from core.learning.tracker import ContinuousLearningTracker
from core.line.retrospective import RetrospectiveLesson, record_lessons_in_learning_tracker
from core.learning.models import MistakeCategory


def test_interaction_is_sanitized_project_scoped_and_recovered_after_restart(tmp_path: Path) -> None:
    root = tmp_path / ".factory"
    tracker = ContinuousLearningTracker(ledger_file=root / "learning" / "learning_ledger.json", session_id="s-1")
    tracker.record_turn(
        "Resumo: corrigir autenticação com api_key=secret-value",
        "corrigir defeito",
        project_id="product-a",
        harness="codex",
        run_id="run-7",
        identity="interaction:request-7",
    )

    fresh_process_tracker = ContinuousLearningTracker(
        ledger_file=root / "learning" / "learning_ledger.json", session_id="s-2"
    )
    context = fresh_process_tracker.get_active_system1_context(project_id="product-a")
    assert "corrigir autenticação" in context
    assert "secret-value" not in context
    records = load_observations("product-a", root=root)
    assert records[0]["harness"] == "codex"
    assert records[0]["run_id"] == "run-7"
    assert records[0]["identity"] == "interaction:request-7"
    assert "secret-value" not in json.dumps(records)
    assert "secret-value" not in (root / "learning" / "learning_ledger.live.json").read_text(encoding="utf-8")
    assert load_observations("product-b", root=root) == []


def test_replay_is_idempotent_and_concurrent_processes_keep_all_observations(tmp_path: Path) -> None:
    root = tmp_path / "shared-state"
    root.mkdir()
    code = (
        "from pathlib import Path; from core.learning.observations import record_observation; "
        "import sys; record_observation(project_id='acme', identity=sys.argv[2], kind='interaction', "
        "summary='sanitized summary', root=Path(sys.argv[1]))"
    )
    commands = [
        [sys.executable, "-c", code, str(root), f"writer-{index}"]
        for index in range(12)
    ]
    processes = [subprocess.Popen(command) for command in commands]
    assert [process.wait(timeout=20) for process in processes] == [0] * len(processes)

    record_observation(
        project_id="acme", identity="writer-0", kind="interaction", summary="replay", root=root
    )
    records = load_observations("acme", root=root)
    assert len(records) == 12
    assert {item["identity"] for item in records} == {f"writer-{index}" for index in range(12)}


def test_other_harness_failure_is_linked_and_cause_consolidates_with_independent_measurement(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".factory"
    tracker = ContinuousLearningTracker(ledger_file=root / "learning" / "learning_ledger.json")
    lessons = [
        RetrospectiveLesson(
            rule="validar dependências antes da execução",
            category=MistakeCategory.TEST_REGRESSION,
            cause_code="Validate-Exhausted",
            stage="validation",
            run_id="run-1",
        ),
        RetrospectiveLesson(
            rule="validar dependências antes da execução",
            category=MistakeCategory.TEST_REGRESSION,
            cause_code="validate exhausted",
            stage="validation",
            run_id="run-2",
        ),
    ]
    record_lessons_in_learning_tracker(
        lessons, "run-1", tracker=tracker, project_id="product-a", ticket_id="USR-9",
        evidence=["evidence://validation/report"],
    )

    records = load_observations("product-a", root=root)
    assert all(item["ticket_id"] == "USR-9" for item in records)
    assert all(item["stage"] == "validation" for item in records)
    assert records[0]["evidence"] == ["evidence://validation/report"]
    candidates = consolidate_by_cause(records)
    assert len(candidates) == 1
    assert candidates[0]["occurrence_count"] == 2

    outcome = record_intervention_outcome(
        project_id="product-a",
        adjustment_id=candidates[0]["candidate_id"],
        independent_case_id="case-independent-1",
        interventions_before=3,
        interventions_after=1,
        root=root,
    )
    assert outcome["metrics"] == {
        "independent_case_id": "case-independent-1",
        "interventions_before": 3,
        "interventions_after": 1,
    }


def test_record_turn_cli_leaves_checkout_clean_and_turn_is_primed(tmp_path: Path, monkeypatch, capsys) -> None:
    """EC-03: live turns go to the untracked overlay; the tracked seed ledger stays byte-identical."""
    import subprocess

    from core.learning import cli, tracker as tracker_module

    repo_gitignore = Path(__file__).resolve().parents[1] / ".gitignore"
    seed = tmp_path / ".factory" / "learning" / "learning_ledger.json"
    seed.parent.mkdir(parents=True)
    seed.write_text("{}", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(repo_gitignore.read_text(encoding="utf-8"), encoding="utf-8")

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=tmp_path, check=True, capture_output=True, text=True,
        ).stdout

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")
    seed_bytes = seed.read_bytes()

    monkeypatch.setattr(tracker_module, "DEFAULT_LEDGER_FILE", seed)
    monkeypatch.setenv("DARKFAC_PROJECT_ROOT", str(tmp_path))
    for prompt in ("turno de coordenacao", "segundo turno"):
        monkeypatch.setattr(
            "sys.argv",
            ["learning", "record-turn", "--prompt", prompt, "--intent", "evitar sujeira", "--project", "darkfac"],
        )
        cli.main()
    capsys.readouterr()

    assert git("status", "--porcelain") == ""
    assert seed.read_bytes() == seed_bytes
    fresh = ContinuousLearningTracker(ledger_file=seed)
    assert [t.turn_id for t in fresh.ledger.turns] == [1, 2]
    assert "turno de coordenacao" in fresh.get_active_system1_context(project_id="darkfac")
