"""USR-305: canonical G1 readiness (placeholder markers, non-goals, projection parity)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from core.demands.grill import DemandGrillEngine
from core.demands.models import UserTicket
from core.demands.readiness import (
    READINESS_DOCUMENTARY_GAP,
    READINESS_HUMAN_DECISION,
    READINESS_READY,
    evaluate_readiness,
    has_placeholder_marker,
)
from core.demands.store import DemandsStore
from core.workflow.line_live import _build_executable_queue

GOOD = "Validar todos os caminhos conhecidos sem alterar as regras."


@pytest.mark.parametrize(
    "text",
    [GOOD, "Cobrir todas as rotas do metodo de leitura", "Revisar todo o fluxo de entrega pelo hub"],
)
def test_words_containing_todo_are_not_placeholders(text: str) -> None:
    assert has_placeholder_marker(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "TODO definir o limite de tentativas do worker",
        "Limite de tentativas: TBD pelo owner",
        "Prazo de retencao a definir pelo owner",
        "Qual canal usar para o alerta???",
        "Implementar [preencher] antes de seguir",
        "todo: escrever o contrato do endpoint",
    ],
)
def test_explicit_placeholders_still_block(text: str) -> None:
    report = evaluate_readiness(text, ["x"], ["y"])
    assert report.is_clear is False
    assert report.classification == READINESS_HUMAN_DECISION


def test_ticket_with_todos_word_is_clear() -> None:
    t = UserTicket(id="USR-1", title="Validar caminhos", problem_statement=GOOD,
                   non_goals=["Nao mudar regras"], acceptance_criteria=["Teste verde"])
    assert DemandGrillEngine().evaluate_ambiguity(t)[0] is True


def test_missing_non_goals_is_documentary_gap_and_still_blocks() -> None:
    report = evaluate_readiness(GOOD, [], ["Teste verde"])
    assert report.is_clear is False
    assert report.classification == READINESS_DOCUMENTARY_GAP
    assert evaluate_readiness(GOOD, ["  "], ["Teste verde"]).is_clear is False
    assert evaluate_readiness(GOOD, ["a"], ["Teste verde passa"]).classification == READINESS_READY


def test_run_ticket_non_goal_flag_preserved(tmp_path: Path) -> None:
    import run_ticket

    args = run_ticket.build_parser().parse_args(
        ["--create", "--title", "Titulo", "--problem", GOOD, "--criteria", "ok",
         "--non-goal", "Nao mudar A", "--non-goal", "Nao mudar B"]
    )
    store = DemandsStore(tmp_path / "demands.json")
    ticket = run_ticket._build_ticket(args, store)
    assert ticket.non_goals == ["Nao mudar A", "Nao mudar B"]
    bare = run_ticket.build_parser().parse_args(["--create", "--title", "Titulo", "--problem", GOOD])
    assert run_ticket._build_ticket(bare, store).non_goals == []


def test_queue_projection_matches_canonical_readiness() -> None:
    rows = [
        {"id": "USR-1", "status": "planned", "problem_statement": GOOD,
         "non_goals": ["n"], "acceptance_criteria": ["Teste verde passa"]},
        {"id": "USR-2", "status": "planned", "problem_statement": GOOD,
         "acceptance_criteria": ["Teste verde passa"]},
        {"id": "USR-3", "status": "planned", "problem_statement": "TODO escrever depois isto aqui",
         "non_goals": ["n"], "acceptance_criteria": ["Teste verde passa"]},
    ]
    queue = _build_executable_queue(rows, [])
    engine = DemandGrillEngine()
    expected = sum(
        1 for r in rows
        if engine.evaluate_ambiguity(
            UserTicket(id=r["id"], title="titulo", problem_statement=r["problem_statement"],
                       non_goals=r.get("non_goals", []), acceptance_criteria=r["acceptance_criteria"])
        )[0]
    )
    assert queue.eligible_count == expected == 1
    reasons = {c.ticket_id: c.reason for c in queue.candidates}
    assert reasons["USR-2"] == "g1_documentary_gap"
    assert reasons["USR-3"] == "missing_specification"
