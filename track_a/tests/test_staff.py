from core.models import Attendance
from core.signals import SignalType
from track_a.agents.staff import StaffAgent


def test_staff_coverage_blocks_and_restores(bus, session_factory, seeded):
    agent = StaffAgent(bus, session_factory)
    initial = agent.recompute()
    assert any(row["station_id"] == 1 and row["covered"] for row in initial)

    result = agent.call_in_sick(staff_id=1, status="sick")
    assert result["staff_id"] == 1
    uncovered = [s for s in bus.live(type=SignalType.STAFF_COVERAGE) if (s.payload or {}).get("station_id") == 1][0]
    assert uncovered.payload["covered"] is False
    assert uncovered.payload["affected_items"] == [1]

    agent.call_in_sick(staff_id=1, status="present", reason="back on station")
    restored = [s for s in bus.live(type=SignalType.STAFF_COVERAGE) if (s.payload or {}).get("station_id") == 1][0]
    assert restored.payload["covered"] is True

    session = session_factory()
    try:
        assert session.query(Attendance).count() == 2
    finally:
        session.close()
