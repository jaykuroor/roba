"""Pure-logic tests for the multi-restaurant manager (manager.py).

No processes are spawned — these cover the derivation logic the dashboard
depends on: instance ids, status rules, issue building, and ranking.
"""
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
