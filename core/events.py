"""Event-log helper — the narrative spine (§19.4).

``event_log`` is the activity-log / narrative feed every component appends to so
the demo dashboard can show a human-readable timeline of what happened and why.
This module is the single tiny seam used by ``core`` (and the API layer) to
write one such row; agents have their own ``BaseAgent.log_event`` that writes
the same table stamped with the agent name.
"""

from __future__ import annotations

from typing import Any, Optional

from .models import EventLog


def log_event(
    db: Any,
    sim_time: float,
    category: str,
    actor: str,
    summary: str,
    detail: Optional[dict] = None,
) -> EventLog:
    """Append one ``event_log`` row using the supplied session and return it.

    ``detail`` defaults to an empty dict so the JSON column is never ``NULL``.
    The caller owns the session lifecycle; this helper commits and refreshes so
    the returned row carries its assigned ``id``.
    """
    row = EventLog(
        sim_time=sim_time,
        category=category,
        actor=actor,
        summary=summary,
        detail=detail or {},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
