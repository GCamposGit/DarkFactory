"""Tests for semantic 3-way merge of demands.json (USR-112)."""

import json
from core.demands.merge import merge_demands_3way


def test_merge_identical() -> None:
    data = json.dumps([{"id": "USR-01", "title": "First", "status": "planned"}], indent=2)
    ok, res, err = merge_demands_3way(data, data, data)
    assert ok is True
    assert err is None
    assert json.loads(res) == json.loads(data)


def test_branch_updates_ticket_and_main_appends_new() -> None:
    """Criterion 1: Branch updates a ticket and main appends new tickets: auto-merge preserves both."""
    base = json.dumps([
        {"id": "USR-01", "title": "First", "status": "planned"},
        {"id": "USR-02", "title": "Second", "status": "planned"},
    ])
    # Branch completed USR-02
    ours = json.dumps([
        {"id": "USR-01", "title": "First", "status": "planned"},
        {"id": "USR-02", "title": "Second", "status": "completed", "delivery_evidence": "abc1234"},
    ])
    # Main had USR-03 and USR-04 appended concurrently
    theirs = json.dumps([
        {"id": "USR-01", "title": "First", "status": "planned"},
        {"id": "USR-02", "title": "Second", "status": "planned"},
        {"id": "USR-03", "title": "Third", "status": "planned"},
        {"id": "USR-04", "title": "Fourth", "status": "planned"},
    ])

    ok, res, err = merge_demands_3way(base, ours, theirs)
    assert ok is True
    assert err is None

    merged = json.loads(res)
    assert len(merged) == 4
    # USR-01 unchanged
    assert merged[0]["id"] == "USR-01"
    assert merged[0]["status"] == "planned"
    # USR-02 updated by branch
    assert merged[1]["id"] == "USR-02"
    assert merged[1]["status"] == "completed"
    assert merged[1]["delivery_evidence"] == "abc1234"
    # USR-03 and USR-04 preserved from main
    assert merged[2]["id"] == "USR-03"
    assert merged[3]["id"] == "USR-04"


def test_conflicting_updates_fail_closed() -> None:
    """Criterion 2: Same row modified on both sides with different values: explicit failure with ticket ID."""
    base = json.dumps([
        {"id": "USR-01", "title": "First", "status": "planned"},
    ])
    ours = json.dumps([
        {"id": "USR-01", "title": "First", "status": "completed", "delivery_evidence": "sha1"},
    ])
    theirs = json.dumps([
        {"id": "USR-01", "title": "First", "status": "in_progress", "delivery_evidence": "sha2"},
    ])

    ok, res, err = merge_demands_3way(base, ours, theirs)
    assert ok is False
    assert err is not None
    assert "Conflict for ticket USR-01" in err
    assert "modified in both branches with different values" in err


def test_both_branches_apply_same_change() -> None:
    base = json.dumps([{"id": "USR-01", "title": "First", "status": "planned"}])
    ours = json.dumps([{"id": "USR-01", "title": "First", "status": "completed"}])
    theirs = json.dumps([{"id": "USR-01", "title": "First", "status": "completed"}])

    ok, res, err = merge_demands_3way(base, ours, theirs)
    assert ok is True
    assert err is None
    merged = json.loads(res)
    assert merged[0]["status"] == "completed"


def test_title_collision_fails_closed() -> None:
    base = json.dumps([{"id": "USR-01", "title": "First", "status": "planned"}])
    ours = json.dumps([{"id": "USR-01", "title": "Branch New Title", "status": "planned"}])
    theirs = json.dumps([{"id": "USR-01", "title": "Main Original Title", "status": "planned"}])

    ok, res, err = merge_demands_3way(base, ours, theirs)
    assert ok is False
    assert err is not None
    assert "collision" in err.lower()
    assert "USR-01" in err


def test_invalid_json_fails_gracefully() -> None:
    ok, res, err = merge_demands_3way("invalid json {", "[]", "[]")
    assert ok is False
    assert err is not None
    assert "Failed to parse demands JSON" in err
