"""Testes da deteccao de chamada de ferramenta sem resposta em sessao Codex (USR-79)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from core.line import codex_session_probe as probe


def _write(path: Path, lines: list[object]) -> Path:
    text = "\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def _call(call_id: str, kind: str = "custom_tool_call", name: str = "shell", **extra: object) -> dict:
    return {"type": "response_item", "payload": {"type": kind, "call_id": call_id, "name": name, **extra}}


def _output(call_id: str, kind: str = "custom_tool_call_output") -> dict:
    return {"type": "response_item", "payload": {"type": kind, "call_id": call_id, "output": "ok"}}


def test_interrupted_session_reports_orphan_call(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "s.jsonl",
        [
            _call("call_1", input="git status"),
            _output("call_1"),
            _call("call_2", kind="function_call", name="exec", arguments="pytest -q"),
        ],
    )
    orphans = probe.find_unanswered_tool_calls(path)
    assert [o.call_id for o in orphans] == ["call_2"]
    assert orphans[0].call_type == "function_call"
    assert orphans[0].line_number == 3
    result = probe.probe_session(path)
    assert result.interrupted
    assert result.last_confirmed == "git status"
    report = probe.format_report(result)
    assert "call_2" in report and "git status" in report and "NAO edite" in report


def test_complete_session_has_no_orphans(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "s.jsonl",
        [_call("a"), _output("a"), _call("b", kind="function_call"), _output("b", "function_call_output")],
    )
    assert probe.find_unanswered_tool_calls(path) == []
    assert probe.main([str(path)]) == 0


def test_corrupt_lines_are_tolerated(tmp_path: Path) -> None:
    path = _write(tmp_path / "s.jsonl", ["{not json", _call("x"), "[1, 2]", "", _output("x"), _call("y")])
    result = probe.probe_session(path)
    assert result.invalid_lines == 2  # JSON invalido e linha nao-objeto
    assert [o.call_id for o in result.unanswered] == ["y"]


def test_unknown_format_yields_no_orphans(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "s.jsonl",
        [{"hello": "world"}, {"payload": "str"}, {"payload": {"type": 5, "call_id": 7}}, {"type": "message"}],
    )
    assert probe.find_unanswered_tool_calls(path) == []


def test_output_before_call_and_top_level_records(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "s.jsonl",
        [_output("early"), _call("early"), {"type": "custom_tool_call", "call_id": "flat"}],
    )
    assert [o.call_id for o in probe.find_unanswered_tool_calls(path)] == ["flat"]


def test_missing_file_and_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert probe.main([str(tmp_path / "nope.jsonl")]) == 1
    assert probe.main([]) == 1
    orphan = _write(tmp_path / "o.jsonl", [_call("z")])
    assert probe.main([str(orphan)]) == 2
    assert "z" in capsys.readouterr().out


def test_warn_if_unanswered_logs_and_never_raises(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    orphan = _write(tmp_path / "o.jsonl", [_call("z")])
    before = orphan.read_bytes()
    with caplog.at_level(logging.WARNING):
        assert probe.warn_if_unanswered(orphan) is not None
        assert probe.warn_if_unanswered(tmp_path / "nope.jsonl") is None
    assert "call_id=z" in caplog.text
    assert orphan.read_bytes() == before
