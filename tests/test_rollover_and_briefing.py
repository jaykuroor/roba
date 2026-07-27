"""Merge, auto-capture, EOD archive, prose briefing, next-day risk (Phase 6).

The sim has no day-rollover hook, so the manager detects the boundary itself by
remembering the last day it saw per instance. That bookkeeping lives in the
Phase 4 SQLite so it survives a restart — otherwise every restart would
re-archive whatever day was already in progress.

Gate:
- a rollover fires **exactly once** per sim-day, and not at all on first sight,
  on a stalled clock, or on a backward jump (a reseed rewinding the clock);
- merging refuses to cross a hole in the audit trail rather than silently
  claiming to cover a window it has no events for, and never rewrites the
  originals;
- a canned briefing is an **error**, never prose;
- next-day coverage risk is the plan's cover lapsing *during tomorrow*, joined
  with tomorrow's forecast — and does not double-count today's stock rows.
"""

import asyncio
import json

import pytest

import manager


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Relocate the whole manager footprint (captures, archives, sqlite)."""
    monkeypatch.setattr(manager, "STATE_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def one_instance(monkeypatch):
    monkeypatch.setattr(manager.registry, "instances", {
        "running_fox": {"id": "running_fox", "title": "Bella's"},
    })


# -- rollover detection (pure) ---------------------------------------------


def test_first_sighting_archives_nothing():
    """The day was already underway — there is no window to snapshot."""
    assert manager.day_rollover(None, 4) is None


def test_a_rollover_reports_the_day_that_ended():
    assert manager.day_rollover(2, 3) == 2


def test_a_stalled_clock_rolls_over_nothing():
    assert manager.day_rollover(3, 3) is None


def test_a_backward_jump_rebases_without_archiving():
    """A reseed rewinds the clock; day 1 is about to be replayed, not archived."""
    assert manager.day_rollover(7, 1) is None


def test_several_days_at_once_report_only_the_most_recent():
    assert manager.day_rollover(2, 5) == 4


# -- rollover bookkeeping (persisted) --------------------------------------


def test_the_last_seen_day_survives_a_manager_restart(state):
    conn = manager.incidents_db()
    try:
        assert manager.last_seen_day(conn, "running_fox") is None
        manager.record_seen_day(conn, "running_fox", 3)
    finally:
        conn.close()

    # A "restart" is just a fresh connection to the same file on disk.
    conn = manager.incidents_db()
    try:
        assert manager.last_seen_day(conn, "running_fox") == 3
    finally:
        conn.close()


def _health_at(day):
    async def fake_get(_inst, path):
        if path.startswith("/api/health"):
            return {"ok": True, "sim": {"sim_time": day * 86400.0 + 100.0,
                                        "day_number": day}}
        if path.startswith("/api/events"):
            return []
        return None
    return fake_get


def _stub_archive(monkeypatch, calls):
    async def fake_archive(instance_id, day):
        calls.append((instance_id, day))
        return {"day": day}
    monkeypatch.setattr(manager, "archive_summary", fake_archive)


def test_a_rollover_fires_exactly_once_per_sim_day(state, one_instance, monkeypatch):
    archived = []
    _stub_archive(monkeypatch, archived)

    monkeypatch.setattr(manager, "_get", _health_at(2))
    assert asyncio.run(manager.check_rollovers()) == []      # first sight
    assert asyncio.run(manager.check_rollovers()) == []      # same day again

    monkeypatch.setattr(manager, "_get", _health_at(3))
    fired = asyncio.run(manager.check_rollovers())
    assert [d["day"] for d in fired] == [2]

    # Polling again inside the same sim-day must not archive day 2 twice.
    assert asyncio.run(manager.check_rollovers()) == []
    assert archived == [("running_fox", 2)]


def test_a_rollover_captures_events_as_well_as_archiving(state, one_instance,
                                                        monkeypatch):
    """The "done when": a capture and an archive, nobody pressing a button."""
    _stub_archive(monkeypatch, [])
    monkeypatch.setattr(manager, "_get", _health_at(1))
    asyncio.run(manager.check_rollovers())

    monkeypatch.setattr(manager, "_get", _health_at(2))
    fired = asyncio.run(manager.check_rollovers())

    assert fired[0]["capture"]["n"] == 1
    assert (manager._catchup_dir("running_fox") / "000001.json").exists()


def test_an_offline_child_is_skipped_not_rolled_over(state, one_instance,
                                                     monkeypatch):
    async def offline(_inst, _path):
        return None

    monkeypatch.setattr(manager, "_get", offline)
    assert asyncio.run(manager.check_rollovers()) == []


# -- the end-of-day archive ------------------------------------------------


def test_the_archive_round_trips_through_its_endpoints(state, one_instance,
                                                       monkeypatch):
    async def fake_summary():
        return {"generated_at": 1.0, "totals": {"sales_today": 42.0},
                "restaurants": [], "major_incidents": [],
                "pending_decisions": [], "next_day_risks": []}

    monkeypatch.setattr(manager, "daily_summary", fake_summary)
    asyncio.run(manager.archive_summary("running_fox", 2))

    assert manager.list_summaries("running_fox") == [
        {"day": 2, "created_at": pytest.approx(manager.time.time(), abs=60)},
    ]
    archived = manager.get_summary_archive("running_fox", 2)
    assert archived["day"] == 2
    assert archived["instance_id"] == "running_fox"
    assert archived["summary"]["totals"]["sales_today"] == 42.0


def test_archives_list_newest_day_first(state, one_instance, monkeypatch):
    async def fake_summary():
        return {"totals": {}}

    monkeypatch.setattr(manager, "daily_summary", fake_summary)
    for day in (1, 3, 2):
        asyncio.run(manager.archive_summary("running_fox", day))

    assert [r["day"] for r in manager.list_summaries("running_fox")] == [3, 2, 1]


def test_a_missing_archived_day_is_a_404(state, one_instance):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        manager.get_summary_archive("running_fox", 9)
    assert exc.value.status_code == 404


# -- merge -----------------------------------------------------------------


def _capture(n, since, until, events):
    return {"n": n, "instance_id": "running_fox", "created_at": 1.0,
            "since_sim": since, "until_sim": until,
            "event_count": len(events), "events": events, "summary": None}


def _write(record):
    path = (manager._catchup_dir(record["instance_id"])
            / f"{record['n']:06d}.json")
    path.write_text(json.dumps(record))
    return path


def _event(id_, category="po_placed"):
    return {"id": id_, "sim_time": 100.0 * id_, "category": category,
            "actor": "procurement", "summary": f"event {id_}", "detail": {}}


def _bullets(text="merged"):
    def complete(**_kwargs):
        return {"bullets": [{"text": text, "event_ids": []}]}
    return complete


def test_merge_gap_accepts_a_contiguous_run():
    assert manager.merge_gap([
        _capture(1, 0.0, 100.0, []), _capture(2, 100.0, 200.0, []),
    ]) is None


def test_merge_gap_names_the_discontinuity():
    gap = manager.merge_gap([
        _capture(1, 0.0, 100.0, []), _capture(3, 500.0, 600.0, []),
    ])

    assert gap is not None
    assert "#1" in gap and "#3" in gap


def test_merging_concatenates_windows_and_keeps_the_originals(
    state, one_instance, monkeypatch
):
    monkeypatch.setattr(
        manager, "_summarizer",
        lambda: (type("P", (), {"complete": staticmethod(_bullets())})(), "m"),
    )
    paths = [
        _write(_capture(1, 0.0, 100.0, [_event(1)])),
        _write(_capture(2, 100.0, 200.0, [_event(2), _event(3)])),
    ]
    before = [p.read_text() for p in paths]

    merged = manager.merge_catchups("running_fox", manager.MergeBody(**{"from": 1, "to": 2}))

    assert merged["since_sim"] == 0.0 and merged["until_sim"] == 200.0
    assert merged["event_count"] == 3
    assert [e["id"] for e in merged["events"]] == [1, 2, 3]
    assert merged["summary"]["buckets"][0]["bullets"][0]["text"] == "merged"
    # The audit trail is untouched — a merge is a lens, not a rewrite.
    assert [p.read_text() for p in paths] == before


def test_merging_refuses_to_cross_a_missing_capture(state, one_instance):
    from fastapi import HTTPException

    _write(_capture(1, 0.0, 100.0, [_event(1)]))
    _write(_capture(3, 200.0, 300.0, [_event(3)]))

    with pytest.raises(HTTPException) as exc:
        manager.merge_catchups("running_fox", manager.MergeBody(**{"from": 1, "to": 3}))

    assert exc.value.status_code == 400
    assert "#2" in exc.value.detail


def test_merging_refuses_a_non_contiguous_pair(state, one_instance):
    from fastapi import HTTPException

    _write(_capture(1, 0.0, 100.0, []))
    _write(_capture(2, 500.0, 600.0, []))  # a hole a deleted file left behind

    with pytest.raises(HTTPException) as exc:
        manager.merge_catchups("running_fox", manager.MergeBody(**{"from": 1, "to": 2}))

    assert exc.value.status_code == 400
    assert "contiguous" in exc.value.detail


def test_merging_an_inverted_range_is_rejected(state, one_instance):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        manager.merge_catchups("running_fox", manager.MergeBody(**{"from": 3, "to": 1}))
    assert exc.value.status_code == 400


# -- the prose briefing ----------------------------------------------------


_SUMMARY = {
    "totals": {"sales_today": 900.0, "waste_today": 12.0},
    "restaurants": [{"title": "Bella's", "status": "critical", "sales_today": 900.0}],
    "major_incidents": [{"restaurant": "Bella's", "problem": "Walk-in at 9C",
                         "impact": "HACCP"}],
    "pending_decisions": [],
    "next_day_risks": [],
}


def test_a_canned_briefing_is_an_error_not_prose():
    summary = manager.write_briefing(
        _SUMMARY,
        complete=lambda **_k: {"result": "no_change", "note": manager.CANNED_NOTE},
        model="gemini-2.5-nonsense",
    )

    assert summary["prose"] == ""
    assert summary["error"] and "gemini-2.5-nonsense" in summary["error"]


def test_an_empty_briefing_is_also_an_error():
    """A blank string rendered as a briefing reads as "all quiet". It is not."""
    for bad in ({"briefing": "   "}, {"briefing": None}, {}, "nope"):
        result = manager.write_briefing(
            _SUMMARY, complete=lambda _bad=bad, **_k: _bad, model="m",
        )
        assert result["error"], bad
        assert result["prose"] == ""


def test_a_real_briefing_comes_back_as_prose():
    result = manager.write_briefing(
        _SUMMARY,
        complete=lambda **_k: {"briefing": "  Bella's is critical.\nWaste EUR 12.  "},
        model="m",
    )

    assert result["error"] is None
    assert result["prose"] == "Bella's is critical.\nWaste EUR 12."


def test_the_briefing_prompt_carries_the_headlines_not_the_whole_cards():
    context = manager.briefing_context({
        **_SUMMARY,
        "restaurants": [{"title": "Bella's", "status": "critical",
                         "snapshot": {"huge": ["nested", "payload"]}}],
    })

    assert context["major_incidents"] == ["Bella's: Walk-in at 9C — HACCP"]
    assert "snapshot" not in json.dumps(context)


# -- next-day coverage risk ------------------------------------------------


def _plan_item(name, covers_until, order_date=None):
    return {"ingredient_id": 1, "ingredient_name": name,
            "covers_until": covers_until, "order_date": order_date}


def _horizon(day, qty, generated_at=1.0):
    return {"generated_at": generated_at, "breakdown": {"by_day": [
        {"day_index": 0, "start": day * 86400.0, "end": (day + 1) * 86400.0,
         "qty": qty},
    ]}}


def _risks(items, horizons=(), sim_time=86400.0 * 2 + 3600):
    return manager.next_day_coverage_risks(
        items, list(horizons), sim_time,
        instance_id="running_fox", restaurant="Bella's",
    )


def test_cover_lapsing_during_tomorrow_is_a_risk():
    risks = _risks([_plan_item("Tomato", 86400.0 * 3 + 50400)],
                   [_horizon(3, 118)])

    assert len(risks) == 1
    assert "Tomato" in risks[0]["problem"]
    assert "Day 3 14:00" in risks[0]["problem"]
    assert risks[0]["impact"] == "Tomorrow forecasts 118 covers."
    assert risks[0]["kind"] == "coverage"


def test_cover_lasting_past_tomorrow_is_not_a_risk():
    assert _risks([_plan_item("Basil", 86400.0 * 9)], [_horizon(3, 100)]) == []


def test_cover_that_already_lapsed_is_left_to_the_stock_rows():
    """Today's problem, already raised as low stock — do not double-count it."""
    assert _risks([_plan_item("Basil", 86400.0 * 2 + 60)], [_horizon(3, 100)]) == []


def test_a_missing_forecast_still_reports_the_coverage_gap():
    risks = _risks([_plan_item("Tomato", 86400.0 * 3 + 3600)], [])

    assert len(risks) == 1
    assert "No forecast" in risks[0]["impact"]


def test_the_newest_horizon_covering_tomorrow_wins():
    risks = _risks(
        [_plan_item("Tomato", 86400.0 * 3 + 3600)],
        [_horizon(3, 50, generated_at=1.0), _horizon(3, 200, generated_at=99.0)],
    )

    assert "200 covers" in risks[0]["impact"]


def test_a_horizon_that_does_not_reach_tomorrow_is_ignored():
    risks = _risks([_plan_item("Tomato", 86400.0 * 3 + 3600)], [_horizon(2, 77)])

    assert "No forecast" in risks[0]["impact"]


def test_risks_are_ordered_by_deadline():
    risks = _risks([
        _plan_item("Late", 86400.0 * 3 + 70000),
        _plan_item("Early", 86400.0 * 3 + 3600),
    ], [_horizon(3, 10)])

    assert [r["problem"].split()[0] for r in risks] == ["Early", "Late"]
