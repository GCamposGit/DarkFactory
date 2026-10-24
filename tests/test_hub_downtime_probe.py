from __future__ import annotations

import json
import re
from pathlib import Path

from core.infra import downtime_probe as probe
from scripts import hub_downtime_probe as cli


def test_all_200_samples_exit_zero_and_record_jsonl(tmp_path: Path) -> None:
    clock = _Clock()
    output = tmp_path / "probe.jsonl"
    result = probe.run_probe(
        "https://hub.example/health", 3, output,
        prober=lambda _url, _timeout: (200, "a" * 40),
        clock=clock.time, sleep=clock.sleep,
    )

    assert result == {"total": 3, "non200": 0, "max_failure_streak": 0, "distinct_shas": ["a" * 40]}
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 3
    assert set(rows[0]) == {"ts", "status", "sha", "latency_ms", "error"}
    assert rows[0]["status"] == 200
    assert rows[0]["sha"] == "a" * 40
    assert clock.sleeps == [1.0, 1.0]


def test_non_200_and_prober_exception_are_counted_as_failures(tmp_path: Path) -> None:
    clock = _Clock()
    statuses = iter([(200, "a"), (502, "a"), RuntimeError("connection reset"), (200, "a")])

    def prober(_url: str, _timeout: float):
        value = next(statuses)
        if isinstance(value, Exception):
            raise value
        return value

    result = probe.run_probe("https://hub.example/health", 4, tmp_path / "x.jsonl", prober=prober,
                             clock=clock.time, sleep=clock.sleep)
    rows = [json.loads(line) for line in (tmp_path / "x.jsonl").read_text(encoding="utf-8").splitlines()]
    assert result["non200"] == 2
    assert result["max_failure_streak"] == 2
    assert rows[2]["status"] == 0
    assert rows[2]["error"] == "connection reset"


def test_sha_switch_is_summarized_and_expect_switch_controls_cli_exit(tmp_path: Path, capsys) -> None:
    clock = _Clock()
    shas = iter(["a" * 40, "a" * 40, "b" * 40])
    result = probe.run_probe("https://hub.example/health", 3, tmp_path / "switch.jsonl",
                             prober=lambda _url, _timeout: (200, next(shas)),
                             clock=clock.time, sleep=clock.sleep)
    assert result["distinct_shas"] == ["a" * 40, "b" * 40]

    cli_clock = _Clock()
    assert cli.main(["--url", "https://hub.example/health", "--seconds", "2", "--out", str(tmp_path / "ok"),
                     "--expect-switch"], prober=lambda _url, _timeout: (200, "a" if cli_clock.time() < 1 else "b"),
                    clock=cli_clock.time, sleep=cli_clock.sleep) == 0
    assert "non200=0" in capsys.readouterr().out


def test_expect_switch_fails_with_single_sha(tmp_path: Path, capsys) -> None:
    code = cli.main(["--url", "https://hub.example/health", "--seconds", "2", "--out", str(tmp_path / "one"),
                     "--expect-switch"], prober=lambda _url, _timeout: (200, "same"),
                    clock=_Clock().time, sleep=lambda _delay: None)
    assert code == 1
    assert "distinct_shas=1" in capsys.readouterr().out


def test_summarize_is_pure_and_counts_consecutive_non_200() -> None:
    samples = [
        {"status": 200, "sha": "b"}, {"status": 502, "sha": "a"},
        {"status": 0, "sha": None}, {"status": 200, "sha": "b"},
    ]
    before = [sample.copy() for sample in samples]
    assert probe.summarize(samples) == {
        "total": 4, "non200": 2, "max_failure_streak": 2, "distinct_shas": ["b", "a"]
    }
    assert samples == before


def test_documented_command_matches_cli_flags() -> None:
    root = Path(__file__).resolve().parents[1]
    proposal = (root / "docs/proposals/USR-207-zero-downtime.md").read_text(encoding="utf-8")
    section = proposal.split("### 2.2b Roteiro de prova", maxsplit=1)[1].split("### 2.3", maxsplit=1)[0]
    command = re.search(r"python C:\\dev\\DarkFac\\scripts\\hub_downtime_probe\.py([^\n`]*)", section)
    assert command
    assert "--url" in command.group(1)
    assert "--seconds" in command.group(1)
    assert "--out" in command.group(1)
    assert "--expect-switch" in command.group(1)


class _Clock:
    def __init__(self) -> None:
        self.current = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.current

    def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.current += delay
