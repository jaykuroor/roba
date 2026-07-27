"""Multi-restaurant manager server (docs/fable/manager-dashboard.md).

Runs a small FastAPI app (default port 8100) that:

- spawns one ``uvicorn core.api:app`` subprocess per restaurant instance,
  each with its own SQLite DB (``dbdata/<instance_id>.db``), seeded from a
  preset in ``data/``;
- keeps a JSON registry (``dbdata/manager_registry.json``) so instances
  survive a manager restart (their DBs persist; "start" respawns them);
- reverse-proxies ``/i/<instance_id>/api/*`` (HTTP) and
  ``/i/<instance_id>/ws*`` (WebSocket, incl. voice) to the child instance —
  the unified frontend talks only to this server for instance traffic;
- serves the ``/admin/api/*`` aggregation endpoints that power the manager
  dashboard: portfolio overview, priority action queue, combined approvals,
  incidents, daily summary, and catch-ups — captured event windows plus the
  per-subsystem LLM summary written over them (docs/fable/catchup.md).

Run:  uvicorn manager:app --port 8100
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sqlite3
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from pydantic import BaseModel, Field
from websockets.asyncio.client import connect as ws_connect

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
STATE_DIR = Path(os.getenv("MANAGER_STATE_DIR", str(ROOT / "dbdata")))
REGISTRY_PATH = STATE_DIR / "manager_registry.json"

# Child probe timeout (fan-out reads). Spawning waits longer (STARTUP_TIMEOUT_S).
# Generous because a child's event loop can stall for seconds during MILP
# solves / LLM calls at high sim speed — a slow child is not an offline child.
PROBE_TIMEOUT_S = 10.0
STARTUP_TIMEOUT_S = 90.0
APPROVAL_TTL_SIM_S = 21600.0  # mirrors core.approvals.APPROVAL_TTL_SIM_S
SECONDS_PER_DAY = 86400.0
# Kitchen ticket backlog thresholds — mirrors core.config.BACKLOG_WARN /
# BACKLOG_CRIT (same env vars, so overriding one overrides both; the manager
# deliberately imports no core module).
BACKLOG_WARN = int(os.getenv("BACKLOG_WARN", "8"))
BACKLOG_CRIT = int(os.getenv("BACKLOG_CRIT", "20"))

# ---------------------------------------------------------------------------
# Instance ids — memorable adjective_animal names (running_fox style)
# ---------------------------------------------------------------------------

ADJECTIVES = [
    "running", "quiet", "brave", "clever", "sunny", "rapid", "gentle", "bold",
    "lucky", "calm", "fiery", "swift", "merry", "wild", "noble", "dusty",
]
ANIMALS = [
    "fox", "otter", "heron", "badger", "lynx", "wolf", "falcon", "ibis",
    "marmot", "tiger", "crane", "panda", "raven", "seal", "bison", "hare",
]


def generate_instance_id(taken: set) -> str:
    for _ in range(64):
        candidate = f"{random.choice(ADJECTIVES)}_{random.choice(ANIMALS)}"
        if candidate not in taken:
            return candidate
    # 256 combos exhausted (or very unlucky): suffix a number.
    base = f"{random.choice(ADJECTIVES)}_{random.choice(ANIMALS)}"
    n = 2
    while f"{base}{n}" in taken:
        n += 1
    return f"{base}{n}"


# ---------------------------------------------------------------------------
# Pure derivation logic (unit-tested in tests/test_manager.py)
# ---------------------------------------------------------------------------

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def sim_label(sim_seconds: Optional[float]) -> str:
    """``"Day N HH:MM"`` — mirrors the frontend's ``fmtSim`` so a sentence the
    manager writes server-side reads the same as a timestamp the UI renders."""
    if sim_seconds is None:
        return "an unknown time"
    total = int(sim_seconds)
    return (f"Day {total // 86400} {total % 86400 // 3600:02d}"
            f":{total % 3600 // 60:02d}")

# ApprovalRequest.urgency is a free string; observed values include
# normal / high plus the optimizer's at_risk / uncoverable labels.
URGENCY_TO_SEVERITY = {
    "critical": "critical",
    "uncoverable": "critical",
    "high": "high",
    "at_risk": "high",
    "normal": "medium",
}


def derive_status(
    *,
    online: bool,
    snapshot: Optional[Dict[str, Any]],
    warnings: List[Dict[str, Any]],
    pending_approvals: List[Dict[str, Any]],
) -> str:
    """Normal | warning | critical | offline for a restaurant card."""
    if not online:
        return "offline"
    snapshot = snapshot or {}
    low_stock = snapshot.get("low_stock_ingredients") or []
    stations = snapshot.get("stations") or []
    staff = snapshot.get("staff") or []
    backlog = snapshot.get("queued_count") or 0
    if warnings:  # INGREDIENT_UNCOVERABLE — nothing can source the demand
        return "critical"
    if snapshot.get("safety_issues"):  # a failed temp/safety check is never "a look later"
        return "critical"
    if backlog >= BACKLOG_CRIT:  # the pass is drowning — guests are waiting
        return "critical"
    if any(s.get("status") == "depleted" for s in low_stock):
        return "critical"
    if any(not s.get("covered", True) for s in stations):
        return "critical"
    if any(
        URGENCY_TO_SEVERITY.get(str(a.get("urgency")), "medium") == "critical"
        for a in pending_approvals
    ):
        return "critical"
    if low_stock or pending_approvals:
        return "warning"
    if backlog >= BACKLOG_WARN:
        return "warning"
    if any(s.get("status") != "present" for s in staff):
        return "warning"
    return "normal"


def revenue_at_stake(
    snapshot: Optional[Dict[str, Any]], dish_names: Optional[List[str]]
) -> Optional[float]:
    """Forecast revenue riding on ``dish_names`` today, or None if unknowable.

    ``revenue_estimate`` (forecast qty × price) is already per-dish on the
    snapshot, so pricing a blocked station is a lookup, not a model. Returns
    None rather than 0.0 when nothing can be attributed — an unpriced row shows
    no chip, which is honest; €0.00 would read as "this costs nothing".
    """
    if not dish_names:
        return None
    by_dish = {
        str(d.get("name")): float(d.get("revenue_estimate") or 0.0)
        for d in (snapshot or {}).get("dishes") or []
    }
    known = [by_dish[n] for n in dish_names if n in by_dish]
    return round(sum(known), 2) if known else None


def stock_deadlines(plan_items: Optional[List[Dict[str, Any]]]) -> Dict[str, float]:
    """``{ingredient_name: order-by sim-time}`` from the procurement plan.

    ``order_date`` is the actionable deadline (place it by then);
    ``latest_safe_arrival`` is the fallback. Earliest wins per ingredient — the
    plan can carry several orders for one ingredient and the manager needs the
    next one.
    """
    deadlines: Dict[str, float] = {}
    for item in plan_items or []:
        name = item.get("ingredient_name")
        due = item.get("order_date")
        if due is None:
            due = item.get("latest_safe_arrival")
        if not name or due is None:
            continue
        try:
            due = float(due)
        except (TypeError, ValueError):
            continue
        if name not in deadlines or due < deadlines[name]:
            deadlines[name] = due
    return deadlines


def build_issues(
    instance_id: str,
    title: str,
    *,
    snapshot: Optional[Dict[str, Any]],
    warnings: List[Dict[str, Any]],
    pending_approvals: List[Dict[str, Any]],
    plan_items: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Priority-action-queue entries for one restaurant (unranked)."""
    issues: List[Dict[str, Any]] = []
    snapshot = snapshot or {}
    deadlines = stock_deadlines(plan_items)

    for a in pending_approvals:
        severity = URGENCY_TO_SEVERITY.get(str(a.get("urgency")), "medium")
        created = a.get("created_at")
        # "notice" = acknowledge-only (no reactor acts on a decision);
        # "decision" = approve/reject drives a real action. The child tags
        # each row (core.approvals.kind_for).
        approval_kind = a.get("kind") or "decision"
        issues.append({
            "instance_id": instance_id,
            "restaurant": title,
            "kind": "approval",
            "approval_kind": approval_kind,
            "problem": a.get("title") or a.get("type") or "Approval pending",
            "severity": severity,
            "deadline_sim": (created + APPROVAL_TTL_SIM_S) if isinstance(created, (int, float)) else None,
            "impact": a.get("summary") or "",
            "recommended_action": (
                "Acknowledge — informational; nothing is gated on a decision."
                if approval_kind == "notice"
                else "Approve if the numbers look right — Roba proposed this."
            ),
            "impact_eur": None,
            "approval_id": a.get("id"),
        })

    for w in warnings:
        issues.append({
            "instance_id": instance_id,
            "restaurant": title,
            "kind": "stock",
            "problem": f"Cannot source {w.get('ingredient_name') or 'ingredient'} "
                       f"(short {w.get('short_qty', 0):g} {w.get('unit', '')})".strip(),
            "severity": "critical",
            "deadline_sim": deadlines.get(w.get("ingredient_name")),
            "impact": w.get("reason") or "Dishes using this ingredient will 86.",
            "recommended_action": "Open the restaurant → Procurement; onboard or call a supplier.",
            "impact_eur": None,
            "approval_id": None,
        })

    for s in snapshot.get("low_stock_ingredients") or []:
        depleted = s.get("status") == "depleted"
        issues.append({
            "instance_id": instance_id,
            "restaurant": title,
            "kind": "stock",
            "problem": f"{s.get('ingredient', '?')} {'depleted' if depleted else 'below safety stock'} "
                       f"({s.get('on_hand_display', s.get('on_hand'))})",
            "severity": "high" if depleted else "medium",
            "deadline_sim": deadlines.get(s.get("ingredient")),
            "impact": "Menu items using it may be disabled." if depleted else "Stockout risk if demand holds.",
            "recommended_action": "Check the procurement plan covers it; expedite if not.",
            "impact_eur": None,
            "approval_id": None,
        })

    for st in snapshot.get("stations") or []:
        if not st.get("covered", True):
            issues.append({
                "instance_id": instance_id,
                "restaurant": title,
                "kind": "staff",
                "problem": f"Station {st.get('station', '?')} unstaffed",
                "severity": "high",
                "deadline_sim": None,
                "impact": f"Dishes blocked: {', '.join(st.get('dishes') or []) or 'unknown'}",
                "recommended_action": "Reassign a qualified cook or disable the dishes.",
                "impact_eur": revenue_at_stake(snapshot, st.get("dishes")),
                "approval_id": None,
            })

    for check in snapshot.get("safety_issues") or []:
        note = (check.get("note") or "").strip()
        issues.append({
            "instance_id": instance_id,
            "restaurant": title,
            "kind": "safety",
            "problem": f"Food-safety check failed: {check.get('title', 'unknown check')}",
            "severity": "critical",
            "deadline_sim": None,
            "impact": (
                f"Kitchen reported: {note}" if note
                else f"{check.get('overdue_min', 0)} min past due and still not done."
            ),
            "recommended_action": "Get the check done and logged now — HACCP records cannot be back-filled.",
            "impact_eur": None,
            "approval_id": None,
        })

    for member in snapshot.get("staff") or []:
        if member.get("status") != "present" and member.get("sole_cover_dishes_at_risk"):
            issues.append({
                "instance_id": instance_id,
                "restaurant": title,
                "kind": "staff",
                "problem": f"{member.get('name', 'Staff')} absent ({member.get('status')}) — sole cover",
                "severity": "high",
                "deadline_sim": None,
                "impact": f"At risk: {', '.join(member['sole_cover_dishes_at_risk'])}",
                "recommended_action": "Find cover or 86 the affected dishes for today.",
                "impact_eur": revenue_at_stake(snapshot, member["sole_cover_dishes_at_risk"]),
                "approval_id": None,
            })

    return issues


