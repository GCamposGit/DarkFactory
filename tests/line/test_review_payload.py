"""Review payload manifest, bounds and fail-closed context (USR-474 T1)."""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path
from urllib import request

import pytest
from pydantic import ValidationError

from core.line import agent_cli, review_payload
from core.line.review_payload import (
    CONTEXT_SPEC_PATH,
    CONTEXT_TICKETS_PATH,
    REASON_CHANGED_OUTSIDE,
    REASON_CHANGED_UNREADABLE,
    REASON_SPEC_EMPTY,
    REASON_SPEC_MISSING,
    REASON_SPEC_NOT_INCLUDED,
    REASON_SPEC_UNREADABLE,
    REASON_TICKETS_EMPTY,
    REASON_TICKETS_MISSING,
    REASON_TICKETS_UNREADABLE,
    ReviewPayload,
    build_review_payload,
    included_text,
)

SECRET = "sk-ant-" + ("Aa1_" * 8)
_CRITERION = "o manifesto traz o sha256 da fonte"
_SPEC = "# SPEC\n\nAceite: o revisor recebe o texto da SPEC.\n"
_FILE = "core/line/sample.py"
_BODY = "def sample():\n    return 'LINHA_UNICA_DO_ARQUIVO_474'\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _tree(
    tmp_path: Path,
    *,
    spec: str | None = _SPEC,
    tickets: object | None = None,
    files: dict[str, str] | None = None,
    write_spec: bool = True,
    write_tickets: bool = True,
) -> tuple[Path, Path, list[str]]:
    root = tmp_path / "repo"
    context = tmp_path / "context"
    root.mkdir(parents=True)
    context.mkdir(parents=True)
    if write_spec and spec is not None:
        _write(context / "SPEC.md", spec)
    if write_tickets:
        payload = tickets
        if payload is None:
            payload = [{"id": "T1", "title": "Payload", "acceptance": [_CRITERION]}]
        _write(context / "tickets.json", json.dumps(payload, ensure_ascii=False))
    written = files if files is not None else {_FILE: _BODY}
    for rel, body in written.items():
        _write(root / rel, body)
    return root, context, list(written)


def _by_path(payload: ReviewPayload) -> dict[str, tuple[str, review_payload.ReviewManifestItem]]:
    manifest = {item.path: item for item in payload.manifest}
    slices = {item.path: item.text for item in payload.slices}
    assert set(slices) == set(manifest)
    return {path: (slices[path], manifest[path]) for path in manifest}


def test_payload_carries_spec_criteria_file_bytes_and_manifest(tmp_path: Path) -> None:
    root, context, changed = _tree(tmp_path)
    payload = build_review_payload(root, context, changed)

    assert payload.complete is True
    assert payload.reasons == ()
    assert "o revisor recebe o texto da SPEC" in payload.spec_text
    assert payload.criteria == (_CRITERION,)
    shown = _by_path(payload)
    assert "LINHA_UNICA_DO_ARQUIVO_474" in shown[_FILE][0]
    assert [item.path for item in payload.manifest] == [CONTEXT_SPEC_PATH, CONTEXT_TICKETS_PATH, _FILE]

    sources = {
        CONTEXT_SPEC_PATH: context / "SPEC.md",
        CONTEXT_TICKETS_PATH: context / "tickets.json",
        _FILE: root / _FILE,
    }
    for path, (text, item) in shown.items():
        raw = sources[path].read_bytes()
        assert item.sha256 == hashlib.sha256(raw).hexdigest()
        assert item.bytes_included == len(text.encode("utf-8"))
        assert item.truncated is False
        assert item.bytes_included == len(raw)


def test_alias_and_wrapped_tickets_supply_criteria(tmp_path: Path) -> None:
    tickets = {
        "tickets": [{"id": "T1", "title": "Objeto", "acceptance_criteria": ["criterio do objeto"]}],
        "criteria": ["criterio de topo"],
    }
    root, context, changed = _tree(tmp_path, tickets=tickets, files={})
    payload = build_review_payload(root, context, changed)
    assert payload.complete is True
    assert payload.criteria == ("criterio do objeto", "criterio de topo")


