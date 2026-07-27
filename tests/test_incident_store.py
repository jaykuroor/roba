"""Incidents as first-class objects (Phase 4).

Detection stays derived — signals remain the source of truth — but the manager
now keeps a row per incident so it can be acknowledged, resolved, and looked up
afterwards. ``reconcile_incidents`` is the pure core; everything else is a thin
stdlib-sqlite3 wrapper around it.

Gate:
- reconcile opens what is new, auto-resolves what vanished, leaves the rest;
- a resolved incident that recurs opens a *new* row (two episodes, not one);
- ack survives a manager restart (the "done when");
- a remediated food-safety check auto-resolves instead of waiting out its TTL.
"""

import asyncio

import pytest

import manager


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Relocate the whole manager footprint into a tmp dir."""
    monkeypatch.setattr(manager, "STATE_DIR", tmp_path)
    return tmp_path


def _derived(instance_id="running_fox", category="stockout", summary="Running low on Basil.",
             **extra):
    return {"instance_id": instance_id, "category": category, "summary": summary,
            "created_at": 100.0, "count": 1, "names": [], "signal_type": "LOW_STOCK",
            "source_signal_id": "sig-1", **extra}


def _rows(store_dir, where=""):
    conn = manager.incidents_db()
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM incidents {where}")]
    finally:
        conn.close()


# -- the pure core ---------------------------------------------------------


def test_reconcile_opens_new_and_leaves_existing_alone():
    derived = [_derived(), _derived(category="staff_no_show", summary="Marco is sick.")]
    stored = [{"incident_id": 1, **_derived(), "status": "open"}]

    plan = manager.reconcile_incidents(derived, stored)

    assert [r["summary"] for r in plan["open"]] == ["Marco is sick."]
    assert plan["resolve"] == []


def test_reconcile_auto_resolves_when_the_source_disappears():
    stored = [
        {"incident_id": 1, **_derived(), "status": "open"},
        {"incident_id": 2, **_derived(summary="Marco is sick."), "status": "acked"},
    ]
    plan = manager.reconcile_incidents([], stored)

    assert plan["open"] == []
    assert plan["resolve"] == [1, 2]        # acked rows auto-resolve too


def test_reconcile_ignores_already_resolved_rows():
    """A resolved row is history — it must not be resolved twice, nor block a
    recurrence from opening a fresh row."""
    stored = [{"incident_id": 1, **_derived(), "status": "resolved"}]

    plan = manager.reconcile_incidents([_derived()], stored)

    assert plan["resolve"] == []
    assert len(plan["open"]) == 1           # the recurrence opens a new episode


def test_reconcile_key_separates_instances():
    """The same problem at two restaurants is two incidents."""
    derived = [_derived(instance_id="running_fox"), _derived(instance_id="quiet_otter")]
    plan = manager.reconcile_incidents(derived, [])
    assert len(plan["open"]) == 2


# -- persistence -----------------------------------------------------------


def test_apply_reconcile_round_trip(store):
    conn = manager.incidents_db()
    try:
        live = manager.apply_reconcile(conn, [_derived()])
        assert len(live) == 1
        assert live[0]["status"] == "open"
        assert live[0]["opened_at"] == 100.0
        assert live[0]["source_signal_id"] == "sig-1"

        # Polling again with the same derived set must not duplicate the row.
        live = manager.apply_reconcile(conn, [_derived()])
        assert len(live) == 1

        # Source gone → auto-resolved, and no longer live.
        assert manager.apply_reconcile(conn, []) == []
    finally:
        conn.close()

    all_rows = _rows(store)
    assert len(all_rows) == 1
    assert all_rows[0]["status"] == "resolved"
    assert all_rows[0]["resolved_at"] is not None


def test_recurrence_opens_a_second_episode(store):
    conn = manager.incidents_db()
    try:
        manager.apply_reconcile(conn, [_derived()])
        manager.apply_reconcile(conn, [])            # resolves it
        manager.apply_reconcile(conn, [_derived()])  # it comes back
    finally:
        conn.close()

    rows = sorted(_rows(store), key=lambda r: r["incident_id"])
    assert [r["status"] for r in rows] == ["resolved", "open"]


def test_ack_survives_a_manager_restart(store):
    """The phase's "done when": acknowledge, restart, still acknowledged."""
    conn = manager.incidents_db()
    try:
        incident_id = manager.apply_reconcile(conn, [_derived()])[0]["incident_id"]
    finally:
        conn.close()

    manager.ack_incident(incident_id, manager.AckBody(acked_by="jay"))

    # "Restart": every connection is opened fresh from STATE_DIR anyway, so a
    # new poll against a new connection is exactly what a restart replays.
    conn = manager.incidents_db()
    try:
        live = manager.apply_reconcile(conn, [_derived()])
    finally:
        conn.close()

    assert len(live) == 1
    assert live[0]["status"] == "acked"
    assert live[0]["acked_by"] == "jay"
    assert live[0]["incident_id"] == incident_id


