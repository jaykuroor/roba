from core.models import Review, ReviewInsight
from core.signals import SignalType
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