# Incident lifecycle: an incident is "live" while it still needs a human.
OPEN_STATUSES = ("open", "acked")


def incident_key(row: Dict[str, Any]) -> tuple:
    """Stable identity for an incident across polls.

    ``merge_incidents`` already dedupes by summary and phrases deterministically,
    so the same ongoing problem produces the same summary every poll — which is
    what makes ``(instance_id, category, summary)`` a usable key without storing
    a fingerprint on the child side.
    """
    return (row.get("instance_id"), row.get("category"), row.get("summary"))


def reconcile_incidents(
    derived: List[Dict[str, Any]], stored: List[Dict[str, Any]]
) -> Dict[str, List[Any]]:
    """Pure: which incidents to open, and which stored rows to auto-resolve.

    ``derived`` is this poll's incidents (from ``merge_incidents``); ``stored``
    is every row currently in the manager DB. Returns
    ``{"open": [derived rows], "resolve": [incident_ids]}``.

    A resolved row is never revived — if the same problem comes back a *new* row
    opens, so the history reads as two episodes rather than one flapping row.
    """
    live = {
        incident_key(r): r for r in stored if r.get("status") in OPEN_STATUSES
    }
    derived_by_key = {incident_key(r): r for r in derived}
    return {
        "open": [r for k, r in derived_by_key.items() if k not in live],
        "resolve": sorted(
            r["incident_id"] for k, r in live.items() if k not in derived_by_key
        ),
    }


# ---------------------------------------------------------------------------
# Incident store — stdlib sqlite3 (the manager has no ORM and should not gain
# one). Everything lives under STATE_DIR so tests relocate the whole footprint.
# ---------------------------------------------------------------------------

_MANAGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    incident_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id      TEXT NOT NULL,
    category         TEXT NOT NULL,
    summary          TEXT NOT NULL,
    opened_at        REAL,          -- child sim-time the source signal fired
    status           TEXT NOT NULL DEFAULT 'open',   -- open | acked | resolved
    acked_by         TEXT,
    resolved_at      REAL,          -- wall-clock epoch (an operator action)
    source_signal_id TEXT           -- NULL when the row batches several signals
);
CREATE INDEX IF NOT EXISTS incidents_status ON incidents (status);
CREATE INDEX IF NOT EXISTS incidents_instance ON incidents (instance_id);

-- Last sim-day seen per instance. The sim has no day-rollover hook (the field
-- is derived on every clock write), so the manager detects the boundary by
-- remembering what it saw last — and it must survive a manager restart, or
-- every restart would re-archive the day already in progress.
CREATE TABLE IF NOT EXISTS instance_days (
    instance_id TEXT PRIMARY KEY,
    last_day    INTEGER NOT NULL,
    updated_at  REAL
);
"""


def incidents_db() -> sqlite3.Connection:
    """Open (and lazily create) the manager incident store.

    One connection per request — at portfolio scale the cost is a rounding error
    next to the child fan-out, and it keeps the manager free of connection-pool
    state. ``STATE_DIR`` is read at call time so tests can monkeypatch it.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STATE_DIR / "manager.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(_MANAGER_SCHEMA)
    conn.commit()
    return conn


