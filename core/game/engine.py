"""Pure deterministic rules for Echo Garden.

# [RESEARCH PROVENANCE & INSIGHTS]
# Ledger ID: 20260905_dark_factory_game_mechanics
# Audit Doc: .factory/research/20260905_dark_factory_game_mechanics/INSIGHTS.md
# Canonical Sources: .factory/research/20260905_dark_factory_game_mechanics/ledger.json
"""

from typing import Dict, Iterable

from core.game.models import (
    EnergyTriple,
    GameMove,
    GameState,
    GameStatus,
    SequenceResult,
    TurnTrace,
)


MAX_TURNS = 6
MIN_WIN_TURN = 3
MAX_ENERGY = 8
BASE_ENERGIES: EnergyTriple = (2, 4, 6)
MOVE_DELTAS: Dict[GameMove, EnergyTriple] = {
    GameMove.WEAVE: (1, 0, -1),
    GameMove.ECHO: (-1, 1, 0),
    GameMove.GROUND: (0, -1, 1),
}


def _rotate_right(values: EnergyTriple, count: int = 1) -> EnergyTriple:
    shift = count % 3
    if shift == 0:
        return values
    return values[-shift:] + values[:-shift]


def _clamp_energy(value: int) -> int:
    return max(0, min(MAX_ENERGY, value))


def new_game(seed: int = 0) -> GameState:
    """Create a game whose initial channel orientation is derived from ``seed``."""

    return GameState(seed=seed, energies=_rotate_right(BASE_ENERGIES, seed))


def score_state(state: GameState) -> int:
    """Score resonance while penalizing extra turns and wide channel spread."""

    spread = max(state.energies) - min(state.energies)
    win_bonus = 50 if state.status == GameStatus.WON else 0
    return max(0, 100 + win_bonus - spread * 20 - state.turn * 3)


def apply_move(state: GameState, move: GameMove | str) -> TurnTrace:
    """Apply one move and the rotated echo of the previous move."""

    if state.status != GameStatus.RUNNING:
        raise ValueError("cannot move after the game has finished")

    selected = move if isinstance(move, GameMove) else GameMove(move)
    direct_delta = MOVE_DELTAS[selected]
    echo_delta: EnergyTriple = (0, 0, 0)
    if state.pending_echo is not None:
        echo_delta = _rotate_right(MOVE_DELTAS[state.pending_echo])

    next_energies: EnergyTriple = tuple(
        _clamp_energy(level + direct_delta[index] + echo_delta[index])
        for index, level in enumerate(state.energies)
    )  # type: ignore[assignment]
    next_turn = state.turn + 1
    spread = max(next_energies) - min(next_energies)
    if next_turn >= MIN_WIN_TURN and spread <= 1:
        status = GameStatus.WON
    elif next_turn >= MAX_TURNS:
        status = GameStatus.LOST
    else:
        status = GameStatus.RUNNING

    next_state = GameState(
        seed=state.seed,
        turn=next_turn,
        energies=next_energies,
        pending_echo=selected,
        history=state.history + (selected,),
        status=status,
    )
    return TurnTrace(
        before=state,
        move=selected,
        direct_delta=direct_delta,
        echo_delta=echo_delta,
        after=next_state,
        spread=spread,
        score=score_state(next_state),
    )


def play_sequence(moves: Iterable[GameMove | str], seed: int = 0) -> SequenceResult:
    """Replay moves until the sequence ends or the game reaches a terminal state."""

    initial = new_game(seed)
    current = initial
    traces = []
    for move in moves:
        trace = apply_move(current, move)
        traces.append(trace)
        current = trace.after
        if current.status != GameStatus.RUNNING:
            break
    return SequenceResult(initial=initial, traces=traces, final=current)


def render_board(state: GameState) -> str:
    """Render an ASCII-safe board suitable for Windows terminals."""

    labels = ("EMBER", "TIDE", "MOSS")
    rows = [f"Echo Garden | turn {state.turn}/{MAX_TURNS} | {state.status.value.upper()}"]
    for label, energy in zip(labels, state.energies):
        rows.append(f"{label:<5} [{'#' * energy}{'.' * (MAX_ENERGY - energy)}] {energy}")
    echo = state.pending_echo.value if state.pending_echo else "none"
    rows.append(f"next echo: {echo} | score: {score_state(state)}")
    return "\n".join(rows)

