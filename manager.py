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
  incidents, daily summary, and catch-up markers (docs/fable/catchup.md).

Run:  uvicorn manager:app --port 8100
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from pydantic import BaseModel
from websockets.asyncio.client import connect as ws_connect

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
STATE_DIR = Path(os.getenv("MANAGER_STATE_DIR", str(ROOT / "dbdata")))
REGISTRY_PATH = STATE_DIR / "manager_registry.json"
CATCHUP_DIR = STATE_DIR / "catchups"

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
) -> List[Dict[str, Any]]:
    """Turn raw live signals + at-risk plan items into merged, human-readable
    incidents: similar items are batched (per supplier / per signal type) and
    phrased for a manager, never as raw status codes. Pure + deterministic
    (tested in tests/test_manager.py)."""

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
            })
        elif sig_type == "FOOD_SAFETY_CHECK":
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
    yield
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
        signals, plan, ingredients = await asyncio.gather(
            _get(inst, "/api/signals?status=live"),
            _get(inst, "/api/track-b/procurement/plan"),
            _get(inst, "/api/ingredients"),
        )
        names = {int(i["id"]): i.get("name") for i in ingredients or [] if i.get("id")}
        merged = merge_incidents(
            signals or [], (plan or {}).get("items") or [], names
        )
        return [{**r, "instance_id": inst["id"], "restaurant": inst["title"]}
                for r in merged]

    nested = await asyncio.gather(*(fetch(i) for i in registry.instances.values()))
    rows = [r for batch in nested for r in batch]
    rows.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return {
        "incidents": rows,
        # Every category has a detector now (Phase 3) — kept in the response so
        # the UI contract is stable and a future gap can be declared again.
        "unavailable_categories": [],
    }


# ---------------------------------------------------------------------------
# Daily summary
# ---------------------------------------------------------------------------

@app.get("/admin/api/summary")
async def daily_summary() -> Dict[str, Any]:
    cards = list(await asyncio.gather(
        *(_instance_overview(i) for i in registry.instances.values())
    ))

    async def waste_today(inst: Dict[str, Any], card: Dict[str, Any]) -> float:
        if not card["online"]:
            return 0.0
        sim_time = float((card.get("sim") or {}).get("sim_time") or 0.0)
        day_start = (sim_time // SECONDS_PER_DAY) * SECONDS_PER_DAY
        rows = await _get(inst, "/api/waste") or []
        return round(sum(
            float(r.get("cost") or 0.0) for r in rows
            if float(r.get("sim_time") or 0.0) >= day_start
        ), 2)

    waste = await asyncio.gather(*(
        waste_today(registry.instances[c["id"]], c) for c in cards
    ))
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
        "next_day_risks": [a for a in actions if a["kind"] == "stock"],
    }


# ---------------------------------------------------------------------------
# Catch-up markers (docs/fable/catchup.md — infrastructure only; the readable
# LLM summary + merge come later. Each catch-up snapshots the child's event
# log since the previous marker, so the future summarizer never loses events.)
# ---------------------------------------------------------------------------

def _catchup_dir(instance_id: str) -> Path:
    d = CATCHUP_DIR / instance_id
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
        "summary": None,  # future: LLM-generated readable summary
    }
    (_catchup_dir(instance_id) / f"{n:06d}.json").write_text(
        json.dumps(record, indent=2, default=str)
    )
    return {k: record[k] for k in
            ("n", "created_at", "since_sim", "until_sim", "event_count", "summary")}


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