def apply_reconcile(
    conn: sqlite3.Connection, derived: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Persist one reconcile pass and return the live rows afterwards."""
    stored = [dict(r) for r in conn.execute("SELECT * FROM incidents")]
    plan = reconcile_incidents(derived, stored)

    for row in plan["open"]:
        conn.execute(
            "INSERT INTO incidents (instance_id, category, summary, opened_at,"
            " status, source_signal_id) VALUES (?, ?, ?, ?, 'open', ?)",
            (row.get("instance_id"), row.get("category"), row.get("summary"),
             row.get("created_at"), row.get("source_signal_id")),
        )
    if plan["resolve"]:
        conn.execute(
            "UPDATE incidents SET status = 'resolved', resolved_at = ?"
            f" WHERE incident_id IN ({','.join('?' * len(plan['resolve']))})",
            [time.time(), *plan["resolve"]],
        )
    conn.commit()
    return [
        dict(r) for r in conn.execute(
            f"SELECT * FROM incidents WHERE status IN {OPEN_STATUSES}"
        )
    ]


def next_day_coverage_risks(
    plan_items: List[Dict[str, Any]],
    horizons: List[Dict[str, Any]],
    sim_time: float,
    *,
    instance_id: str,
    restaurant: str,
) -> List[Dict[str, Any]]:
    """Pure: ingredients whose procurement cover lapses *during tomorrow*.

    Today's stock rows say "you are low now". This says "the plan stops covering
    Basil at 14:00 tomorrow, and tomorrow forecasts 118 covers" — the thing that
    is still fixable tonight and invisible until it is not.

    Deliberately scoped to cover ending strictly inside tomorrow's window.
    Anything lapsing *before* tomorrow starts is today's problem and
    ``build_issues`` already raises it as low stock or uncoverable; repeating it
    here would double-count the same ingredient in two lists.
    """
    day = int(sim_time // SECONDS_PER_DAY)
    start = (day + 1) * SECONDS_PER_DAY
    end = (day + 2) * SECONDS_PER_DAY

    # Newest horizon that actually forecasts tomorrow. `day_index` is relative
    # to each horizon's own start, so match on the absolute window instead.
    forecast_qty: Optional[float] = None
    for horizon in sorted(horizons, key=lambda h: h.get("generated_at") or 0,
                          reverse=True):
        for row in ((horizon.get("breakdown") or {}).get("by_day") or []):
            if int(float(row.get("start") or 0.0) // SECONDS_PER_DAY) == day + 1:
                forecast_qty = row.get("qty")
                break
        if forecast_qty is not None:
            break

    demand = (
        f"Tomorrow forecasts {int(forecast_qty)} covers."
        if forecast_qty is not None
        else "No forecast covering tomorrow has been generated yet."
    )

    risks = []
    for item in plan_items:
        covers_until = item.get("covers_until")
        if covers_until is None or not (start <= float(covers_until) < end):
            continue
        name = item.get("ingredient_name") or str(item.get("ingredient_id"))
        risks.append({
            "instance_id": instance_id,
            "restaurant": restaurant,
            "kind": "coverage",
            "problem": (
                f"{name} is only covered until {sim_label(covers_until)} — "
                "tomorrow runs dry after that."
            ),
            "severity": "high",
            "deadline_sim": item.get("order_date") or float(covers_until),
            "impact": demand,
            "impact_eur": None,
            "recommended_action": (
                f"Extend tomorrow's cover for {name} on the next procurement run."
            ),
            "approval_id": None,
        })
    return sorted(risks, key=lambda r: r["deadline_sim"])


def rank_issues(issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Severity first, then earliest deadline (no deadline sorts last)."""
    return sorted(issues, key=lambda i: (
        SEVERITY_ORDER.get(i.get("severity"), 9),
        i.get("deadline_sim") if i.get("deadline_sim") is not None else float("inf"),
    ))


# Incident categories (docs/fable/incidents.md). Every category now has a
# detector, so `unavailable_categories` is empty.
SIGNAL_TO_INCIDENT = {
    "ORDER_BACKLOG": "order_backlog",
    "EQUIPMENT_FAILURE": "equipment_failure",
    "STAFF_AVAILABILITY": "staff_no_show",
    "STAFF_COVERAGE": "staff_no_show",
    "STOCKOUT_RISK": "stockout",
    "LOW_STOCK": "stockout",
    "INGREDIENT_UNCOVERABLE": "stockout",
    "EXPIRY_RISK": "food_safety",
    "FOOD_SAFETY_CHECK": "food_safety_checks",
}

# One manager-readable sentence per merged group; {names} is the ingredient
# list. Raw statuses like "at_risk" never reach the UI.
_GROUP_PHRASES = {
    "INGREDIENT_UNCOVERABLE": "No supplier can deliver {names} in time — the kitchen will run short.",
    "LOW_STOCK": "Running low on {names} (at or below safety stock).",
    "STOCKOUT_RISK": "{names} projected to run out before the next delivery.",
    "EXPIRY_RISK": "{names} close to expiry — use first or discard.",
}
_DELAY_PHRASES = {
    "at_risk": "may arrive late — a one-day delivery slip would leave the kitchen short",
    "uncoverable": "cannot be delivered in time by any supplier — dishes will run short",
}
# Food-safety checks stay one row per check rather than batching into a
# {names} list: the *reason* a fridge log was skipped is the whole point, and
# merging two different HACCP failures into one sentence would lose it.
_BACKLOG_PHRASES = {
    "critical": "{queued} tickets are backed up on the pass with {cooks} — guests are "
                "waiting and orders will start walking.",
    "warning": "{queued} tickets waiting on the pass with {cooks} — the kitchen is "
               "falling behind.",
}
_SAFETY_PHRASES = {
    "not_done": "{title} — the kitchen reported this not done{reason}.",
    "overdue": "{title} — still not done {overdue_min} min past due; a food-safety "
               "check cannot be back-filled later.",
}


def join_names(names: List[str]) -> str:
    """Deterministic '"A", "B" and "C"' list (deduped, sorted)."""
    unique = sorted({n for n in names if n})
    if not unique:
        return "some items"
    if len(unique) == 1:
        return unique[0]
    return ", ".join(unique[:-1]) + " and " + unique[-1]


def merge_incidents(
    signals: List[Dict[str, Any]],
    plan_items: List[Dict[str, Any]],
    ingredient_names: Dict[int, str],
    open_safety_task_ids: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Turn raw live signals + at-risk plan items into merged, human-readable
    incidents: similar items are batched (per supplier / per signal type) and
    phrased for a manager, never as raw status codes. Pure + deterministic
    (tested in tests/test_manager.py).

    ``open_safety_task_ids`` is the set of temp/safety checks *still* failing
    (from the child's snapshot). A FOOD_SAFETY_CHECK signal cannot be retracted —
    the bus only expires by TTL — so without this a remediated check would sit on
    the incident board for 24h after the kitchen fixed it. Pass ``None`` when the
    snapshot is unavailable: an unreachable child must not silently close
    incidents."""

    def ingredient_name(payload: Dict[str, Any]) -> str:
        return (
            payload.get("ingredient_name")
            or ingredient_names.get(payload.get("ingredient_id"))
            or (f"ingredient #{payload['ingredient_id']}" if payload.get("ingredient_id") else "")
        )

    rows: List[Dict[str, Any]] = []

    # --- signal groups batched by type: {names} phrased per _GROUP_PHRASES --
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for s in signals:
        sig_type = str(s.get("type"))
        category = SIGNAL_TO_INCIDENT.get(sig_type)
        if category is None:
            continue
        payload = s.get("payload") or {}
        if sig_type in _GROUP_PHRASES:
            grouped.setdefault(sig_type, []).append(s)
        elif sig_type == "ORDER_BACKLOG":
            cooks = payload.get("cooks_present")
            rows.append({
                "category": category, "signal_type": sig_type,
                "summary": _BACKLOG_PHRASES[
                    "critical" if payload.get("level") == "critical" else "warning"
                ].format(
                    queued=payload.get("queued_count") or 0,
                    cooks=("nobody cooking" if not cooks
                           else "1 cook working" if cooks == 1
                           else f"{cooks} cooks working"),
                ),
                "count": 1, "names": [], "created_at": s.get("created_at"),
                "source_signal_id": s.get("signal_id"),
            })
        elif sig_type == "EQUIPMENT_FAILURE":
            rows.append({
                "category": category, "signal_type": sig_type,
                "summary": (
                    f"{payload.get('label') or 'Equipment'} is out of service — the "
                    f"{payload.get('station') or 'station'} station's dishes are off "
                    f"the menu until it is back."
                ),
                "count": 1, "names": [], "created_at": s.get("created_at"),
                "source_signal_id": s.get("signal_id"),
            })
        elif sig_type == "FOOD_SAFETY_CHECK":
            if (
                open_safety_task_ids is not None
                and payload.get("task_id") not in open_safety_task_ids
            ):
                continue  # the kitchen has since done it — stop showing it
            note = str(payload.get("note") or "").strip()
            rows.append({
                "category": category, "signal_type": sig_type,
                "summary": _SAFETY_PHRASES[
                    "overdue" if payload.get("outcome") == "overdue" else "not_done"
                ].format(
                    title=payload.get("title") or "A food-safety check",
                    reason=f": {note}" if note else "",
                    overdue_min=payload.get("overdue_min") or 0,
                ),
                "count": 1, "names": [], "created_at": s.get("created_at"),
                "source_signal_id": s.get("signal_id"),
            })
        elif sig_type == "STAFF_COVERAGE":
            # Routine coverage broadcast — only a lost station is an incident.
            if payload.get("covered", False) and not payload.get("shortfall"):
                continue
            station = payload.get("station_name") or payload.get("station_id")
            rows.append({
                "category": category, "signal_type": sig_type,
                "summary": f"Station {station} has no qualified cover — its dishes are blocked.",
                "count": 1, "names": [], "created_at": s.get("created_at"),
                "source_signal_id": s.get("signal_id"),
            })
        else:  # STAFF_AVAILABILITY
            who = payload.get("staff_name") or payload.get("name") or "A staff member"
            status = payload.get("status") or "absent"
            reason = payload.get("reason")
            summary = f"{who} is {status}" + (f" — {reason}." if reason else ".")
            rows.append({
                "category": category, "signal_type": sig_type,
                "summary": summary,
                "count": 1, "names": [], "created_at": s.get("created_at"),
                "source_signal_id": s.get("signal_id"),
            })

    for sig_type, members in grouped.items():
        names = [ingredient_name(m.get("payload") or {}) for m in members]
        rows.append({
            "category": SIGNAL_TO_INCIDENT[sig_type],
            "signal_type": sig_type,
            "summary": _GROUP_PHRASES[sig_type].format(names=join_names(names)),
            "count": len(members),
            "names": sorted({n for n in names if n}),
            "created_at": max((m.get("created_at") or 0) for m in members),
            # Batched: no single source signal to point at.
            "source_signal_id": None,
        })

    # --- supplier delays batched per (supplier, status) ---------------------
    delays: Dict[tuple, List[Dict[str, Any]]] = {}
    for item in plan_items:
        if item.get("status") in _DELAY_PHRASES:
            key = (item.get("supplier_name") or "a supplier", item["status"])
            delays.setdefault(key, []).append(item)
    for (supplier, status), members in delays.items():
        names = [m.get("ingredient_name") or "?" for m in members]
        rows.append({
            "category": "supplier_delay",
            "signal_type": "PLANNED_ORDER",
            "summary": f"{join_names(names)} from {supplier} {_DELAY_PHRASES[status]}.",
            "count": len(members),
            "names": sorted(set(names)),
            "created_at": max((m.get("order_date") or 0) for m in members),
            "source_signal_id": None,
        })

    # Dedupe identical summaries (e.g. repeated coverage signals), keep newest.
    seen: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        prior = seen.get(r["summary"])
        if prior is None or (r["created_at"] or 0) > (prior["created_at"] or 0):
            seen[r["summary"]] = r
    return sorted(seen.values(), key=lambda r: (r["category"], r["summary"]))


# ---------------------------------------------------------------------------
# Registry + child process management
# ---------------------------------------------------------------------------

class Registry:
    """Instance records persisted to JSON; live Popen handles kept in-memory."""

    def __init__(self) -> None:
        self.instances: Dict[str, Dict[str, Any]] = {}
        self.procs: Dict[str, subprocess.Popen] = {}
        self.load()

    def load(self) -> None:
        if REGISTRY_PATH.exists():
            try:
                self.instances = json.loads(REGISTRY_PATH.read_text())
            except (OSError, json.JSONDecodeError):
                logger.warning("registry unreadable, starting empty")
                self.instances = {}

    def save(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        REGISTRY_PATH.write_text(json.dumps(self.instances, indent=2))

    def running(self, instance_id: str) -> bool:
        proc = self.procs.get(instance_id)
        return proc is not None and proc.poll() is None


registry = Registry()


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn(instance_id: str, port: int) -> subprocess.Popen:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "DB_PATH": str(STATE_DIR / f"{instance_id}.db"),
    }
    log_file = open(STATE_DIR / f"{instance_id}.log", "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "core.api:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=log_file, stderr=subprocess.STDOUT,
    )
    registry.procs[instance_id] = proc
    return proc


def _base_url(inst: Dict[str, Any]) -> str:
    return f"http://127.0.0.1:{inst['port']}"


_client: Optional[httpx.AsyncClient] = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=PROBE_TIMEOUT_S)
    return _client


async def _wait_healthy(inst: Dict[str, Any]) -> Dict[str, Any]:
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        proc = registry.procs.get(inst["id"])
        if proc is not None and proc.poll() is not None:
            raise HTTPException(502, f"instance {inst['id']} exited on startup "
                                     f"(see {STATE_DIR / (inst['id'] + '.log')})")
        try:
            resp = await client().get(f"{_base_url(inst)}/api/health")
            if resp.status_code == 200:
                return resp.json()
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.5)
    raise HTTPException(504, f"instance {inst['id']} did not become healthy")


async def _get(inst: Dict[str, Any], path: str) -> Optional[Any]:
    """GET a child endpoint; None on any failure (offline / slow / 5xx)."""
    try:
        resp = await client().get(f"{_base_url(inst)}{path}")
        if resp.status_code == 200:
            return resp.json()
    except httpx.HTTPError:
        pass
    return None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Detects sim-day rollovers and auto-captures / archives on them. Started
    # here rather than lazily so a manager left running overnight keeps its
    # archive complete without anyone opening the dashboard.
    watcher = asyncio.create_task(_rollover_watch())
    yield
    watcher.cancel()
    # Terminate the children we spawned; DBs and the registry persist, so
    # /admin/api/instances/{id}/start brings any of them back.
    for instance_id, proc in registry.procs.items():
        if proc.poll() is None:
            logger.info("terminating instance %s", instance_id)
            proc.terminate()
    for proc in registry.procs.values():
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    if _client is not None:
        await _client.aclose()


app = FastAPI(title="roba manager", lifespan=lifespan)


class CreateInstanceBody(BaseModel):
    preset: str
    id: Optional[str] = None  # normally generated (running_fox style)


def _inst_or_404(instance_id: str) -> Dict[str, Any]:
    inst = registry.instances.get(instance_id)
    if inst is None:
        raise HTTPException(404, f"unknown instance {instance_id!r}")
    return inst


@app.get("/admin/api/presets")
def list_presets() -> List[str]:
    return sorted(p.stem for p in DATA_DIR.glob("*.json"))


@app.get("/admin/api/instances")
async def list_instances() -> List[Dict[str, Any]]:
    async def probe(inst: Dict[str, Any]) -> Dict[str, Any]:
        health = await _get(inst, "/api/health")
        return {**inst, "online": health is not None,
                "sim": (health or {}).get("sim")}
    return list(await asyncio.gather(*(probe(i) for i in registry.instances.values())))


@app.post("/admin/api/instances")
async def create_instance(body: CreateInstanceBody) -> Dict[str, Any]:
    if body.preset not in list_presets():
        raise HTTPException(404, f"unknown preset {body.preset!r}")
    instance_id = body.id or generate_instance_id(set(registry.instances))
    if instance_id in registry.instances:
        raise HTTPException(409, f"instance {instance_id!r} already exists")
    inst = {
        "id": instance_id,
        "preset": body.preset,
        "port": _free_port(),
        "title": body.preset.replace("_", " ").title(),
        "created_at": time.time(),
    }
    _spawn(instance_id, inst["port"])
    registry.instances[instance_id] = inst
    registry.save()
    await _wait_healthy(inst)
    seed = await client().post(
        f"{_base_url(inst)}/api/seed/preset/{body.preset}", timeout=60.0
    )
    seed.raise_for_status()
    identity = await _get(inst, "/api/settings/identity") or {}
    if identity.get("title"):
        inst["title"] = identity["title"]
        registry.save()
    return {**inst, "online": True}


@app.post("/admin/api/instances/{instance_id}/start")
async def start_instance(instance_id: str) -> Dict[str, Any]:
    inst = _inst_or_404(instance_id)
    if registry.running(instance_id) or await _get(inst, "/api/health"):
        return {**inst, "online": True}
    inst["port"] = _free_port()  # old port may be taken after a restart
    registry.save()
    _spawn(instance_id, inst["port"])
    await _wait_healthy(inst)  # DB persists — no reseed
    return {**inst, "online": True}


@app.post("/admin/api/instances/{instance_id}/stop")
async def stop_instance(instance_id: str) -> Dict[str, Any]:
    inst = _inst_or_404(instance_id)
    proc = registry.procs.get(instance_id)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        await asyncio.to_thread(proc.wait, 10)
    return {**inst, "online": False}


@app.delete("/admin/api/instances/{instance_id}")
async def delete_instance(instance_id: str) -> Dict[str, Any]:
    inst = _inst_or_404(instance_id)
    await stop_instance(instance_id)
    registry.instances.pop(instance_id, None)
    registry.procs.pop(instance_id, None)
    registry.save()
    # DB + log files are kept on disk (cheap, recoverable); docs cover cleanup.
    return {"deleted": instance_id, "db_kept": str(STATE_DIR / f"{instance_id}.db")}


# ---------------------------------------------------------------------------
# Aggregation: overview + priority action queue
# ---------------------------------------------------------------------------

async def _instance_overview(inst: Dict[str, Any]) -> Dict[str, Any]:
    health = await _get(inst, "/api/health")
    if health is None:
        # Probe failed — but if the process we spawned is verifiably alive,
        # it's busy (solver/LLM stall), not gone. Don't cry "offline".
        busy = registry.running(inst["id"])
        return {
            **inst, "online": False,
            "status": "warning" if busy else "offline",
            "note": "unresponsive — likely mid-solve" if busy else None,
            "issues": [],
            "sales_today": None, "forecast_today": None, "orders_today": None,
            "staff_present": None, "staff_total": None, "absent": [],
            "stock_risks": [], "pending_approvals": 0,
            # No snapshot to read them from — an offline card must not report
            # the last numbers it saw.
            "orders_waiting": None, "ticket_time_min": None, "safety_issues": None,
            "task_compliance": None,
        }
    sim_time = float((health.get("sim") or {}).get("sim_time") or 0.0)
    day_start = (sim_time // SECONDS_PER_DAY) * SECONDS_PER_DAY
    snapshot, stats, approvals, warnings, plan = await asyncio.gather(
        _get(inst, "/api/ops/snapshot"),
        _get(inst, f"/api/pos/stats?since={day_start}&window=day"),
        _get(inst, "/api/approvals?status=pending"),
        _get(inst, "/api/track-b/procurement/warnings"),
        # Only for the order-by deadlines on stock rows — the plan is already
        # fetched per-instance by /admin/api/incidents, so this is one more
        # parallel read, not a new round trip.
        _get(inst, "/api/track-b/procurement/plan"),
    )
    approvals = approvals or []
    warnings = warnings or []
    snapshot = snapshot or {}
    staff = snapshot.get("staff") or []
    dishes = snapshot.get("dishes") or []
    status = derive_status(online=True, snapshot=snapshot, warnings=warnings,
                           pending_approvals=approvals)
    issues = build_issues(inst["id"], inst["title"], snapshot=snapshot,
                          warnings=warnings, pending_approvals=approvals,
                          plan_items=(plan or {}).get("items") or [])
    return {
        **inst,
        "online": True,
        "sim": health.get("sim"),
        "status": status,
        "issues": issues,
        "sales_today": round(float((stats or {}).get("revenue") or 0.0), 2),
        "orders_today": (stats or {}).get("orders"),
        "forecast_today": round(sum(
            float(d.get("revenue_estimate") or 0.0) for d in dishes if d.get("active")
        ), 2),
        "staff_present": sum(1 for s in staff if s.get("status") == "present"),
        "staff_total": len(staff),
        "absent": [s.get("name") for s in staff if s.get("status") != "present"],
        "stock_risks": (snapshot.get("low_stock_ingredients") or []) + [
            {"ingredient": w.get("ingredient_name"), "status": "uncoverable",
             "on_hand_display": f"short {w.get('short_qty', 0):g} {w.get('unit', '')}"}
            for w in warnings
        ],
        "pending_approvals": len(approvals),
        "orders_waiting": snapshot.get("queued_count"),
        "ticket_time_min": snapshot.get("avg_ticket_minutes"),
        # The snapshot carries the failures themselves; the card carries a count.
        "safety_issues": len(snapshot.get("safety_issues") or []),
        "task_compliance": snapshot.get("task_compliance"),
    }


@app.get("/admin/api/overview")
async def overview() -> Dict[str, Any]:
    cards = list(await asyncio.gather(
        *(_instance_overview(i) for i in registry.instances.values())
    ))
    actions = rank_issues([issue for card in cards for issue in card["issues"]])
    return {"instances": cards, "actions": actions}


# ---------------------------------------------------------------------------
# Combined approvals
# ---------------------------------------------------------------------------

@app.get("/admin/api/approvals")
async def all_approvals(status: Optional[str] = None) -> List[Dict[str, Any]]:
    path = "/api/approvals" + (f"?status={status}" if status else "")

    async def fetch(inst: Dict[str, Any]) -> List[Dict[str, Any]]:
        rows = await _get(inst, path) or []
        return [{**r, "instance_id": inst["id"], "restaurant": inst["title"]}
                for r in rows]

    nested = await asyncio.gather(*(fetch(i) for i in registry.instances.values()))
    rows = [r for batch in nested for r in batch]
    rows.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return rows


@app.post("/admin/api/approvals/{instance_id}/{approval_id}/{decision}")
async def resolve_approval(instance_id: str, approval_id: int, decision: str) -> Any:
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")
    inst = _inst_or_404(instance_id)
    resp = await client().post(
        f"{_base_url(inst)}/api/approvals/{approval_id}/{decision}"
    )
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type"))


# ---------------------------------------------------------------------------
# Incidents (docs/fable/incidents.md — partial: derived from live signals)
# ---------------------------------------------------------------------------

@app.get("/admin/api/incidents")
async def incidents() -> Dict[str, Any]:
    async def fetch(inst: Dict[str, Any]) -> List[Dict[str, Any]]:
        signals, plan, ingredients, snapshot = await asyncio.gather(
            _get(inst, "/api/signals?status=live"),
            _get(inst, "/api/track-b/procurement/plan"),
            _get(inst, "/api/ingredients"),
            # Live truth for which safety checks are *still* failing — a
            # FOOD_SAFETY_CHECK signal cannot be retracted, only expired.
            _get(inst, "/api/ops/snapshot"),
        )
        names = {int(i["id"]): i.get("name") for i in ingredients or [] if i.get("id")}
        open_safety = (
            {c.get("task_id") for c in snapshot.get("safety_issues") or []}
            if isinstance(snapshot, dict) and "safety_issues" in snapshot
            else None  # unreachable child → never silently close incidents
        )
        merged = merge_incidents(
            signals or [], (plan or {}).get("items") or [], names, open_safety
        )
        return [{**r, "instance_id": inst["id"], "restaurant": inst["title"]}
                for r in merged]

    nested = await asyncio.gather(*(fetch(i) for i in registry.instances.values()))
    derived = [r for batch in nested for r in batch]

    # Incidents are first-class rows now: this pass opens the new ones, resolves
    # the ones whose source is gone, and carries ack state back onto the view.
    conn = incidents_db()
    try:
        live = apply_reconcile(conn, derived)
    finally:
        conn.close()
    stored_by_key = {incident_key(r): r for r in live}

    rows: List[Dict[str, Any]] = []
    for row in derived:
        stored = stored_by_key.get(incident_key(row)) or {}
        rows.append({
            **row,
            "incident_id": stored.get("incident_id"),
            "status": stored.get("status", "open"),
            "acked_by": stored.get("acked_by"),
        })
    rows.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return {
        "incidents": rows,
        # Every category has a detector now (Phase 3) — kept in the response so
        # the UI contract is stable and a future gap can be declared again.
        "unavailable_categories": [],
    }


class AckBody(BaseModel):
    acked_by: Optional[str] = None


@app.post("/admin/api/incidents/{incident_id}/ack")
def ack_incident(incident_id: int, body: Optional[AckBody] = None) -> Dict[str, Any]:
    """Acknowledge an incident — "seen, being handled", not "fixed"."""
    return _set_incident_state(
        incident_id, "acked",
        acked_by=(body.acked_by if body else None) or "manager",
    )


@app.post("/admin/api/incidents/{incident_id}/resolve")
def resolve_incident(incident_id: int) -> Dict[str, Any]:
    """Close an incident by hand.

    Needed because not every incident's source disappears on its own — a
    FOOD_SAFETY_CHECK signal, for instance, only expires on its 24h TTL.
    Reconcile will not re-open this row; a recurrence opens a fresh one.
    """
    return _set_incident_state(incident_id, "resolved", resolved_at=time.time())


def _set_incident_state(
    incident_id: int,
    status: str,
    *,
    acked_by: Optional[str] = None,
    resolved_at: Optional[float] = None,
) -> Dict[str, Any]:
    conn = incidents_db()
    try:
        row = conn.execute(
            "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Incident {incident_id} not found")
        conn.execute(
            "UPDATE incidents SET status = ?,"
            " acked_by = COALESCE(?, acked_by), resolved_at = COALESCE(?, resolved_at)"
            " WHERE incident_id = ?",
            (status, acked_by, resolved_at, incident_id),
        )
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
        ).fetchone())
    finally:
        conn.close()


@app.get("/admin/api/incidents/history")
def incidents_history(
    instance_id: Optional[str] = None,
    since: Optional[float] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """Every incident ever opened, newest first — including resolved ones.

    ``since`` filters on ``opened_at`` (child sim-time, as recorded when the row
    was opened), so it lines up with the sim clock the rest of the UI shows.
    """
    clauses, params = [], []
    if instance_id:
        clauses.append("instance_id = ?")
        params.append(instance_id)
    if since is not None:
        clauses.append("opened_at >= ?")
        params.append(float(since))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = incidents_db()
    try:
        rows = conn.execute(
            f"SELECT * FROM incidents{where}"
            " ORDER BY COALESCE(opened_at, 0) DESC, incident_id DESC LIMIT ?",
            [*params, max(1, min(int(limit), 1000))],
        ).fetchall()
        return {"incidents": [dict(r) for r in rows]}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Daily summary
# ---------------------------------------------------------------------------

@app.get("/admin/api/summary")
async def daily_summary() -> Dict[str, Any]:
    cards = list(await asyncio.gather(
        *(_instance_overview(i) for i in registry.instances.values())
    ))

    async def tomorrow(inst: Dict[str, Any], card: Dict[str, Any]) -> tuple:
        """Today's waste plus tomorrow's coverage gaps, in one round trip.

        Yes, this re-reads the plan that ``_instance_overview`` already fetched.
        Deliberate: the alternative is moving the coverage join into the
        overview, which is polled every **5s** by the dashboard versus this
        endpoint's 30s — so sharing the read would mean fetching the forecast
        horizons six times as often, and would put a next-*day* lens into the
        priority queue, which is a today surface. One extra child query per
        instance per 30s is the cheaper side of that trade.
        """
        if not card["online"]:
            return 0.0, []
        sim_time = float((card.get("sim") or {}).get("sim_time") or 0.0)
        day_start = (sim_time // SECONDS_PER_DAY) * SECONDS_PER_DAY
        waste_rows, plan, horizons = await asyncio.gather(
            _get(inst, "/api/waste"),
            _get(inst, "/api/track-b/procurement/plan"),
            _get(inst, "/api/track-a/forecast/horizons"),
        )
        cost = round(sum(
            float(r.get("cost") or 0.0) for r in waste_rows or []
            if float(r.get("sim_time") or 0.0) >= day_start
        ), 2)
        return cost, next_day_coverage_risks(
            (plan or {}).get("items") or [],
            (horizons or {}).get("horizons") or [],
            sim_time,
            instance_id=card["id"],
            restaurant=card["title"],
        )

    per_instance = await asyncio.gather(*(
        tomorrow(registry.instances[c["id"]], c) for c in cards
    ))
    waste = [w for w, _ in per_instance]
    coverage = [r for _, rows in per_instance for r in rows]
    for card, w in zip(cards, waste):
        card["waste_today"] = w

    actions = rank_issues([i for c in cards for i in c["issues"]])
    online = [c for c in cards if c["online"]]
    return {
        "generated_at": time.time(),
        "restaurants": cards,
        "totals": {
            "sales_today": round(sum(c["sales_today"] or 0 for c in online), 2),
            "forecast_today": round(sum(c["forecast_today"] or 0 for c in online), 2),
            "waste_today": round(sum(waste), 2),
            "stock_risks": sum(len(c["stock_risks"]) for c in online),
            "staff_absent": sum(len(c["absent"]) for c in online),
            "pending_approvals": sum(c["pending_approvals"] for c in online),
            "offline": len(cards) - len(online),
        },
        "major_incidents": [a for a in actions if a["severity"] == "critical"],
        "pending_decisions": [a for a in actions if a["kind"] == "approval"],
        # Stock rows are "low right now"; coverage rows are "the plan stops
        # covering this during tomorrow" — joined from the procurement plan's
        # covers_until against the forecaster's horizon, here rather than in the
        # frontend so the archive snapshots carry it too.
        "next_day_risks": rank_issues(
            [a for a in actions if a["kind"] == "stock"] + coverage
        ),
    }


# ---------------------------------------------------------------------------
# Catch-up summarizer (docs/fable/catchup.md §"Readable structured summary")
#
# One prompt per subsystem, never over the raw firehose: the bucketing below is
# deterministic and unit-tested, so the LLM only ever writes prose about a
# handful of related events and the manager can tell which events a bullet came
# from. Merging is Phase 6's job; this file only summarizes one capture.
# ---------------------------------------------------------------------------

# ``EventLog.category`` -> subsystem bucket. There is no enum for it — every
# writer passes a free string to ``core.events.log_event`` — so this map is
# enumerated from the call sites and an unrecognised category lands in "other"
# rather than being silently dropped.
EVENT_BUCKETS: Dict[str, str] = {
    # Purchase orders, sourcing plans, supplier negotiation.
    "po_placed": "procurement",
    "po_delivered": "procurement",
    "po_pending_approval": "procurement",
    "sourcing_plan": "procurement",
    "sourcing_plan_skipped": "procurement",
    "reorder_failed": "procurement",
    "optimizer": "procurement",
    "llm_optimize": "procurement",
    "negotiation_requested": "procurement",
    "negotiation_agreed": "procurement",
    "negotiation_no_deal": "procurement",
    "negotiation_skipped": "procurement",
    # Stock movements and what they cost.
    "receipt": "inventory",
    "waste": "inventory",
    "reconciliation": "inventory",
    "stockout_risk": "inventory",
    "low_stock": "inventory",
    "manual_shortage": "inventory",
    "inventory_signal_muted": "inventory",
    "spoilage_pattern": "inventory",
    # Forecasts and the cook feedback that tunes them.
    "forecast": "demand",
    "cook_feedback": "demand",
    "batch_advisor": "demand",
    # Who turned up.
    "attendance": "staffing",
    # What guests could actually order.
    "menu_toggle": "menu",
    "promo_proposal": "promos",
    "promo_activated": "promos",
    # Competitor intel and the calls that produced it.
    "competitor": "market",
    "call": "market",
    # Scenario injections land in "other" alongside anything unrecognised.
}

# Render/prompt order. Every bucket here has at least one real event source —
# note there is deliberately no "reviews" bucket: the review agent writes no
# event_log rows at all (docs/fable/progress.md §4).
BUCKET_ORDER = (
    "procurement", "inventory", "demand", "staffing",
    "menu", "promos", "market", "other",
)

_BUCKET_BRIEF = {
    "procurement": "purchase orders, sourcing plans and supplier negotiation",
    "inventory": "stock levels, deliveries received, waste and spoilage",
    "demand": "demand forecasts and the cook feedback that tunes them",
    "staffing": "who turned up, who was absent, and how it was covered",
    "menu": "dishes taken off or put back on the menu",
    "promos": "promotions proposed and activated",
    "market": "competitor intel and the calls that produced it",
    "other": "activity that belongs to no other subsystem",
}

# Mirrors core.llm.CANNED_NOTE — the marker a *degraded* LLM call carries when
# the provider never actually answered. Kept as a literal so the summarizer is
# testable without importing core; tests assert the two stay equal.
CANNED_NOTE = "canned_fallback"

# Prompt sizing. A busy sim-day can log thousands of rows; the cap keeps one
# bucket's prompt bounded and reports what it dropped rather than lying.
CATCHUP_MAX_EVENTS_PER_BUCKET = int(os.getenv("CATCHUP_MAX_EVENTS_PER_BUCKET", "120"))
# Generous on purpose: a 2.5 "thinking" model spends reasoning tokens against
# max_output_tokens, and a starved one truncates into the canned path (§4 LLM).
CATCHUP_LLM_MAX_TOKENS = int(os.getenv("CATCHUP_LLM_MAX_TOKENS", "3000"))
CATCHUP_MAX_BULLETS = 6

_BULLET_SCHEMA = {
    "type": "object",
    "properties": {"bullets": {"type": "array"}},
    "required": ["bullets"],
}

def _summarizer() -> tuple:
    """A fresh ``(LLMProvider, model)`` for one summarize request.

    ``core`` is imported here as a *library*: the manager's "do not reach into
    child state, HTTP is the contract" rule is about child **data**, not shared
    code (docs/fable/progress.md §4). The import is lazy so the manager still
    boots and serves every non-LLM endpoint without the sim's dependencies.

    Deliberately **not** a cached singleton. ``LLMProvider`` memoises by content
    hash for the life of the instance, and a *canned* answer is memoised like
    any other — so a shared provider would keep serving the same failure to
    every "Re-summarize" press until the manager was restarted, even after the
    credentials it choked on were fixed. One provider per request costs a client
    build against a ~30s call and makes retrying mean something.
    """
    from core import config as core_config
    from core.llm import LLMProvider
    return (
        LLMProvider(timeout_s=core_config.LLM_AUTHORITY_TIMEOUT_S),
        core_config.GEMINI_REASONER_MODEL,
    )


def bucket_events(events: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Pure: group a capture's events by subsystem, in ``BUCKET_ORDER``.

    Groups on ``category``. ``EventLog`` has **no** ``event_type`` column
    whatever catchup.md §2 claims — see docs/fable/progress.md §4. Empty buckets
    are omitted so they never cost a prompt.
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for event in events:
        bucket = EVENT_BUCKETS.get(event.get("category") or "", "other")
        grouped.setdefault(bucket, []).append(event)
    return {b: grouped[b] for b in BUCKET_ORDER if b in grouped}


def _event_digest(event: Dict[str, Any]) -> Dict[str, Any]:
    """The fields worth spending prompt tokens on.

    ``detail`` is deliberately dropped: one optimizer row's detail alone can be
    kilobytes of solver output, while ``summary`` is already the human sentence
    the narrative feed renders. The raw rows stay in the capture file, so
    "click to expand" still shows everything.
    """
    return {
        "id": event.get("id"),
        "sim_time": event.get("sim_time"),
        "category": event.get("category"),
        "actor": event.get("actor"),
        "summary": event.get("summary"),
    }


def _bucket_messages(bucket: str, events: List[Dict[str, Any]]) -> List[dict]:
    """The one prompt this bucket gets."""
    brief = _BUCKET_BRIEF.get(bucket, bucket)
    return [
        {
            "role": "system",
            "content": (
                "You are briefing a restaurant manager who has been away.\n"
                f"Summarize the {bucket} activity below — {brief} — from the "
                "restaurant's event log.\n\n"
                f"Rules:\n"
                f"- At most {CATCHUP_MAX_BULLETS} bullets. Fewer is better: "
                "merge repetitive events into a single counted bullet.\n"
                "- One short factual sentence each, past tense. No preamble, "
                "no advice, no recommendations.\n"
                "- Quantify wherever the events do (counts, euros, ingredient "
                "and supplier names).\n"
                '- Every bullet must list the "id" of each event it came from '
                'in "event_ids" — the manager clicks a bullet to read those '
                "raw events.\n"
                "- Report only what the events say. Invent nothing.\n\n"
                'Return JSON: {"bullets": [{"text": "...", "event_ids": [1, 2]}]}'
            ),
        },
        {
            "role": "user",
            "content": json.dumps([_event_digest(e) for e in events], default=str),
        },
    ]


def _as_event_id(value: Any) -> Optional[int]:
    """Coerce an id the model echoed back; ``None`` when it is not one."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_bullets(
    result: Any, valid_ids: set
) -> Optional[List[Dict[str, Any]]]:
    """Validated bullets, or ``None`` when the provider never really answered.

    ``None`` is the canned-fallback / malformed case and must surface as an
    error — rendering canned filler as a summary is a trap this project has
    fallen into twice (docs/fable/progress.md §4 LLM).

    Event ids are filtered to the ones actually put in the prompt: a
    hallucinated id would make "click to expand" resolve against nothing.
    """
    if not isinstance(result, dict) or result.get("note") == CANNED_NOTE:
        return None
    raw = result.get("bullets")
    if not isinstance(raw, list):
        return None
    bullets: List[Dict[str, Any]] = []
    for item in raw[:CATCHUP_MAX_BULLETS]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        ids = item.get("event_ids")
        bullets.append({
            "text": text,
            "event_ids": [
                i for i in (_as_event_id(v) for v in (ids if isinstance(ids, list) else []))
                if i in valid_ids
            ],
        })
    return bullets


def summarize_capture(
    record: Dict[str, Any],
    incidents: Optional[List[Dict[str, Any]]] = None,
    complete: Optional[Any] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Turn one capture into the per-subsystem summary written to its file.

    ``complete`` is ``LLMProvider.complete``; tests inject a fake. A degraded
    provider returns ``error`` set and ``buckets`` empty — never prose.

    ponytail: buckets are prompted sequentially, so a capture touching every
    subsystem costs ~8 serial round trips. Fan out with a thread pool if the
    wait becomes the complaint.
    """
    if complete is None:
        provider, default_model = _summarizer()
        complete, model = provider.complete, model or default_model

    buckets: List[Dict[str, Any]] = []
    for name, rows in bucket_events(record.get("events") or []).items():
        recent = rows[-CATCHUP_MAX_EVENTS_PER_BUCKET:]
        result = complete(
            messages=_bucket_messages(name, recent),
            json_schema=_BULLET_SCHEMA,
            max_tokens=CATCHUP_LLM_MAX_TOKENS,
            use_site="catchup_summary",
            model=model,
        )
        bullets = _clean_bullets(result, {e.get("id") for e in recent})
        if bullets is None:
            return {
                "generated_at": time.time(),
                "model": model or "",
                "error": (
                    f"The summarizer did not answer: {model or 'the model'} fell "
                    f"back to canned output on the '{name}' bucket. Check "
                    "GEMINI_REASONER_MODEL and the Vertex credentials — a bad "
                    "model id degrades silently."
                ),
                "buckets": [],
                "incidents": incidents or [],
            }
        buckets.append({
            "bucket": name,
            "event_count": len(rows),
            "truncated": len(rows) - len(recent),
            "bullets": bullets,
        })

    return {
        "generated_at": time.time(),
        "model": model or "",
        "error": None,
        "buckets": buckets,
        "incidents": incidents or [],
    }


def incidents_in_window(
    instance_id: str, since_sim: float, until_sim: float
) -> List[Dict[str, Any]]:
    """Incidents *opened* inside a catch-up's window — "what blew up while you
    were away", including ones that have since resolved.

    Read from the Phase 4 store rather than re-derived from live signals,
    because a signal that has expired can no longer tell you it ever fired.
    ``opened_at`` is child sim-time (§4), the same clock the window uses. The
    lower bound is exclusive to match the event filter in ``create_catchup``,
    except on the very first capture, whose window starts at 0.
    """
    lower = ">=" if since_sim <= 0 else ">"
    conn = incidents_db()
    try:
        return [dict(r) for r in conn.execute(
            f"SELECT incident_id, category, summary, opened_at, status, resolved_at"
            f" FROM incidents WHERE instance_id = ? AND opened_at {lower} ?"
            " AND opened_at <= ? ORDER BY opened_at",
            (instance_id, float(since_sim), float(until_sim)),
        )]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Catch-up markers (docs/fable/catchup.md). Each catch-up snapshots the child's
# event log since the previous marker, so the summarizer above never loses
# events — it can run later, or again with a better prompt, after the child has
# been reseeded. Merging windows is Phase 6.
# ---------------------------------------------------------------------------

def _catchup_dir(instance_id: str) -> Path:
    # STATE_DIR is read at call time, like incidents_db() — a module-level
    # CATCHUP_DIR constant froze the path at import and put captures outside
    # whatever a test (or a late MANAGER_STATE_DIR) had relocated to.
    d = STATE_DIR / "catchups" / instance_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _catchup_metas(instance_id: str) -> List[Dict[str, Any]]:
    metas = []
    for f in sorted(_catchup_dir(instance_id).glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        metas.append({k: data.get(k) for k in
                      ("n", "created_at", "since_sim", "until_sim", "event_count", "summary")})
    return metas


@app.get("/admin/api/instances/{instance_id}/catchups")
def list_catchups(instance_id: str) -> List[Dict[str, Any]]:
    _inst_or_404(instance_id)
    return _catchup_metas(instance_id)


@app.get("/admin/api/instances/{instance_id}/catchups/{n}")
def get_catchup(instance_id: str, n: int) -> Dict[str, Any]:
    _inst_or_404(instance_id)
    path = _catchup_dir(instance_id) / f"{n:06d}.json"
    if not path.exists():
        raise HTTPException(404, "no such catch-up")
    return json.loads(path.read_text())


@app.post("/admin/api/instances/{instance_id}/catchups")
async def create_catchup(instance_id: str) -> Dict[str, Any]:
    inst = _inst_or_404(instance_id)
    health = await _get(inst, "/api/health")
    if health is None:
        raise HTTPException(502, "instance offline — cannot capture events")
    until_sim = float((health.get("sim") or {}).get("sim_time") or 0.0)
    metas = _catchup_metas(instance_id)
    since_sim = max((m["until_sim"] or 0.0 for m in metas), default=0.0)
    events = await _get(inst, f"/api/events?since={since_sim}") or []
    # /api/events uses >= — drop the boundary rows already in the last capture.
    events = [e for e in events if float(e.get("sim_time") or 0.0) > since_sim] \
        if metas else events
    n = (max((m["n"] for m in metas), default=0)) + 1
    record = {
        "n": n, "instance_id": instance_id, "created_at": time.time(),
        "since_sim": since_sim, "until_sim": until_sim,
        "event_count": len(events), "events": events,
        "summary": None,  # filled in by .../summarize, on demand
    }
    (_catchup_dir(instance_id) / f"{n:06d}.json").write_text(
        json.dumps(record, indent=2, default=str)
    )
    return {k: record[k] for k in
            ("n", "created_at", "since_sim", "until_sim", "event_count", "summary")}


def merge_gap(records: List[Dict[str, Any]]) -> Optional[str]:
    """Pure: the first discontinuity in a run of captures, as a human sentence.

    Captures are contiguous *by construction* — ``create_catchup`` starts each
    window where the last one ended — so a gap can only mean a capture file was
    removed. Merging across it would silently drop a window of events while
    claiming to cover the whole span, which is exactly the lie the audit trail
    exists to prevent.
    """
    for earlier, later in zip(records, records[1:]):
        until = float(earlier.get("until_sim") or 0.0)
        since = float(later.get("since_sim") or 0.0)
        if since != until:
            return (
                f"catch-ups #{earlier.get('n')} and #{later.get('n')} are not "
                f"contiguous: #{earlier.get('n')} ends at {sim_label(until)} but "
                f"#{later.get('n')} starts at {sim_label(since)}. Merging would "
                "claim to cover a window it has no events for."
            )
    return None


class MergeBody(BaseModel):
    """``from`` is a Python keyword, hence the aliases."""
    model_config = {"populate_by_name": True}

    from_n: int = Field(alias="from")
    to_n: int = Field(alias="to")


@app.post("/admin/api/instances/{instance_id}/catchups/merge")
def merge_catchups(instance_id: str, body: MergeBody) -> Dict[str, Any]:
    """Summarize captures ``from``..``to`` as one window, without saving.

    Merging is concatenation: the captures are contiguous, so their events in
    order *are* the wider window's events, and the Phase 5 summarizer needs no
    new prompt code. The result is deliberately **transient** — the originals
    are the audit trail and are never rewritten or deleted, so a merged view is
    a lens on them rather than a replacement for them.
    """
    _inst_or_404(instance_id)
    lo, hi = body.from_n, body.to_n
    if lo > hi:
        raise HTTPException(400, f"empty range: #{lo} is after #{hi}")

    records = []
    for n in range(lo, hi + 1):
        path = _catchup_dir(instance_id) / f"{n:06d}.json"
        if not path.exists():
            raise HTTPException(
                400,
                f"catch-up #{n} is missing — cannot merge across a hole in the "
                "audit trail.",
            )
        records.append(json.loads(path.read_text()))

    gap = merge_gap(records)
    if gap is not None:
        raise HTTPException(400, gap)

    events = [e for r in records for e in (r.get("events") or [])]
    since_sim = float(records[0].get("since_sim") or 0.0)
    until_sim = float(records[-1].get("until_sim") or 0.0)
    return {
        "from": lo,
        "to": hi,
        "since_sim": since_sim,
        "until_sim": until_sim,
        "event_count": len(events),
        "events": events,
        "summary": summarize_capture(
            {"events": events, "since_sim": since_sim, "until_sim": until_sim},
            incidents_in_window(instance_id, since_sim, until_sim),
        ),
    }


@app.post("/admin/api/instances/{instance_id}/catchups/{n}/summarize")
def summarize_catchup(instance_id: str, n: int) -> Dict[str, Any]:
    """Write a readable summary into capture ``n`` and return it.

    Idempotent overwrite — re-summarizing with a better prompt is expected, and
    the raw ``events`` are never touched, so nothing is lost by trying again.

    Deliberately a sync ``def``: ``LLMProvider.complete`` blocks, so FastAPI
    runs this in its threadpool instead of stalling the event loop (and with it
    every child health probe) for the length of a Gemini call.
    """
    _inst_or_404(instance_id)
    path = _catchup_dir(instance_id) / f"{n:06d}.json"
    if not path.exists():
        raise HTTPException(404, "no such catch-up")
    record = json.loads(path.read_text())
    record["summary"] = summarize_capture(
        record,
        incidents_in_window(
            instance_id,
            float(record.get("since_sim") or 0.0),
            float(record.get("until_sim") or 0.0),
        ),
    )
    path.write_text(json.dumps(record, indent=2, default=str))
    return record["summary"]


# ---------------------------------------------------------------------------
# End-of-day archive + auto-capture (docs/fable/daily-summary.md §Guidance).
#
# The sim has no day-rollover hook — day_number is derived on every clock write
# and per-day rows are materialized lazily on first read (§4) — so the manager
# detects the boundary itself by remembering the last day it saw per instance.
# ---------------------------------------------------------------------------

ROLLOVER_POLL_S = float(os.getenv("MANAGER_ROLLOVER_POLL_S", "20"))


def day_rollover(seen: Optional[int], current: Optional[int]) -> Optional[int]:
    """Pure: the sim-day that just *ended*, or ``None`` if nothing rolled over.

    - ``seen is None`` (first sighting) archives nothing: the day was already
      underway when the manager started, so there is no window to snapshot.
    - A backward jump — a reseed rewinds the clock — re-bases silently rather
      than archiving a day that is about to be replayed.
    - Several days at once (a fast sim between polls) reports only the most
      recent completed day. Snapshotting *now* three times and labelling the
      copies day 2, 3 and 4 would be fiction; the catch-up capture that runs
      alongside still covers the whole span, because its window is "everything
      since the last capture".
    """
    if current is None or seen is None or current <= seen:
        return None
    return current - 1


def last_seen_day(conn: sqlite3.Connection, instance_id: str) -> Optional[int]:
    row = conn.execute(
        "SELECT last_day FROM instance_days WHERE instance_id = ?", (instance_id,)
    ).fetchone()
    return None if row is None else int(row["last_day"])


def record_seen_day(conn: sqlite3.Connection, instance_id: str, day: int) -> None:
    conn.execute(
        "INSERT INTO instance_days (instance_id, last_day, updated_at)"
        " VALUES (?, ?, ?) ON CONFLICT(instance_id) DO UPDATE SET"
        " last_day = excluded.last_day, updated_at = excluded.updated_at",
        (instance_id, int(day), time.time()),
    )
    conn.commit()


def _summary_dir(instance_id: str) -> Path:
    # STATE_DIR read at call time, like _catchup_dir / incidents_db (§4).
    d = STATE_DIR / "summaries" / instance_id
    d.mkdir(parents=True, exist_ok=True)
    return d


async def archive_summary(instance_id: str, day: int) -> Dict[str, Any]:
    """Snapshot the portfolio summary as the record of ``day`` for ``instance_id``.

    The *portfolio* summary, not just this instance's card: the archive answers
    "what did the estate look like when this restaurant's day ended", which is
    what makes two archived days comparable. ``instance_id`` records whose
    rollover triggered it.

    Overwrites an existing file on purpose — a reseed can legitimately replay a
    sim-day, and last-write-wins beats silently keeping the pre-reseed numbers.
    """
    record = {
        "day": int(day),
        "created_at": time.time(),
        "instance_id": instance_id,
        "summary": await daily_summary(),
    }
    (_summary_dir(instance_id) / f"day-{int(day):03d}.json").write_text(
        json.dumps(record, indent=2, default=str)
    )
    return record


@app.get("/admin/api/instances/{instance_id}/summaries")
def list_summaries(instance_id: str) -> List[Dict[str, Any]]:
    """Archived end-of-day snapshots, newest day first."""
    _inst_or_404(instance_id)
    out = []
    for f in _summary_dir(instance_id).glob("day-*.json"):
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"day": data.get("day"), "created_at": data.get("created_at")})
    return sorted(out, key=lambda r: r.get("day") or 0, reverse=True)


@app.get("/admin/api/instances/{instance_id}/summaries/{day}")
def get_summary_archive(instance_id: str, day: int) -> Dict[str, Any]:
    _inst_or_404(instance_id)
    path = _summary_dir(instance_id) / f"day-{int(day):03d}.json"
    if not path.exists():
        raise HTTPException(404, f"no archived summary for day {day}")
    return json.loads(path.read_text())


async def check_rollovers() -> List[Dict[str, Any]]:
    """One sweep: capture + archive for every instance that changed sim-day.

    Returns what it did, so the test can assert "exactly once per sim-day"
    without reaching into the filesystem.
    """
    done: List[Dict[str, Any]] = []
    for instance_id, inst in list(registry.instances.items()):
        health = await _get(inst, "/api/health")
        if health is None:
            continue  # offline children simply stop being watched
        raw = (health.get("sim") or {}).get("day_number")
        if raw is None:
            continue
        current = int(raw)

        conn = incidents_db()
        try:
            ended = day_rollover(last_seen_day(conn, instance_id), current)
            record_seen_day(conn, instance_id, current)
        finally:
            conn.close()
        if ended is None:
            continue

        # The day is marked *before* the work, so a persistently failing child
        # cannot spin the watcher into re-capturing the same rollover forever —
        # a missed archive is better than an endless one. Capture before
        # archive, because the capture is what defines the window.
        try:
            capture = await create_catchup(instance_id)
        except HTTPException as exc:
            logger.warning("rollover capture for %s failed: %s", instance_id, exc.detail)
            capture = None
        await archive_summary(instance_id, ended)
        done.append({"instance_id": instance_id, "day": ended, "capture": capture})
    return done


async def _rollover_watch() -> None:
    """Background poll. Never dies: a raising sweep would silently end
    auto-capture for the rest of the process's life."""
    while True:
        await asyncio.sleep(ROLLOVER_POLL_S)
        try:
            await check_rollovers()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must outlive any one sweep
            logger.exception("rollover sweep failed")


# ---------------------------------------------------------------------------
# LLM prose briefing (docs/fable/daily-summary.md §Guidance)
# ---------------------------------------------------------------------------

BRIEFING_MAX_TOKENS = int(os.getenv("BRIEFING_MAX_TOKENS", "3000"))

_BRIEFING_SCHEMA = {
    "type": "object",
    "properties": {"briefing": {"type": "string"}},
    "required": ["briefing"],
}


def briefing_context(summary: Dict[str, Any]) -> Dict[str, Any]:
    """Pure: the slice of the daily summary worth prompting over.

    The full payload carries every restaurant card with its nested snapshot;
    the briefing only needs the headline numbers and the already-phrased action
    rows, which ``build_issues`` has written as human sentences anyway.
    """
    def rows(key: str) -> List[str]:
        return [
            f"{a.get('restaurant')}: {a.get('problem')}"
            + (f" — {a['impact']}" if a.get("impact") else "")
            for a in summary.get(key) or []
        ]

    return {
        "totals": summary.get("totals") or {},
        "restaurants": [
            {
                "name": c.get("title"),
                "status": c.get("status"),
                "sales_today": c.get("sales_today"),
                "forecast_today": c.get("forecast_today"),
                "waste_today": c.get("waste_today"),
                "tickets_waiting": c.get("orders_waiting"),
            }
            for c in summary.get("restaurants") or []
        ],
        "major_incidents": rows("major_incidents"),
        "pending_decisions": rows("pending_decisions"),
        "next_day_risks": rows("next_day_risks"),
    }


def write_briefing(
    summary: Dict[str, Any],
    complete: Optional[Any] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Six lines of portfolio prose over the daily-summary JSON.

    Same canned-fallback contract as the catch-up summarizer: a degraded
    provider yields ``error`` set and ``prose`` empty. Canned filler presented
    as a briefing is the exact failure daily-summary.md warns about.
    """
    if complete is None:
        provider, default_model = _summarizer()
        complete, model = provider.complete, model or default_model

    result = complete(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are writing the morning briefing for someone who owns "
                    "several restaurants and has not looked at them yet today.\n\n"
                    "Rules:\n"
                    "- At most 6 short lines, one per line, no bullet characters.\n"
                    "- Lead with the thing that costs money or blocks service "
                    "today; end with what needs a decision.\n"
                    "- Name restaurants. Quantify with the numbers given "
                    "(euros, counts) and no others.\n"
                    "- Plain declarative prose. No greeting, no sign-off, no "
                    "advice that the data does not support.\n"
                    "- If the estate is quiet, say so in one line rather than "
                    "padding to six.\n\n"
                    'Return JSON: {"briefing": "line one\\nline two\\n..."}'
                ),
            },
            {"role": "user", "content": json.dumps(
                briefing_context(summary), sort_keys=True, default=str)},
        ],
        json_schema=_BRIEFING_SCHEMA,
        max_tokens=BRIEFING_MAX_TOKENS,
        use_site="portfolio_briefing",
        model=model,
    )

    prose = result.get("briefing") if isinstance(result, dict) else None
    if (not isinstance(result, dict) or result.get("note") == CANNED_NOTE
            or not isinstance(prose, str) or not prose.strip()):
        return {
            "generated_at": time.time(),
            "model": model or "",
            "error": (
                f"The briefing was not written: {model or 'the model'} fell back "
                "to canned output. Check GEMINI_REASONER_MODEL and the Vertex "
                "credentials — a bad model id degrades silently."
            ),
            "prose": "",
        }
    return {
        "generated_at": time.time(),
        "model": model or "",
        "error": None,
        "prose": prose.strip(),
    }


@app.post("/admin/api/briefing")
async def briefing() -> Dict[str, Any]:
    """Write the prose briefing over a freshly-read portfolio summary.

    POST, and only ever on demand: ``GET /admin/api/summary`` is polled every
    30s by the dashboard, and hanging an LLM call off that poll would bill a
    Gemini call twice a minute forever.

    The fan-out is async but ``write_briefing`` blocks, so it goes to a thread —
    the same reason ``summarize_catchup`` is a sync ``def``.
    """
    snapshot = await daily_summary()
    return await asyncio.to_thread(write_briefing, snapshot)


# ---------------------------------------------------------------------------
# Reverse proxy: /i/{instance_id}/... → child instance (HTTP + WebSocket)
# ---------------------------------------------------------------------------

_HOP_HEADERS = {"content-length", "transfer-encoding", "connection", "host"}


@app.api_route("/i/{instance_id}/{path:path}",
               methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
async def http_proxy(instance_id: str, path: str, request: Request) -> Response:
    inst = _inst_or_404(instance_id)
    body = await request.body()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in _HOP_HEADERS}
    try:
        resp = await client().request(
            request.method,
            f"{_base_url(inst)}/{path}",
            params=request.query_params,
            content=body,
            headers=headers,
            timeout=120.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"instance {instance_id} unreachable: {exc}") from exc
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
    )


@app.websocket("/i/{instance_id}/{path:path}")
async def ws_proxy(websocket: WebSocket, instance_id: str, path: str) -> None:
    inst = registry.instances.get(instance_id)
    if inst is None:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    query = websocket.url.query
    uri = f"ws://127.0.0.1:{inst['port']}/{path}" + (f"?{query}" if query else "")
    try:
        upstream = await ws_connect(uri, max_size=None)
    except OSError:
        await websocket.close(code=4502)
        return

    async def client_to_upstream() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("text") is not None:
                await upstream.send(message["text"])
            elif message.get("bytes") is not None:
                await upstream.send(message["bytes"])

    async def upstream_to_client() -> None:
        async for message in upstream:
            if isinstance(message, (bytes, bytearray)):
                await websocket.send_bytes(bytes(message))
            else:
                await websocket.send_text(message)

    tasks = [asyncio.create_task(client_to_upstream()),
             asyncio.create_task(upstream_to_client())]
    try:
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
    finally:
        await upstream.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass  # already closed by the client
