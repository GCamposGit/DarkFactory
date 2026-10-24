"""USR-170: audit chain distinguishes intact / unreadable / absent, fail-closed."""
from __future__ import annotations

import threading

import pytest

from core.enterprise.audit_chain import AuditReadError, ImmutableAuditChain


def _chain(tmp_path):
    return ImmutableAuditChain(root=tmp_path, log_file=tmp_path / "a" / "audit.jsonl")


def _fill(c, n=3):
    for i in range(n):
        c.record_event("u", "p", "act", f"r{i}", {"i": i})


def test_absent_vs_intact(tmp_path):
    c = _chain(tmp_path)
    assert c.verify_integrity().status == "absent"
    _fill(c)
    r = c.verify_integrity()
    assert r.is_valid and r.status == "intact" and r.total_events == 3


def test_malformed_line_blocks(tmp_path):
    c = _chain(tmp_path)
    _fill(c)
    with c.log_file.open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    r = c.verify_integrity()
    assert not r.is_valid and r.status == "unreadable"
    assert r.error_code == "malformed_line" and r.error_line == 3
    with pytest.raises(AuditReadError):
        c.record_event("u", "p", "a", "r")


def test_truncated_last_line_blocks(tmp_path):
    c = _chain(tmp_path)
    _fill(c)
    data = c.log_file.read_bytes()
    c.log_file.write_bytes(data[:-40])
    r = c.verify_integrity()
    assert not r.is_valid and r.error_code == "malformed_line"


def test_corrupted_content_detected(tmp_path):
    c = _chain(tmp_path)
    _fill(c)
    c.log_file.write_text(c.log_file.read_text("utf-8").replace('"r1"', '"rX"'), "utf-8")
    r = c.verify_integrity()
    assert not r.is_valid and r.status == "tampered"


def test_invalid_utf8_is_read_failure(tmp_path):
    c = _chain(tmp_path)
    _fill(c, 1)
    with c.log_file.open("ab") as f:
        f.write(b"\xff\xfe\n")
    r = c.verify_integrity()
    assert not r.is_valid and r.error_code == "read_failure"


def test_concurrent_appends_keep_chain_valid(tmp_path):
    c = _chain(tmp_path)
    errs = []

    def work(k):
        try:
            local = ImmutableAuditChain(root=tmp_path, log_file=c.log_file)
            for i in range(5):
                local.record_event("u", "p", "a", f"{k}-{i}")
        except Exception as e:  # pragma: no cover
            errs.append(e)

    ts = [threading.Thread(target=work, args=(k,)) for k in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs
    r = c.verify_integrity()
    assert r.is_valid and r.total_events == 20
