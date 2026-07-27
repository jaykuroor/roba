"""Tests for the multi-restaurant manager (manager.py).

No processes are spawned — these cover the derivation logic the dashboard
depends on (instance ids, status rules, issue building, ranking) plus the card
fan-out, which is exercised by stubbing ``manager._get`` rather than talking to
a real child.
"""
import asyncio

import manager


def test_generate_instance_id_shape_and_uniqueness():
    taken = set()
    for _ in range(50):
        name = manager.generate_instance_id(taken)
        assert name not in taken
        adjective, _, animal = name.partition("_")
        assert adjective in manager.ADJECTIVES
        assert animal.rstrip("0123456789") in manager.ANIMALS
        taken.add(name)


def test_derive_status_offline_beats_everything():
    assert manager.derive_status(
        online=False, snapshot=None, warnings=[{"x": 1}], pending_approvals=[]
    ) == "offline"


def test_derive_status_levels():
    healthy = {"low_stock_ingredients": [], "stations": [{"covered": True}], "staff": [{"status": "present"}]}
    assert manager.derive_status(online=True, snapshot=healthy, warnings=[], pending_approvals=[]) == "normal"

    # uncoverable ingredient → critical
    assert manager.derive_status(online=True, snapshot=healthy, warnings=[{"ingredient_name": "flour"}], pending_approvals=[]) == "critical"

    # depleted stock → critical; below safety → warning
    depleted = {**healthy, "low_stock_ingredients": [{"status": "depleted"}]}
    low = {**healthy, "low_stock_ingredients": [{"status": "below_safety_stock"}]}
    assert manager.derive_status(online=True, snapshot=depleted, warnings=[], pending_approvals=[]) == "critical"
    assert manager.derive_status(online=True, snapshot=low, warnings=[], pending_approvals=[]) == "warning"

    # unstaffed station → critical; absent staff (covered elsewhere) → warning
    unstaffed = {**healthy, "stations": [{"covered": False}]}
    absent = {**healthy, "staff": [{"status": "sick"}]}
    assert manager.derive_status(online=True, snapshot=unstaffed, warnings=[], pending_approvals=[]) == "critical"
    assert manager.derive_status(online=True, snapshot=absent, warnings=[], pending_approvals=[]) == "warning"

    # pending approvals → warning, critical-urgency approval → critical
    assert manager.derive_status(online=True, snapshot=healthy, warnings=[], pending_approvals=[{"urgency": "normal"}]) == "warning"
    assert manager.derive_status(online=True, snapshot=healthy, warnings=[], pending_approvals=[{"urgency": "uncoverable"}]) == "critical"


def test_derive_status_kitchen_backlog():
    """A growing pass is a manager-visible problem (docs/fable/progress.md Phase 1)."""
    healthy = {"low_stock_ingredients": [], "stations": [{"covered": True}],
               "staff": [{"status": "present"}]}

    def at(backlog):
        return manager.derive_status(
            online=True, snapshot={**healthy, "queued_count": backlog},
            warnings=[], pending_approvals=[],
        )

    assert at(0) == "normal"
    assert at(manager.BACKLOG_WARN - 1) == "normal"
    assert at(manager.BACKLOG_WARN) == "warning"
    assert at(manager.BACKLOG_CRIT - 1) == "warning"
    assert at(manager.BACKLOG_CRIT) == "critical"
    # A snapshot from before the lifecycle existed has no queued_count at all.
    assert manager.derive_status(online=True, snapshot=healthy, warnings=[],
                                 pending_approvals=[]) == "normal"


def test_derive_status_failed_food_safety_check_is_critical():
    """A missed temperature log is never "have a look later" (Phase 2)."""
    healthy = {"low_stock_ingredients": [], "stations": [{"covered": True}],
               "staff": [{"status": "present"}]}
    failed = {**healthy, "safety_issues": [
        {"title": "Record walk-in fridge & freezer temperatures",
         "outcome": "not_done", "note": "walk-in reading 9C", "severity": "high"},
    ]}
    assert manager.derive_status(online=True, snapshot=failed, warnings=[],
                                 pending_approvals=[]) == "critical"
    # An empty list is not a failure, and a snapshot predating Phase 2 has no key.
    assert manager.derive_status(online=True, snapshot={**healthy, "safety_issues": []},
                                 warnings=[], pending_approvals=[]) == "normal"
    assert manager.derive_status(online=True, snapshot=healthy, warnings=[],
                                 pending_approvals=[]) == "normal"


