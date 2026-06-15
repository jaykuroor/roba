from core.models import Review, ReviewInsight
from core.signals import SignalType
from track_a.agents.review import ReviewAgent


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
