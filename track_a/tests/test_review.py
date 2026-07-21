from core.models import Review, ReviewInsight
from core.signals import SignalType
from track_a.agents import review as review_mod
from track_a.agents.review import ReviewAgent


class FakeReviewLLM:
    def complete(self, messages, json_schema=None, max_tokens=800, use_site=""):
        return {
            "severity": "medium",
            "summary": "LLM spotted dry chicken complaints.",
            "suggested_action": "Check holding time for chicken.",
            "dish_mentions": ["Crispy Chicken Burger"],
            "sentiment": "negative",
        }


def test_review_canned_path_dedups_and_sets_trend_severity(bus, session_factory, seeded):
    session = session_factory()
    try:
        for idx in range(3):
            session.add(Review(source="test", rating=1.0, text="cold soggy pizza", dish_mentions=["Margherita Pizza"], sentiment="negative", sim_time=idx, processed=0))
        session.commit()
    finally:
        session.close()

    agent = ReviewAgent(bus, session_factory)
    rows = agent.process_unprocessed()
    assert len(rows) == 3

    session = session_factory()
    try:
        assert session.query(ReviewInsight).count() == 3
        assert session.query(ReviewInsight).order_by(ReviewInsight.id.desc()).first().severity == "high"
    finally:
        session.close()

    signals = bus.live(type=SignalType.REVIEW_INSIGHT)
    assert len(signals) == 1
    assert signals[0].dedup_key == "review:Margherita Pizza"


def test_review_uses_llm_when_available(bus, session_factory, seeded):
    session = session_factory()
    try:
        session.query(Review).update({Review.processed: 1})
        session.add(Review(source="test", rating=3.0, text="chicken was dry", dish_mentions=[], sentiment="neutral", sim_time=1, processed=0))
        session.commit()
    finally:
        session.close()

    agent = ReviewAgent(bus, session_factory, FakeReviewLLM())
    rows = agent.process_unprocessed()
    assert len(rows) == 1

    session = session_factory()
    try:
        insight = session.query(ReviewInsight).one()
        review = session.query(Review).one()
        assert insight.summary == "LLM spotted dry chicken complaints."
        assert review.sentiment == "negative"
        assert review.dish_mentions == ["Crispy Chicken Burger"]
    finally:
        session.close()


def test_review_holds_no_db_session_during_llm(bus, session_factory, seeded):
    """The LLM analysis must run with NO write transaction open — otherwise the
    sqlite write lock is pinned across the (slow) call and the orchestrator tick
    can't commit sim_time ('database is locked', frozen clock at ~08:15)."""
    session = session_factory()
    try:
        session.query(Review).update({Review.processed: 1})
        session.add(Review(source="test", rating=2.0, text="salty pasta",
                           dish_mentions=[], sentiment="neutral", sim_time=1, processed=0))
        session.commit()
    finally:
        session.close()

    committed_during_llm = {"ok": False}

    class LockProbeLLM:
        def __init__(self, sf):
            self.sf = sf

        def complete(self, messages, json_schema=None, max_tokens=800, use_site=""):
            # A concurrent short write must succeed while the LLM is "thinking":
            # proves process_unprocessed isn't holding the write lock here.
            s = self.sf()
            try:
                r = s.query(Review).order_by(Review.id.desc()).first()
                r.rating = 2.5
                s.commit()
                committed_during_llm["ok"] = True
            finally:
                s.close()
            return {
                "severity": "low", "summary": "probe", "suggested_action": "none",
                "dish_mentions": [], "sentiment": "neutral",
            }

    agent = ReviewAgent(bus, session_factory, LockProbeLLM(session_factory))
    agent.process_unprocessed()
    assert committed_during_llm["ok"], "write lock was held across the LLM call"


def test_review_caps_llm_analyses_per_scan(bus, session_factory, seeded, monkeypatch):
    """A seeded backlog must not drive an unbounded number of LLM calls in one
    scan (which would pin the clock at realtime for minutes)."""
    monkeypatch.setattr(review_mod, "_LLM_BATCH_MAX", 3)
    session = session_factory()
    try:
        session.query(Review).update({Review.processed: 1})
        for idx in range(10):
            session.add(Review(source="test", rating=1.0, text="bad",
                               dish_mentions=[], sentiment="negative",
                               sim_time=idx, processed=0))
        session.commit()
    finally:
        session.close()

    calls = {"n": 0}

    class CountingLLM:
        def complete(self, messages, json_schema=None, max_tokens=800, use_site=""):
            calls["n"] += 1
            return {"severity": "low", "summary": "x", "suggested_action": "y",
                    "dish_mentions": [], "sentiment": "negative"}

    agent = ReviewAgent(bus, session_factory, CountingLLM())
    rows = agent.process_unprocessed()
    assert calls["n"] == 3 and len(rows) == 3, (calls["n"], len(rows))

    # Remaining reviews drain on the next scan.
    agent.process_unprocessed()
    assert calls["n"] == 6
