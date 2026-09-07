"""Unit and property tests for the Echo Garden domain."""

from itertools import product

import pytest
from pydantic import ValidationError

from core.game.engine import MAX_TURNS, apply_move, new_game, play_sequence, render_board
from core.game.models import GameMove, GameState, GameStatus


def test_seed_rotates_initial_channels_deterministically() -> None:
    assert new_game(0).energies == (2, 4, 6)
    assert new_game(1).energies == (6, 2, 4)
    assert new_game(4).energies == new_game(1).energies


def test_echo_is_previous_delta_rotated_right() -> None:
    first = apply_move(new_game(), GameMove.WEAVE)
    second = apply_move(first.after, GameMove.GROUND)
    assert first.after.energies == (3, 4, 5)
    assert second.echo_delta == (-1, 1, 0)
    assert second.after.energies == (2, 4, 6)


def test_known_three_turn_sequence_reaches_perfect_resonance() -> None:
    result = play_sequence(["weave", "ground", "weave"])
    assert result.final.status == GameStatus.WON
    assert result.final.energies == (4, 4, 4)
    assert result.final.turn == 3
    assert result.traces[-1].spread == 0


def test_every_transition_preserves_energy_bounds() -> None:
    for sequence in product(GameMove, repeat=MAX_TURNS):
        result = play_sequence(sequence, seed=2)
        assert all(0 <= level <= 8 for trace in result.traces for level in trace.after.energies)


def test_game_loses_at_turn_budget_when_never_resonant() -> None:
    result = play_sequence([GameMove.ECHO] * MAX_TURNS)
    assert result.final.status == GameStatus.LOST
    assert result.final.turn == MAX_TURNS


def test_terminal_game_rejects_additional_moves() -> None:
    won = play_sequence(["weave", "ground", "weave"]).final
    with pytest.raises(ValueError, match="finished"):
        apply_move(won, GameMove.ECHO)


def test_invalid_move_and_invalid_energy_are_rejected() -> None:
    with pytest.raises(ValueError):
        apply_move(new_game(), "teleport")
    with pytest.raises(ValidationError):
        GameState(energies=(9, 1, 1))


def test_ascii_board_is_stable_and_windows_safe() -> None:
    board = render_board(new_game())
    assert "Echo Garden" in board
    assert "EMBER [##......] 2" in board
    board.encode("ascii")