def test_truncation_keeps_full_source_hash(tmp_path: Path) -> None:
    body = ("b" * 40) + "FIM_QUE_NAO_PODE_ENTRAR"
    root, context, changed = _tree(
        tmp_path,
        spec="# SPEC\n\nAceite curto.\n",
        tickets=[{"id": "T1", "acceptance": ["criterio curto"]}],
        files={"wide.py": body},
    )
    payload = build_review_payload(
        root, context, changed, item_budget_bytes=24, total_budget_bytes=10_000,
    )
    text, item = _by_path(payload)["wide.py"]
    raw = (root / "wide.py").read_bytes()
    assert item.sha256 == hashlib.sha256(raw).hexdigest()
    assert item.truncated is True
    assert item.bytes_included == len(text.encode("utf-8"))
    assert item.bytes_included == 24
    assert "FIM_QUE_NAO_PODE_ENTRAR" not in text
    assert text == body.encode("utf-8")[: item.bytes_included].decode("utf-8")


def test_cut_stops_on_utf8_boundary(tmp_path: Path) -> None:
    root, context, changed = _tree(tmp_path, files={"wide.py": "ááá"})
    payload = build_review_payload(
        root, context, changed, item_budget_bytes=3, total_budget_bytes=10_000,
    )
    text, item = _by_path(payload)["wide.py"]
    assert item.truncated is True
    assert text == "á"
    assert item.bytes_included == 2
    assert item.sha256 == hashlib.sha256((root / "wide.py").read_bytes()).hexdigest()


def test_included_text_passes_through_redact_secrets(tmp_path: Path) -> None:
    spec = f"# SPEC\n\nsegredo {SECRET}\n"
    tickets = [{"id": "T1", "title": "Segredo", "acceptance": [f"nao vazar {SECRET}"]}]
    files = {"mod.py": f"token = '{SECRET}'\n"}
    root, context, changed = _tree(tmp_path, spec=spec, tickets=tickets, files=files)
    payload = build_review_payload(root, context, changed)

    dumped = payload.model_dump_json()
    visible = included_text(payload)
    assert payload.complete is True
    assert SECRET not in dumped
    assert SECRET not in visible
    assert "[REDACTED]" in payload.spec_text
    assert "[REDACTED]" in payload.criteria[0]
    assert "[REDACTED]" in _by_path(payload)["mod.py"][0]
    assert payload.manifest[2].sha256 == hashlib.sha256((root / "mod.py").read_bytes()).hexdigest()


def test_missing_or_empty_context_is_not_ready(tmp_path: Path) -> None:
    root, context, changed = _tree(tmp_path, write_spec=False, write_tickets=False)
    missing = build_review_payload(root, context, changed)
    assert missing.complete is False
    assert REASON_SPEC_MISSING in missing.reasons
    assert REASON_TICKETS_MISSING in missing.reasons
    assert missing.spec_text == ""
    assert missing.criteria == ()

    root_empty, context_empty, changed_empty = _tree(
        tmp_path / "empty", spec="  \n", tickets=[{"id": "T1", "title": "Sem aceite", "goal": "nada"}],
    )
    empty = build_review_payload(root_empty, context_empty, changed_empty)
    assert empty.complete is False
    assert REASON_SPEC_EMPTY in empty.reasons
    assert REASON_TICKETS_EMPTY in empty.reasons


def test_unreadable_spec_and_tickets_are_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, context, changed = _tree(tmp_path)
    real = review_payload._read_bytes

    def _deny_spec(path: Path) -> bytes:
        if path.name == "SPEC.md":
            raise OSError("denied")
        return real(path)

    monkeypatch.setattr(review_payload, "_read_bytes", _deny_spec)
    spec_denied = build_review_payload(root, context, changed)
    assert spec_denied.complete is False
    assert REASON_SPEC_UNREADABLE in spec_denied.reasons
    assert CONTEXT_SPEC_PATH not in {item.path for item in spec_denied.manifest}

    def _deny_tickets(path: Path) -> bytes:
        if path.name == "tickets.json":
            raise OSError("denied")
        return real(path)

    monkeypatch.setattr(review_payload, "_read_bytes", _deny_tickets)
    tickets_denied = build_review_payload(root, context, changed)
    assert tickets_denied.complete is False
    assert REASON_TICKETS_UNREADABLE in tickets_denied.reasons

    monkeypatch.setattr(review_payload, "_read_bytes", real)
    _write(context / "tickets.json", "{")
    broken = build_review_payload(root, context, changed)
    assert broken.complete is False
    assert REASON_TICKETS_UNREADABLE in broken.reasons
    assert broken.criteria == ()

    _write(context / "tickets.json", json.dumps([{"id": "T2", "acceptance": [1]}]))
    shaped = build_review_payload(root, context, changed)
    assert shaped.complete is False
    assert shaped.criteria == ()
    assert REASON_TICKETS_UNREADABLE in shaped.reasons