def test_build_issues_surfaces_failed_safety_checks():
    snapshot = {"safety_issues": [
        {"task_id": 4, "title": "Midday hot-holding temperature check",
         "outcome": "not_done", "note": "unit reading 51C", "severity": "high",
         "overdue_min": 0},
        {"task_id": 9, "title": "Break down & sanitize the deli slicer",
         "outcome": "overdue", "note": None, "severity": "high", "overdue_min": 20},
    ]}
    issues = manager.build_issues("running_fox", "Bella's", snapshot=snapshot,
                                  warnings=[], pending_approvals=[])
    safety = [i for i in issues if i["kind"] == "safety"]
    assert len(safety) == 2
    assert all(i["severity"] == "critical" for i in safety)
    not_done = next(i for i in safety if "hot-holding" in i["problem"])
    assert "unit reading 51C" in not_done["impact"]
    overdue = next(i for i in safety if "slicer" in i["problem"])
    assert "20 min past due" in overdue["impact"]
    # Raw payload keys never leak into what the manager reads.
    assert "not_done" not in not_done["problem"]


def test_merge_incidents_phrases_food_safety_checks():
    signals = [
        {"type": "FOOD_SAFETY_CHECK", "created_at": 30.0, "payload": {
            "task_id": 2, "title": "Record walk-in fridge & freezer temperatures",
            "category": "temp", "outcome": "not_done", "severity": "high",
            "note": "walk-in reading 9C"}},
        {"type": "FOOD_SAFETY_CHECK", "created_at": 31.0, "payload": {
            "task_id": 9, "title": "Break down & sanitize the deli slicer",
            "category": "safety", "outcome": "overdue", "severity": "high",
            "overdue_min": 20}},
    ]
    rows = manager.merge_incidents(signals, [], {})
    checks = [r for r in rows if r["category"] == "food_safety_checks"]
    assert len(checks) == 2, checks

    not_done = next(r for r in checks if "fridge" in r["summary"])
    assert not_done["summary"] == (
        "Record walk-in fridge & freezer temperatures — the kitchen reported "
        "this not done: walk-in reading 9C."
    )
    overdue = next(r for r in checks if "slicer" in r["summary"])
    assert "20 min past due" in overdue["summary"]
    # No raw status codes or payload keys reach the manager.
    for row in checks:
        assert "not_done" not in row["summary"] and "outcome" not in row["summary"]


def test_food_safety_checks_is_no_longer_an_unavailable_category():
    assert manager.SIGNAL_TO_INCIDENT["FOOD_SAFETY_CHECK"] == "food_safety_checks"


def test_build_issues_and_ranking():
    snapshot = {
        "low_stock_ingredients": [{"ingredient": "milk", "status": "below_safety_stock", "on_hand_display": "200 ml"}],
        "stations": [{"station": "grill", "covered": False, "dishes": ["burger"]}],
        "staff": [{"name": "Marco", "status": "sick", "sole_cover_dishes_at_risk": ["risotto"]}],
    }
    approvals = [
        {"id": 7, "title": "PO #12", "summary": "€600 order", "urgency": "normal", "created_at": 1000.0},
        {"id": 8, "title": "Emergency PO", "summary": "", "urgency": "uncoverable", "created_at": 2000.0},
    ]
    warnings = [{"ingredient_name": "mascarpone", "short_qty": 1500, "unit": "g", "reason": "no supplier"}]

    issues = manager.build_issues("running_fox", "Bella's", snapshot=snapshot,
                                  warnings=warnings, pending_approvals=approvals)
    ranked = manager.rank_issues(issues)

    # criticals first: the uncoverable approval and the uncoverable warning
    assert {i["severity"] for i in ranked[:2]} == {"critical"}
    # approval deadline = created + TTL
    po = next(i for i in ranked if i.get("approval_id") == 7)
    assert po["deadline_sim"] == 1000.0 + manager.APPROVAL_TTL_SIM_S
    # every issue carries the restaurant identity for the queue UI
    assert all(i["instance_id"] == "running_fox" and i["restaurant"] == "Bella's" for i in ranked)
    # deadline ordering inside the same severity: earlier deadline first
    crit = [i for i in ranked if i["severity"] == "critical"]
    with_deadline = [i for i in crit if i["deadline_sim"] is not None]
    assert crit.index(with_deadline[0]) == 0


