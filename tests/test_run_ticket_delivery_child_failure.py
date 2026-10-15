"""USR-257: a failed delivery child must show cause, attempts and resume command on the console."""
from __future__ import annotations

from pathlib import Path

from core.line.agent_cli import BoundedProcessResult

import run_ticket


def _report(proc: BoundedProcessResult, info: dict, is_json: bool, capsys) -> str:
    run_ticket._report_delivery_child_failure(proc, info, is_json, "USR-9", Path("wt"))
    return capsys.readouterr().err


def test_json_failure_prints_cause_error_attempts_and_resume(capsys) -> None:
    info = {
        "ok": False, "cause": "gate_failed", "error": "boom", "exit_code": 3,
        "resume_command": "python X --resume-delivery USR-9",
        "self_correction_log": [{
            "event": "reinvocation", "attempt": 1, "stage": "gate", "cause": "tests",
            "duration_s": 2.0, "limit_s": 10.0, "changed": True,
        }],
    }
    err = _report(BoundedProcessResult(returncode=3), info, True, capsys)
    assert "[ERRO NA ENTREGA] causa: gate_failed; erro: boom" in err
    assert "AUTOCORREÇÃO" in err
    assert "python X --resume-delivery USR-9" in err


def test_non_json_output_prints_exit_code_duration_and_tails(capsys) -> None:
    out = "\n".join(f"o{i}" for i in range(50))
    err_text = "\n".join(f"e{i}" for i in range(50))
    proc = BoundedProcessResult(stdout=out, stderr=err_text, returncode=-9, duration_s=12.5, timed_out=True)
    err = _report(proc, {"ok": False, "error": "x"}, False, capsys)
    assert "codigo de saida -9" in err and "12.5s" in err and "timeout" in err
    assert "o49" in err and "e49" in err and "o29\n" not in err
    assert "--resume-delivery USR-9" in err


def test_launcher_wiring_calls_the_failure_report() -> None:
    import inspect
    src = inspect.getsource(run_ticket)
    assert "_report_delivery_child_failure(" in src