def test_changed_file_outside_root_is_not_read(tmp_path: Path) -> None:
    root, context, _changed = _tree(tmp_path, files={})
    outside = tmp_path / "outside.txt"
    _write(outside, f"fora {SECRET}\n")
    payload = build_review_payload(root, context, ["../outside.txt"])
    assert payload.complete is False
    assert any(reason.startswith(f"{REASON_CHANGED_OUTSIDE}:") for reason in payload.reasons)
    assert SECRET not in payload.model_dump_json()
    assert outside.read_bytes() == f"fora {SECRET}\n".encode("utf-8")
    assert all(item.path != "../outside.txt" for item in payload.manifest)


def test_missing_changed_file_is_incomplete(tmp_path: Path) -> None:
    root, context, _changed = _tree(tmp_path, files={})
    payload = build_review_payload(root, context, ["ausente.py"])
    assert payload.complete is False
    assert f"{REASON_CHANGED_UNREADABLE}:ausente.py" in payload.reasons
    assert "ausente.py" not in {item.path for item in payload.manifest}


def test_duplicate_changed_path_is_one_manifest_item(tmp_path: Path) -> None:
    root, context, changed = _tree(tmp_path)
    payload = build_review_payload(root, context, [changed[0], ".\\" + changed[0].replace("/", "\\")])
    paths = [item.path for item in payload.manifest]
    assert paths.count(_FILE) == 1
    assert payload.complete is True


