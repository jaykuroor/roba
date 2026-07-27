"""Catch-up summarizer (Phase 5, docs/fable/catchup.md).

A capture is a frozen window of the child's event log. The summarizer buckets
it deterministically by subsystem, writes one prompt per bucket, and stores
bullets that point back at the event ids they came from — that is what makes
"click to expand" resolve.

Gate:
- bucketing is deterministic and total (an unknown category is never dropped);
- a canned / malformed provider answer yields an **error**, never prose — the
  trap this project has fallen into twice (docs/fable/progress.md §4 LLM);
- hallucinated event ids are filtered, so an expander never resolves to nothing;
- the endpoint writes the summary into the capture file, idempotently, and
  joins the incidents opened inside that window.
"""

import json

import pytest

import manager


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Relocate the whole manager footprint (captures + incident DB)."""
    monkeypatch.setattr(manager, "STATE_DIR", tmp_path)
    return tmp_path


def _event(id_, category, summary="something happened", sim_time=100.0):
    return {"id": id_, "sim_time": sim_time, "category": category,
            "actor": "optimizer", "summary": summary, "detail": {"x": 1}}


def _capture(events, n=1, instance_id="running_fox", since=0.0, until=86400.0):
    return {"n": n, "instance_id": instance_id, "created_at": 1.0,
            "since_sim": since, "until_sim": until,
            "event_count": len(events), "events": events, "summary": None}


def _answering(bullets_by_bucket=None, default=None):
    """A fake ``LLMProvider.complete`` recording the prompts it was given."""
    calls = []

    def complete(*, messages, json_schema, max_tokens, use_site, model):
        calls.append({"messages": messages, "model": model,
                      "max_tokens": max_tokens, "use_site": use_site})
        # The bucket name is in the system prompt; cheap way to vary answers.
        system = messages[0]["content"]
        for bucket, answer in (bullets_by_bucket or {}).items():
            if f"Summarize the {bucket} activity" in system:
                return answer
        return default if default is not None else {"bullets": []}

    complete.calls = calls
    return complete


# -- bucketing (pure) ------------------------------------------------------


def test_bucketing_is_deterministic_and_ordered():
    events = [
        _event(1, "attendance"),
        _event(2, "po_placed"),
        _event(3, "waste"),
        _event(4, "po_delivered"),
    ]

    first = manager.bucket_events(events)
    second = manager.bucket_events(list(reversed(events)))

    # Buckets come out in BUCKET_ORDER regardless of arrival order.
    assert list(first) == ["procurement", "inventory", "staffing"]
    assert list(second) == ["procurement", "inventory", "staffing"]
    assert [e["id"] for e in first["procurement"]] == [2, 4]


def test_an_unknown_category_lands_in_other_rather_than_vanishing():
    grouped = manager.bucket_events([_event(1, "brand_new_thing"), _event(2, "scenario")])

    assert list(grouped) == ["other"]
    assert [e["id"] for e in grouped["other"]] == [1, 2]


def test_every_mapped_bucket_is_renderable():
    """A bucket the map can produce but BUCKET_ORDER omits would be dropped."""
    assert set(manager.EVENT_BUCKETS.values()) <= set(manager.BUCKET_ORDER)
    assert set(manager.BUCKET_ORDER) <= set(manager._BUCKET_BRIEF)


def test_empty_buckets_cost_nothing():
    assert manager.bucket_events([]) == {}


# -- the canned-fallback guard ---------------------------------------------


def test_canned_note_matches_core():
    """The mirrored literal must not drift from the real marker."""
    from core.llm import CANNED_NOTE

    assert manager.CANNED_NOTE == CANNED_NOTE


def test_a_canned_answer_is_an_error_not_a_summary():
    canned = {"result": "no_change", "note": manager.CANNED_NOTE}

    summary = manager.summarize_capture(
        _capture([_event(1, "po_placed")]),
        complete=_answering(default=canned),
        model="gemini-2.5-nonsense",
    )

    assert summary["buckets"] == []
    assert summary["error"]
    assert "gemini-2.5-nonsense" in summary["error"]
    # Nothing that could be mistaken for prose about the restaurant.
    assert "no_change" not in json.dumps(summary)


def test_a_malformed_answer_is_also_an_error():
    for bad in ({"bullets": "not a list"}, "just a string", {}):
        summary = manager.summarize_capture(
            _capture([_event(1, "po_placed")]),
            complete=_answering(default=bad),
            model="m",
        )
        assert summary["error"], bad
        assert summary["buckets"] == []


def test_one_bad_bucket_fails_the_whole_summary():
    """A half-summary reads as "nothing else happened" — worse than an error."""
    complete = _answering(
        {"procurement": {"bullets": [{"text": "3 POs placed.", "event_ids": [1]}]}},
        default={"note": manager.CANNED_NOTE},
    )

    summary = manager.summarize_capture(
        _capture([_event(1, "po_placed"), _event(2, "attendance")]),
        complete=complete, model="m",
    )

    assert summary["error"]
    assert summary["buckets"] == []


# -- the happy path --------------------------------------------------------


def test_bullets_keep_only_event_ids_that_are_really_in_the_bucket():
    complete = _answering({"procurement": {"bullets": [
        {"text": "Two POs placed.", "event_ids": [1, "2", 999, None]},
    ]}})

    summary = manager.summarize_capture(
        _capture([_event(1, "po_placed"), _event(2, "po_delivered")]),
        complete=complete, model="m",
    )

    assert summary["error"] is None
    # 999 was never in the prompt; "2" is the same event quoted as a string.
    assert summary["buckets"][0]["bullets"][0]["event_ids"] == [1, 2]


def test_one_prompt_per_non_empty_bucket_carrying_no_event_detail():
    complete = _answering()

    manager.summarize_capture(
        _capture([_event(1, "po_placed"), _event(2, "attendance")]),
        complete=complete, model="gemini-2.5-pro",
    )

    assert len(complete.calls) == 2
    payload = complete.calls[0]["messages"][1]["content"]
    assert '"id": 1' in payload
    assert "detail" not in payload  # kilobytes of solver output, never prompted
    assert complete.calls[0]["model"] == "gemini-2.5-pro"


def test_a_bucket_over_the_cap_reports_what_it_dropped(monkeypatch):
    monkeypatch.setattr(manager, "CATCHUP_MAX_EVENTS_PER_BUCKET", 2)
    complete = _answering()

    summary = manager.summarize_capture(
        _capture([_event(i, "po_placed") for i in range(1, 6)]),
        complete=complete, model="m",
    )

    bucket = summary["buckets"][0]
    assert bucket["event_count"] == 5
    assert bucket["truncated"] == 3
    # The most recent survive — the newest events are the ones still actionable.
    assert '"id": 4' in complete.calls[0]["messages"][1]["content"]
    assert '"id": 1' not in complete.calls[0]["messages"][1]["content"]


def test_empty_bullets_are_a_valid_answer():
    """"Nothing worth reporting" is a summary; it must not read as failure."""
    summary = manager.summarize_capture(
        _capture([_event(1, "po_placed")]),
        complete=_answering(default={"bullets": []}), model="m",
    )

    assert summary["error"] is None
    assert summary["buckets"][0]["bullets"] == []


# -- incidents opened in the window ----------------------------------------


def _open_incident(instance_id, summary, opened_at):
    conn = manager.incidents_db()
    try:
        conn.execute(
            "INSERT INTO incidents (instance_id, category, summary, opened_at, status)"
            " VALUES (?, 'food_safety_checks', ?, ?, 'open')",
            (instance_id, summary, opened_at),
        )
        conn.commit()
    finally:
        conn.close()


def test_incidents_in_window_reads_the_store_not_live_signals(state):
    _open_incident("running_fox", "Before the window.", 50.0)
    _open_incident("running_fox", "Inside the window.", 500.0)
    _open_incident("running_fox", "After the window.", 5000.0)
    _open_incident("quiet_otter", "Another restaurant.", 500.0)

    rows = manager.incidents_in_window("running_fox", 100.0, 1000.0)

    assert [r["summary"] for r in rows] == ["Inside the window."]


def test_a_resolved_incident_still_shows_in_its_window(state):
    """The whole point of reading the store: the signal is long gone."""
    _open_incident("running_fox", "Walk-in fridge at 9C.", 500.0)
    conn = manager.incidents_db()
    try:
        conn.execute("UPDATE incidents SET status='resolved', resolved_at=1.0")
        conn.commit()
    finally:
        conn.close()

    rows = manager.incidents_in_window("running_fox", 100.0, 1000.0)

    assert len(rows) == 1
    assert rows[0]["status"] == "resolved"


def test_the_first_capture_includes_an_incident_opened_at_sim_zero(state):
    _open_incident("running_fox", "Opened at the very start.", 0.0)

    assert manager.incidents_in_window("running_fox", 0.0, 100.0) != []


# -- the endpoint ----------------------------------------------------------


@pytest.fixture
def one_instance(monkeypatch):
    monkeypatch.setattr(manager.registry, "instances", {
        "running_fox": {"id": "running_fox", "title": "Bella's"},
    })


def _write_capture(record):
    path = manager._catchup_dir(record["instance_id"]) / f"{record['n']:06d}.json"
    path.write_text(json.dumps(record))
    return path


def test_summarize_writes_into_the_capture_file(state, one_instance, monkeypatch):
    monkeypatch.setattr(
        manager, "_summarizer",
        lambda: (type("P", (), {"complete": staticmethod(_answering(
            {"procurement": {"bullets": [{"text": "3 POs placed.", "event_ids": [1]}]}}
        ))})(), "gemini-2.5-pro"),
    )
    _open_incident("running_fox", "Fridge failed.", 500.0)
    path = _write_capture(_capture([_event(1, "po_placed")], since=0.0, until=86400.0))

    returned = manager.summarize_catchup("running_fox", 1)

    on_disk = json.loads(path.read_text())
    assert on_disk["summary"] == returned
    assert on_disk["summary"]["buckets"][0]["bullets"][0]["text"] == "3 POs placed."
    assert [i["summary"] for i in on_disk["summary"]["incidents"]] == ["Fridge failed."]
    # The raw events survive, so a re-summarize with a better prompt is possible.
    assert on_disk["event_count"] == 1 and len(on_disk["events"]) == 1


def test_resummarizing_overwrites_and_keeps_the_events(state, one_instance, monkeypatch):
    _write_capture(_capture([_event(1, "po_placed")]))

    for text in ("first pass", "second pass"):
        monkeypatch.setattr(
            manager, "_summarizer",
            lambda text=text: (type("P", (), {"complete": staticmethod(_answering(
                default={"bullets": [{"text": text, "event_ids": []}]}
            ))})(), "m"),
        )
        summary = manager.summarize_catchup("running_fox", 1)

    assert summary["buckets"][0]["bullets"][0]["text"] == "second pass"
    assert len(json.loads(
        (manager._catchup_dir("running_fox") / "000001.json").read_text()
    )["events"]) == 1


def test_summarizing_a_missing_capture_is_a_404(state, one_instance):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        manager.summarize_catchup("running_fox", 42)
    assert exc.value.status_code == 404


def test_capture_files_follow_a_relocated_state_dir(state, one_instance):
    """Regression: CATCHUP_DIR used to freeze the path at import time."""
    assert manager._catchup_dir("running_fox").is_relative_to(state)
