from __future__ import annotations

import json

from core.infra.remote_data import RemoteData, from_env, read_json


def test_remote_data_caches_by_ttl_and_refreshes():
    now = [0.0]
    values = iter(([{"id": 1}], [{"id": 2}]))
    reader = RemoteData("o/r", ttl=10, fetch=lambda path: next(values), clock=lambda: now[0])
    assert reader.get(".factory/demands/demands.json") == [{"id": 1}]
    assert reader.get(".factory/demands/demands.json") == [{"id": 1}]
    now[0] = 10
    assert reader.get(".factory/demands/demands.json") == [{"id": 2}]


def test_remote_failure_keeps_last_good_and_backs_off():
    now = [0.0]
    calls = [0]

    def fetch(_path):
        calls[0] += 1
        if calls[0] == 1:
            return {"version": 1}
        raise OSError("offline")

    reader = RemoteData("o/r", ttl=5, fetch=fetch, clock=lambda: now[0])
    assert reader.get("docs/example.json") == {"version": 1}
    now[0] = 6
    assert reader.get("docs/example.json") == {"version": 1}
    assert reader.get("docs/example.json") == {"version": 1}
    assert calls == [2]


def test_remote_reader_never_raises_and_local_json_is_fallback(tmp_path):
    local = tmp_path / "demands.json"
    local.write_text('{"demands": [{"id": "LOCAL-1"}]}', encoding="utf-8")
    reader = RemoteData("o/r", fetch=lambda _path: (_ for _ in ()).throw(RuntimeError("down")))
    assert read_json(local, reader, ".factory/demands/demands.json") == {"demands": [{"id": "LOCAL-1"}]}


def test_invalid_remote_shape_uses_the_local_fallback(tmp_path):
    local = tmp_path / "demands.json"
    local.write_text('{"demands": [{"id": "LOCAL-1"}]}', encoding="utf-8")
    reader = RemoteData("o/r", fetch=lambda _path: {"unexpected": []})
    payload = read_json(
        local,
        reader,
        ".factory/demands/demands.json",
        validator=lambda value: isinstance(value, list) or (
            isinstance(value, dict) and isinstance(value.get("demands"), list)
        ),
    )
    assert payload == {"demands": [{"id": "LOCAL-1"}]}


def test_opt_out_disables_remote_reader(monkeypatch):
    monkeypatch.setenv("DARKHUB_REMOTE_DATA", "off")
    assert from_env() is None