def test_build_does_not_call_network_or_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, context, changed = _tree(tmp_path)

    def _network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network")

    def _agent(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("agent")

    monkeypatch.setattr(socket, "create_connection", _network)
    monkeypatch.setattr(socket.socket, "connect", _network)
    monkeypatch.setattr(request, "urlopen", _network)
    monkeypatch.setattr(agent_cli, "run_agent", _agent)
    payload = build_review_payload(root, context, changed)
    assert payload.complete is True
    assert _CRITERION in payload.criteria


def test_model_rejects_empty_context_marked_ready() -> None:
    with pytest.raises(ValidationError):
        ReviewPayload(complete=True, reasons=(REASON_SPEC_MISSING,))
    with pytest.raises(ValidationError):
        ReviewPayload(complete=False, reasons=())


def test_negative_budget_is_rejected(tmp_path: Path) -> None:
    root, context, changed = _tree(tmp_path, files={})
    with pytest.raises(ValueError):
        build_review_payload(root, context, changed, item_budget_bytes=-1)


def test_changed_paths_follow_sorted_order_for_any_input(tmp_path: Path) -> None:
    files = {"b.py": "b=1\n", "a.py": "a=1\n", "c/d.py": "d=1\n"}
    root, context, _changed = _tree(tmp_path, files=files)
    forward = ["c/d.py", "b.py", "a.py", "b.py", ".\\a.py"]
    first = build_review_payload(root, context, forward)
    second = build_review_payload(root, context, list(reversed(forward)))
    assert first.model_dump() == second.model_dump()
    assert first.complete is True
    assert [item.path for item in first.manifest] == [
        CONTEXT_SPEC_PATH, CONTEXT_TICKETS_PATH, "a.py", "b.py", "c/d.py",
    ]


def test_total_budget_is_utf8_and_stops_on_a_character_boundary(tmp_path: Path) -> None:
    spec = "SPEC_OK\n"
    tickets = [{"id": "T1", "acceptance": ["criterio curto"]}]
    root, context, _changed = _tree(
        tmp_path, spec=spec, tickets=tickets, files={"wide.py": "ááá"},
    )
    spec_size = (context / "SPEC.md").stat().st_size
    tickets_size = (context / "tickets.json").stat().st_size
    total = spec_size + tickets_size + 3
    payload = build_review_payload(
        root, context, ["wide.py"], item_budget_bytes=1000, total_budget_bytes=total,
    )
    text, item = _by_path(payload)["wide.py"]
    assert payload.complete is True
    assert text == "á"
    assert item.truncated is True
    assert item.bytes_included == 2
    assert text.encode("utf-8").decode("utf-8") == text
    assert sum(entry.bytes_included for entry in payload.manifest) <= total
    assert all(entry.bytes_included <= 1000 for entry in payload.manifest)
    assert item.sha256 == hashlib.sha256((root / "wide.py").read_bytes()).hexdigest()


def test_redaction_runs_before_the_byte_cut(tmp_path: Path) -> None:
    body = ("A" * 12) + SECRET + "FIM_SECRETO"
    root, context, changed = _tree(
        tmp_path,
        spec="SPEC_OK\n",
        tickets=[{"id": "T1", "acceptance": ["criterio curto"]}],
        files={"mod.py": body},
    )
    payload = build_review_payload(
        root, context, changed, item_budget_bytes=16, total_budget_bytes=100_000,
    )
    text, item = _by_path(payload)["mod.py"]
    raw_prefix = body.encode("utf-8")[:16].decode("utf-8")
    assert "sk-a" in raw_prefix
    assert "sk-a" not in text
    assert SECRET not in text
    assert item.truncated is True
    assert item.bytes_included <= 16
    assert item.sha256 == hashlib.sha256(body.encode("utf-8")).hexdigest()
    redacted = agent_cli.redact_secrets(body)
    assert text == redacted.encode("utf-8")[: item.bytes_included].decode("utf-8")


def test_absolute_and_duplicate_outside_paths_are_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, context, _changed = _tree(tmp_path, files={})
    outside = tmp_path / "secret.txt"
    _write(outside, f"fora {SECRET}\n")
    reads: list[Path] = []
    real = review_payload._read_bytes

    def _spy(path: Path) -> bytes:
        reads.append(path)
        return real(path)

    monkeypatch.setattr(review_payload, "_read_bytes", _spy)
    payload = build_review_payload(root, context, [str(outside), outside.as_posix(), str(outside)])
    assert payload.complete is False
    assert sum(reason.startswith(f"{REASON_CHANGED_OUTSIDE}:") for reason in payload.reasons) == 1
    assert SECRET not in payload.model_dump_json()
    assert all(path.resolve() != outside.resolve() for path in reads)
    assert outside.read_bytes() == f"fora {SECRET}\n".encode("utf-8")


def test_unreadable_or_invalid_changed_file_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, context, changed = _tree(tmp_path)
    real = review_payload._read_bytes

    def _deny(path: Path) -> bytes:
        if path.name == "sample.py":
            raise OSError("denied")
        return real(path)

    monkeypatch.setattr(review_payload, "_read_bytes", _deny)
    denied = build_review_payload(root, context, changed)
    assert denied.complete is False
    assert f"{REASON_CHANGED_UNREADABLE}:{_FILE}" in denied.reasons
    assert _FILE not in {item.path for item in denied.manifest}

    monkeypatch.setattr(review_payload, "_read_bytes", real)
    (root / "bin.dat").write_bytes(b"\xff\xfe nao e utf-8")
    invalid = build_review_payload(root, context, ["bin.dat"])
    assert invalid.complete is False
    assert f"{REASON_CHANGED_UNREADABLE}:bin.dat" in invalid.reasons
    assert "bin.dat" not in {item.path for item in invalid.manifest}
    assert invalid.spec_text


def test_spec_cut_to_blank_is_not_ready(tmp_path: Path) -> None:
    root, context, changed = _tree(
        tmp_path,
        spec="\n\nCONTEUDO_DA_SPEC",
        tickets=[{"id": "T1", "acceptance": ["criterio curto"]}],
        files={},
    )
    payload = build_review_payload(
        root, context, changed, item_budget_bytes=1, total_budget_bytes=1,
    )
    assert payload.complete is False
    assert REASON_SPEC_NOT_INCLUDED in payload.reasons
    assert payload.spec_text.strip() == ""
    assert review_payload.context_blocks_review(payload) is True