def test_resolve_by_hand_then_reconcile_does_not_reopen(store):
    """Manual resolve is how an un-retractable signal gets closed."""
    conn = manager.incidents_db()
    try:
        incident_id = manager.apply_reconcile(conn, [_derived()])[0]["incident_id"]
    finally:
        conn.close()

    manager.resolve_incident(incident_id)

    conn = manager.incidents_db()
    try:
        live = manager.apply_reconcile(conn, [_derived()])
    finally:
        conn.close()

    # The old row stays resolved; the still-live source opens a fresh episode.
    assert live[0]["incident_id"] != incident_id
    rows = sorted(_rows(store), key=lambda r: r["incident_id"])
    assert [r["status"] for r in rows] == ["resolved", "open"]


def test_ack_unknown_incident_is_404(store):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as excinfo:
        manager.ack_incident(999)
    assert excinfo.value.status_code == 404


def test_history_returns_resolved_rows_and_filters(store):
    conn = manager.incidents_db()
    try:
        manager.apply_reconcile(conn, [
            _derived(instance_id="running_fox", created_at=100.0),
            _derived(instance_id="quiet_otter", summary="Marco is sick.", created_at=500.0),
        ])
        manager.apply_reconcile(conn, [])   # resolve both
    finally:
        conn.close()

    everything = manager.incidents_history()["incidents"]
    assert len(everything) == 2
    assert all(r["status"] == "resolved" for r in everything)
    # Newest first.
    assert everything[0]["opened_at"] == 500.0

    assert len(manager.incidents_history(instance_id="running_fox")["incidents"]) == 1
    assert len(manager.incidents_history(since=200.0)["incidents"]) == 1


# -- remediated food-safety checks -----------------------------------------


def _safety_signal(task_id=2):
    return {"type": "FOOD_SAFETY_CHECK", "created_at": 30.0, "signal_id": "sig-9",
            "payload": {"task_id": task_id, "title": "Afternoon fridge temperature log",
                        "category": "temp", "outcome": "not_done", "severity": "high",
                        "note": "door seal broken"}}


def test_remediated_safety_check_drops_out_of_the_derived_set():
    """Its signal lives 24h and cannot be retracted, so the *snapshot* decides."""
    still_failing = manager.merge_incidents([_safety_signal()], [], {}, {2})
    assert [r["category"] for r in still_failing] == ["food_safety_checks"]

    fixed = manager.merge_incidents([_safety_signal()], [], {}, set())
    assert fixed == []

    # No snapshot (child unreachable) must not silently close anything.
    unknown = manager.merge_incidents([_safety_signal()], [], {}, None)
    assert [r["category"] for r in unknown] == ["food_safety_checks"]


def test_remediated_safety_check_auto_resolves_its_incident(store):
    conn = manager.incidents_db()
    try:
        derived = [{**manager.merge_incidents([_safety_signal()], [], {}, {2})[0],
                    "instance_id": "running_fox", "restaurant": "Bella's"}]
        incident_id = manager.apply_reconcile(conn, derived)[0]["incident_id"]

        # The kitchen does the check: it leaves the snapshot's safety_issues.
        after = [{**r, "instance_id": "running_fox", "restaurant": "Bella's"}
                 for r in manager.merge_incidents([_safety_signal()], [], {}, set())]
        assert manager.apply_reconcile(conn, after) == []
    finally:
        conn.close()

    rows = _rows(store)
    assert rows[0]["incident_id"] == incident_id
    assert rows[0]["status"] == "resolved"


# -- the endpoint ----------------------------------------------------------


def test_incidents_endpoint_carries_ack_state(store, monkeypatch):
    async def fake_get(_inst, path):
        if path.startswith("/api/signals"):
            return [_safety_signal()]
        if path.startswith("/api/ops/snapshot"):
            return {"safety_issues": [{"task_id": 2}]}
        if path.startswith("/api/track-b/procurement/plan"):
            return {"items": []}
        return []

    monkeypatch.setattr(manager, "_get", fake_get)
    monkeypatch.setattr(manager.registry, "instances", {
        "running_fox": {"id": "running_fox", "title": "Bella's"},
    })

    first = asyncio.run(manager.incidents())["incidents"]
    assert len(first) == 1
    assert first[0]["status"] == "open"
    incident_id = first[0]["incident_id"]
    assert incident_id is not None

    manager.ack_incident(incident_id, manager.AckBody(acked_by="jay"))
    second = asyncio.run(manager.incidents())["incidents"]

    assert second[0]["incident_id"] == incident_id
    assert second[0]["status"] == "acked"
    assert second[0]["acked_by"] == "jay"