def test_rank_issues_no_deadline_sorts_last_within_severity():
    issues = [
        {"severity": "high", "deadline_sim": None},
        {"severity": "high", "deadline_sim": 50.0},
        {"severity": "critical", "deadline_sim": None},
    ]
    ranked = manager.rank_issues(issues)
    assert ranked[0]["severity"] == "critical"
    assert ranked[1]["deadline_sim"] == 50.0


def test_join_names_deterministic():
    assert manager.join_names(["Tomato"]) == "Tomato"
    assert manager.join_names(["Tomato", "Basil"]) == "Basil and Tomato"
    assert manager.join_names(["Tomato", "Basil", "Tomato"]) == "Basil and Tomato"
    assert manager.join_names([]) == "some items"


def test_merge_incidents_batches_supplier_delays_and_kills_raw_status():
    items = [
        {"status": "at_risk", "supplier_name": "GreenFarm Produce", "ingredient_name": "Tomato", "order_date": 1.0},
        {"status": "at_risk", "supplier_name": "GreenFarm Produce", "ingredient_name": "Basil", "order_date": 2.0},
        {"status": "at_risk", "supplier_name": "GreenFarm Produce", "ingredient_name": "Romaine Lettuce", "order_date": 3.0},
        {"status": "at_risk", "supplier_name": "GreenFarm Produce", "ingredient_name": "Romaine Lettuce", "order_date": 4.0},
        {"status": "uncoverable", "supplier_name": "GreenFarm Produce", "ingredient_name": "Mascarpone", "order_date": 5.0},
        {"status": "planned", "supplier_name": "GreenFarm Produce", "ingredient_name": "Flour"},
    ]
    rows = manager.merge_incidents([], items, {})
    delays = [r for r in rows if r["category"] == "supplier_delay"]
    # one merged row per (supplier, status); "planned" is not an incident
    assert len(delays) == 2, delays
    at_risk = next(r for r in delays if "may arrive late" in r["summary"])
    assert at_risk["summary"].startswith(
        "Basil, Romaine Lettuce and Tomato from GreenFarm Produce"
    ), at_risk["summary"]
    assert "at_risk" not in at_risk["summary"]
    assert at_risk["count"] == 4 and at_risk["created_at"] == 4.0
    uncoverable = next(r for r in delays if "cannot be delivered" in r["summary"])
    assert uncoverable["names"] == ["Mascarpone"]


def test_merge_incidents_batches_signals_and_humanizes():
    signals = [
        {"type": "LOW_STOCK", "payload": {"ingredient_id": 1}, "created_at": 10.0},
        {"type": "LOW_STOCK", "payload": {"ingredient_id": 2}, "created_at": 11.0},
        {"type": "INGREDIENT_UNCOVERABLE", "payload": {"ingredient_name": "Mascarpone"}, "created_at": 12.0},
        {"type": "STAFF_COVERAGE", "payload": {"covered": True, "shortfall": 0.0}, "created_at": 13.0},
        {"type": "STAFF_COVERAGE", "payload": {"covered": False, "station_name": "Grill"}, "created_at": 14.0},
        {"type": "STAFF_AVAILABILITY", "payload": {"staff_name": "Marco", "status": "sick"}, "created_at": 15.0},
        {"type": "DEMAND_FORECAST", "payload": {}, "created_at": 16.0},
    ]
    rows = manager.merge_incidents(signals, [], {1: "Milk", 2: "Butter"})
    low = next(r for r in rows if r["signal_type"] == "LOW_STOCK")
    assert low["summary"] == "Running low on Butter and Milk (at or below safety stock)."
    assert low["count"] == 2 and low["created_at"] == 11.0
    assert any("Grill has no qualified cover" in r["summary"] for r in rows)
    assert any(r["summary"] == "Marco is sick." for r in rows)
    # routine covered-station broadcast and unmapped signal types are dropped
    staff_rows = [r for r in rows if r["category"] == "staff_no_show"]
    assert len(staff_rows) == 2, staff_rows
    assert not any(r["signal_type"] == "DEMAND_FORECAST" for r in rows)


