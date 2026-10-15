"""Lock policy for complete versus explicitly selected pytest runs (USR-179)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests import conftest as pytest_plugin


def _config(
    *, args: list[str], keyword: str = "", markexpr: str = "", numprocesses: int | None = 2
) -> SimpleNamespace:
    return SimpleNamespace(
        args=args,
        option=SimpleNamespace(keyword=keyword, markexpr=markexpr, deselect=[], ignore=[], ignore_glob=[]),
        getini=lambda name: ["tests"] if name == "testpaths" else [],
        getoption=lambda name: numprocesses if name == "numprocesses" else None,
    )


def _session(config: SimpleNamespace, item_count: int) -> SimpleNamespace:
    return SimpleNamespace(config=config, items=[object() for _ in range(item_count)])


def test_explicit_focal_selection_does_not_acquire_lock_even_with_many_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acquired: list[bool] = []
    monkeypatch.setattr(pytest_plugin, "_session_suite_lock", None)
    monkeypatch.setattr(pytest_plugin, "_lock_should_be_skipped", lambda _config: False)
    monkeypatch.setattr(pytest_plugin, "_start_tree_guard", lambda _config: None)
    monkeypatch.setattr(pytest_plugin, "_acquire_session_lock", lambda: acquired.append(True))

    session = _session(_config(args=["tests/test_suite_lock.py::test_example"]), 140)
    pytest_plugin.pytest_sessionstart(session)
    pytest_plugin.pytest_collection_finish(session)

    assert acquired == []


def test_complete_xdist_suite_takes_lock_before_workers_start(monkeypatch: pytest.MonkeyPatch) -> None:
    acquired: list[bool] = []
    config = _config(args=["tests"])
    session = _session(config, 140)
    monkeypatch.setattr(pytest_plugin, "_session_suite_lock", None)
    monkeypatch.setattr(pytest_plugin, "_lock_should_be_skipped", lambda _config: False)
    monkeypatch.setattr(pytest_plugin, "_start_tree_guard", lambda _config: None)

    def acquire() -> None:
        acquired.append(True)
        monkeypatch.setattr(pytest_plugin, "_session_suite_lock", object())

    monkeypatch.setattr(pytest_plugin, "_acquire_session_lock", acquire)

    # xdist requested: default testpaths identify a complete run at
    # sessionstart, before creating workers; it holds the lock through execution.
    pytest_plugin.pytest_sessionstart(session)
    assert acquired == [True]
    pytest_plugin.pytest_collection_finish(session)

    assert acquired == [True]


@pytest.mark.parametrize("filter_name", ["keyword", "markexpr"])
def test_pytest_filter_is_an_explicit_focal_selection(filter_name: str) -> None:
    config = _config(args=["tests"], **{filter_name: "selected"})

    assert pytest_plugin._is_explicit_focal_selection(config) is True


def test_default_testpaths_are_not_a_focal_selection() -> None:
    assert pytest_plugin._is_explicit_focal_selection(_config(args=["tests"])) is False


def test_non_xdist_complete_suite_takes_lock_after_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    acquired: list[bool] = []
    monkeypatch.setattr(pytest_plugin, "_session_suite_lock", None)
    monkeypatch.setattr(pytest_plugin, "_lock_should_be_skipped", lambda _config: False)
    monkeypatch.setattr(pytest_plugin, "_start_tree_guard", lambda _config: None)
    monkeypatch.setattr(pytest_plugin, "_acquire_session_lock", lambda: acquired.append(True))

    session = _session(_config(args=["tests"], numprocesses=None), 140)
    pytest_plugin.pytest_sessionstart(session)
    assert acquired == []
    pytest_plugin.pytest_collection_finish(session)

    assert acquired == [True]