def test_build_issues_notice_kind_changes_recommendation():
    approvals = [
        {"id": 1, "title": "Overdue kitchen task: X", "kind": "notice",
         "urgency": "high", "created_at": 0.0, "summary": ""},
        {"id": 2, "title": "PO #9", "kind": "decision",
         "urgency": "normal", "created_at": 0.0, "summary": ""},
    ]
    issues = manager.build_issues("running_fox", "Bella's", snapshot={},
                                  warnings=[], pending_approvals=approvals)
    notice = next(i for i in issues if i["approval_id"] == 1)
    decision = next(i for i in issues if i["approval_id"] == 2)
    assert notice["approval_kind"] == "notice"
    assert "Acknowledge" in notice["recommended_action"]
    assert decision["approval_kind"] == "decision"
    assert "Approve" in decision["recommended_action"]


# ---------------------------------------------------------------------------
# Card fan-out
#
# The pattern for every later phase: stub ``manager._get`` with the child
# responses the card is assembled from, and call ``_instance_overview``
# directly. No child process, no HTTP, no registry.
# ---------------------------------------------------------------------------

def _overview(monkeypatch, responses):
    async def fake_get(_inst, path):
        for prefix, payload in responses.items():
            if path.startswith(prefix):
                return payload
        return None

    monkeypatch.setattr(manager, "_get", fake_get)
    return asyncio.run(manager._instance_overview(
        {"id": "running_fox", "title": "Bella's", "preset": "bellas_kitchen", "port": 8101}
    ))


def test_instance_overview_reports_tickets_from_the_snapshot(monkeypatch):
    card = _overview(monkeypatch, {
        "/api/health": {"sim": {"sim_time": 30000.0, "day_number": 0}},
        "/api/ops/snapshot": {
            "staff": [{"name": "Luca", "status": "present"}],
            "dishes": [], "stations": [], "low_stock_ingredients": [],
            "queued_count": 11, "cooking_count": 1, "avg_ticket_minutes": 7.5,
            "safety_issues": [], "task_compliance": {"rate": 0.9, "accountable": 10},
        },
        "/api/pos/stats": {"revenue": 120.0, "orders": 9},
        "/api/approvals": [],
        "/api/track-b/procurement/warnings": [],
    })
    assert card["orders_waiting"] == 11
    assert card["ticket_time_min"] == 7.5
    assert card["status"] == "warning"  # 11 ≥ BACKLOG_WARN
    # The snapshot carries the failures; the card carries a count.
    assert card["safety_issues"] == 0
    assert card["task_compliance"] == {"rate": 0.9, "accountable": 10}


def test_instance_overview_counts_safety_failures(monkeypatch):
    card = _overview(monkeypatch, {
        "/api/health": {"sim": {"sim_time": 48000.0}},
        "/api/ops/snapshot": {
            "staff": [], "dishes": [], "stations": [], "low_stock_ingredients": [],
            "queued_count": 0, "avg_ticket_minutes": None,
            "safety_issues": [
                {"task_id": 2, "title": "Afternoon fridge temperature log",
                 "outcome": "not_done", "note": "door seal broken", "severity": "high"},
            ],
            "task_compliance": {"rate": 0.5, "accountable": 4},
        },
        "/api/pos/stats": {}, "/api/approvals": [],
        "/api/track-b/procurement/warnings": [],
    })
    assert card["safety_issues"] == 1
    assert card["status"] == "critical"
    assert [i["kind"] for i in card["issues"]] == ["safety"]


def test_offline_card_reports_no_ticket_numbers(monkeypatch):
    """An offline card must not show the last numbers it saw."""
    monkeypatch.setattr(manager.registry, "instances", {})
    card = _overview(monkeypatch, {})
    assert card["online"] is False
    assert card["orders_waiting"] is None and card["ticket_time_min"] is None
    # Every card key the UI reads exists on both branches.
    online = _overview(monkeypatch, {
        "/api/health": {"sim": {"sim_time": 0.0}},
        "/api/ops/snapshot": {}, "/api/pos/stats": {},
        "/api/approvals": [], "/api/track-b/procurement/warnings": [],
    })
    assert set(card) - {"note", "sim"} <= set(online)
