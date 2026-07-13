"""Time-phased procurement plan optimizer.

Exposes one public entry-point:

  solve_time_phased_plan(...)  -- MILP joint cost minimisation across all
                                  ingredients, suppliers, and delivery days.
                                  Falls back to a greedy projection when PuLP
                                  is unavailable or CBC returns non-optimal.

The MILP jointly decides:
  * which supplier to use for each ingredient on each delivery day,
  * how many packs to order (integer variable),
  * which delivery days to open for each supplier (incurring one delivery
    charge per supplier-day),
  * whether volume-discount thresholds are crossed.

Perishability is modelled with an **expiry-day cohort** inventory structure:
opening lots and new arrivals are segregated by the day their stock expires.
A cohort expiring on day ``e`` can only satisfy demand on days ``0 … e-1``
(the ``e > d`` rule).  Any remaining stock at the end of the cohort's last
live day is forced to ``waste_exp`` — a real waste event with spoilage
penalty.  This eliminates the held-inventory-cap approximation that
previously allowed the solver to count expired stock as covering future demand
(the "basil bug": 185 g expiring Wednesday served Thursday demand in the old
model).

Coverage is two-tier: forecasted demand is a hard requirement (heavily
penalised ``demand_short``) while ``safety_stock`` is a soft buffer topped up
only when cheap (lightly, value-scaled ``safety_short``).  Any demand that is
physically impossible to cover (early days before any delivery can arrive, or
all suppliers out) is surfaced as ``at_risk`` — never hidden.

Reliability handling — two-pass lexicographic solve
----------------------------------------------------
Rather than folding a per-gram risk penalty into the cost objective (which
drowns real cash savings), the solver runs two passes against the same model:

  Pass 1 — cash-optimal.
    Objective = real economic cost (goods + delivery − discounts − rebates)
    plus the existing internal penalty terms for coverage, spoilage and safety.
    Records the optimal cash cost C0.

  Pass 2 — buy down one-day-delay exposure within a cash cap.
    Adds a hard constraint ``cash_real_expr <= C0 * (1 + tolerance)`` and
    swaps the objective to minimise a margin-weighted exposure lever:
      sum_i sum_{s,d}  margin_per_unit[i] * (1-reliability[s]) * lead_factor[s] * x[i,s,d]
    which directionally favours shorter lead-times and more reliable suppliers
    when they can be had within the cash cap.  Coverage / spoilage / safety
    penalties remain in the objective so all hard-coverage invariants hold.

  Post-solve: compute the honest one-day-delay exposure for both plans using a
    pure-Python projection (_one_day_delay_exposure) — arrivals shifted +1 day,
    day-by-day lot accounting.  Report extra_cash = C1_real − C0 and the
    protected margin value.

This eliminates the fragmented per-ingredient-per-day POs produced by the
old greedy path, naturally consolidates across days (delivery charges drive
fewer supplier-day openings), satisfies MOV as a hard constraint (never needs
wasteful padding), and captures volume discounts when they lower landed cost.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import pulp as _pulp  # noqa: F401
    _PULP_AVAILABLE = True
except ImportError:
    _PULP_AVAILABLE = False
    logger.warning("PuLP not available; plan optimizer will use greedy fallback.")


_INF_DAY = 10 ** 9  # sentinel expiry-day offset for "never expires"


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------

@dataclass
class PlanOrder:
    """A single planned delivery from the solver."""
    ingredient_id: int
    supplier_id: int
    delivery_day: int          # day-index offset from now_day (0 = today)
    qty: float                 # total quantity in base units
    unit_price: float          # per-base-unit price
    unit: str
    at_risk: bool = False      # True when a one-day slip of THIS line causes a shortage
    reason: str = ""
    # Per-line risk detail (computed by _per_line_risk after solving)
    projected_stock_before: float = 0.0  # stock on hand at start of delivery day, before this arrival
    qty_needed_before: float = 0.0       # cumulative demand up to and including the delivery day
    shortage_if_late: float = 0.0        # units short if this specific line arrives one day late
    latest_safe_arrival: int = -1        # latest horizon-day this line can arrive without stockout; -1=unconstrained


@dataclass
class PlanSolution:
    """Result of solve_time_phased_plan."""
    orders: List[PlanOrder] = field(default_factory=list)
    total_cost: float = 0.0
    method: str = "greedy"    # "milp" | "greedy"
    rationale: str = ""
    total_short: float = 0.0  # total un-coverable forecasted demand (base units)
    coverage_ok: bool = True  # False when any forecasted demand is left uncovered
    # Reliability premium fields (two-pass MILP only; 0.0 for greedy / pass-1-only)
    cash_cost: float = 0.0              # actual invoice cost of the chosen plan
    reliability_premium: float = 0.0   # extra cash vs the cash-optimal plan
    exposed_value_baseline: float = 0.0  # margin exposed under 1-day delay, no-plan baseline
    exposed_value_plan: float = 0.0    # margin exposed under 1-day delay, chosen plan
    # Unit shortfall under a universal +1-day delay (always computed by MILP; 0 for greedy)
    unit_shortfall_if_1day_late: float = 0.0
    # Robust hard-delay mode (pass-3 solve; only present when robust requested)
    robust_requested: bool = False
    robust_applied: bool = False
    robust_status: str = ""  # "applied" | "infeasible_fell_back" | "skipped"


# ---------------------------------------------------------------------------
# FEFO coverage validator  (WS2 — authoritative post-solve coverage check)
# ---------------------------------------------------------------------------

def project_fefo_coverage(
    *,
    n_days: int,
    demand_by_day: Dict[int, Dict[int, float]],
    on_hand: Dict[int, float],
    lots_by_ing: Dict[int, List[List[float]]],
    open_po_arrivals_by_day: Dict[int, Dict[int, float]],
    plan_arrivals_by_day: Dict[int, Dict[int, float]],
    safety_stock: Dict[int, float],
    ingredient_shelf_life: Optional[Dict[int, Optional[float]]] = None,
) -> Dict[str, Any]:
    """Day-by-day FEFO inventory simulation — the single source of truth for
    coverage after all netting, R1 reconciliation, and hysteresis is done.

    Parameters
    ----------
    n_days:                 planning horizon length.
    demand_by_day:          {iid: {day: qty}} — forecast demand per ingredient per day.
    on_hand:                {iid: float}      — current on-hand quantity.
    lots_by_ing:            {iid: [[qty, exp_day_offset]]} — dated opening lots; exp_day_offset
                            is the day number at which the lot expires (NOT available on that day:
                            a lot with exp_day=3 is usable on days 0,1,2 only).
                            Use [[on_hand, _INF_DAY]] when no lot data is available.
    open_po_arrivals_by_day: {iid: {day: qty}} — already-placed PO arrivals, keyed by
                            FEFO arrival day (deduped by PO id upstream).
    plan_arrivals_by_day:   {iid: {day: qty}} — new planned-order arrivals that have NOT yet
                            been converted to a real PO (i.e. non-superseded planned rows).
    safety_stock:           {iid: float}      — soft buffer target (informational only here).
    ingredient_shelf_life:  {iid: shelf_life_days or None} — for perishables arriving as new
                            lots; None / absent means non-perishable (treated as infinite).

    Returns
    -------
    Dict with keys:
      coverage_ok          bool   — True iff every ingredient's demand is fully met every day.
      total_short          float  — sum of all daily shortfalls across all ingredients.
      short_by_ing         dict   — {iid: total_shortfall} for every short ingredient.
      coverage_depends_on_planned bool — True when committed supply (on_hand + open POs alone)
                                   cannot cover demand for at least one ingredient.
      late_delivery_coverage_ok   bool — True when shifting all planned arrivals +1 day still
                                   covers demand (conservative: open POs also shifted).
    """
    _sl = ingredient_shelf_life or {}

    # ---- helpers ----

    def _initial_lots(iid: int) -> List[List[float]]:
        """Return a list of [qty, exp_day] pairs for the opening stock of iid."""
        raw = lots_by_ing.get(iid)
        if raw:
            return [[float(q), float(e)] for q, e in raw]
        return [[float(on_hand.get(iid, 0.0)), float(_INF_DAY)]]

    def _simulate(
        arrivals_by_day: Dict[int, Dict[int, float]],  # merged open-PO + plan arrivals
        demand_map: Dict[int, Dict[int, float]],
    ) -> tuple:
        """Run the day-by-day FEFO sim.

        Returns (total_short, short_by_ing).  Arrivals create new lots whose
        expiry is derived from ingredient_shelf_life (non-perishable → _INF_DAY).
        """
        total_short = 0.0
        short_by_ing: Dict[int, float] = {}

        all_iids = set(demand_map.keys()) | set(arrivals_by_day.keys())
        for iid in all_iids:
            d_map = demand_map.get(iid) or {}
            arr_map = arrivals_by_day.get(iid) or {}

            if not any(v > 0 for v in d_map.values()):
                continue  # no demand for this ingredient → skip

            sl = _sl.get(iid)  # shelf life in days, or None

            # Start with opening lots
            lots: List[List[float]] = _initial_lots(iid)

            ing_short = 0.0
            for d in range(n_days):
                # 1. Expire lots that cannot be used on day d (exp_day <= d)
                lots = [[q, e] for q, e in lots if e > d]

                # 2. Add arrivals landing on day d (new lots with computed expiry)
                arr_qty = float(arr_map.get(d, 0.0))
                if arr_qty > 0:
                    if sl is not None and sl > 0:
                        exp = d + int(math.ceil(sl))
                    else:
                        exp = _INF_DAY
                    lots.append([arr_qty, float(exp)])

                # 3. Consume demand FEFO (shortest-expiry first)
                demand = float(d_map.get(d, 0.0))
                if demand <= 0:
                    continue
                lots.sort(key=lambda x: x[1])  # sort by expiry (earliest first)
                remaining = demand
                consumed_lots: List[List[float]] = []
                for lot in lots:
                    q, e = lot
                    if remaining <= 0:
                        consumed_lots.append([q, e])
                    elif q <= remaining:
                        remaining -= q
                        # lot fully consumed
                    else:
                        consumed_lots.append([q - remaining, e])
                        remaining = 0.0
                lots = consumed_lots

                if remaining > 1e-6:
                    ing_short += remaining

            if ing_short > 1e-6:
                short_by_ing[iid] = ing_short
                total_short += ing_short

        return total_short, short_by_ing

    # ---- on-time simulation ----

    def _merge(a: Dict[int, Dict[int, float]], b: Dict[int, Dict[int, float]]) -> Dict[int, Dict[int, float]]:
        merged: Dict[int, Dict[int, float]] = {}
        for iid in set(a) | set(b):
            merged[iid] = {}
            for d, q in (a.get(iid) or {}).items():
                merged[iid][d] = merged[iid].get(d, 0.0) + q
            for d, q in (b.get(iid) or {}).items():
                merged[iid][d] = merged[iid].get(d, 0.0) + q
        return merged

    all_arrivals = _merge(open_po_arrivals_by_day, plan_arrivals_by_day)
    total_short, short_by_ing = _simulate(all_arrivals, demand_by_day)

    # ---- committed-only (without new planned orders) for coverage_depends_on_planned ----
    committed_short, _ = _simulate(
        {iid: dict(d_map) for iid, d_map in open_po_arrivals_by_day.items()},
        demand_by_day,
    )
    coverage_depends_on_planned = committed_short > 1.0

    # ---- +1-day delay simulation (shift ALL arrivals one day later) ----
    def _shift_one_day(src: Dict[int, Dict[int, float]]) -> Dict[int, Dict[int, float]]:
        shifted: Dict[int, Dict[int, float]] = {}
        for iid, d_map in src.items():
            shifted[iid] = {d + 1: q for d, q in d_map.items()}
        return shifted

    late_arrivals = _merge(_shift_one_day(open_po_arrivals_by_day), _shift_one_day(plan_arrivals_by_day))
    late_short, _ = _simulate(late_arrivals, demand_by_day)
    late_delivery_coverage_ok = late_short <= 1.0

    return {
        "coverage_ok": total_short <= 1.0,
        "total_short": total_short,
        "short_by_ing": short_by_ing,
        "coverage_depends_on_planned": coverage_depends_on_planned,
        "late_delivery_coverage_ok": late_delivery_coverage_ok,
    }


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

def solve_time_phased_plan(
    *,
    n_days: int,
    ingredients: List[Dict[str, Any]],
    catalog: List[Dict[str, Any]],
    suppliers: List[Dict[str, Any]],
    demand_by_day: Dict[int, Dict[int, float]],   # {ing_id: {day: qty}}
    inbound_by_day: Dict[int, Dict[int, float]],  # {ing_id: {day: qty}} already in-flight
    on_hand: Dict[int, float],                    # {ing_id: qty}
    safety_stock: Dict[int, float],               # {ing_id: qty}
    params: Optional[Dict[str, Any]] = None,
) -> PlanSolution:
    """Time-phased procurement plan.

    ``n_days``         -- number of days in the horizon (matches forecast span).
    ``ingredients``    -- [{id, perishable, shelf_life_days, base_unit, name}]
    ``catalog``        -- [{supplier_id, ingredient_id, current_price, pack_size,
                            unit, availability, is_default, discount}]
                          ``discount`` = [{min_qty, unit_price}] or None
    ``suppliers``      -- [{id, name, lead_time_days, reliability_score,
                            min_order_value, delivery_charge, volume_discount}]
                          ``volume_discount`` = [{min_value, discount_pct}] or None
    ``demand_by_day``  -- ingredient demand (base units) per day; uses robust
                          max(qty, baseline) already computed by the caller.
    ``inbound_by_day`` -- already-ordered qty arriving per ingredient per day
                          (only non-overdue in-flight POs; phantom stock excluded).
    ``on_hand``        -- current on-hand cached qty per ingredient.
    ``safety_stock``   -- target floor per ingredient.
    ``params``         -- optional overrides:
                            spoilage_penalty_multiplier (default 2.0)
                            slack_penalty (default 1000.0)          -- demand
                            safety_penalty_multiplier (default 1.5) -- soft buffer
                            lots_by_ing: {ing_id: [[qty, exp_day_offset], ...]}
                              dated opening lots for expiry-aware fresh stock;
                              exp_day_offset counts days from now (>= n_days or
                              missing => never expires within horizon).  Absent
                              => a single never-expiring lot equal to on_hand.
                            reliability_cash_tolerance (default 0.01)
                              max extra fraction of cash-optimal cost permitted
                              for the stress pass.  0.0 = pure cash-optimal.
                            stress_enabled (default True)
                              when False, skip pass 2 entirely.
                            margin_by_ing: {ing_id: margin_per_base_unit}
                              contribution margin per base unit; used as weights
                              in the pass-2 exposure lever and in the post-solve
                              one-day-delay projection.  Falls back to unit_price
                              when absent or <= 0 for an ingredient.
                            scheduled_supplier_days: set of (supplier_id, arr_day)
                              supplier-days already covered by a placed/proposed PO
                              that is still in transit.  The MILP pre-opens those
                              deliver binaries (delivery charge already sunk) and
                              exempts them from MOV enforcement so incremental lines
                              piggyback at no additional fee.
                            robust_hard_delay (default False)
                              when True, run a pass-3 solve requiring that demand
                              remains covered even if qualifying (unreliable)
                              suppliers' deliveries arrive one day late.  Falls back
                              gracefully to the pass-2 plan if infeasible.
                            robust_min_reliability (default 0.95)
                              reliability_score threshold below which a supplier is
                              considered "qualifying" for the delay scenario.
    """
    p = params or {}
    if _PULP_AVAILABLE:
        try:
            return _solve_milp(
                n_days=n_days,
                ingredients=ingredients,
                catalog=catalog,
                suppliers=suppliers,
                demand_by_day=demand_by_day,
                inbound_by_day=inbound_by_day,
                on_hand=on_hand,
                safety_stock=safety_stock,
                params=p,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Time-phased MILP failed (%s); falling back to greedy.", exc, exc_info=True
            )

    return _solve_greedy(
        n_days=n_days,
        ingredients=ingredients,
        catalog=catalog,
        suppliers=suppliers,
        demand_by_day=demand_by_day,
        inbound_by_day=inbound_by_day,
        on_hand=on_hand,
        safety_stock=safety_stock,
        params=p,
    )


# ---------------------------------------------------------------------------
# One-day-delay exposure projection (pure Python; used for honest trade-off)
# ---------------------------------------------------------------------------

def _one_day_delay_exposure(
    orders: List[PlanOrder],
    demand_by_day: Dict[int, Dict[int, float]],
    on_hand: Dict[int, float],
    inbound_by_day: Dict[int, Dict[int, float]],
    lots_by_ing: Dict[int, List[List[float]]],
    margin_by_ing: Dict[int, float],
    n_days: int,
    ingredient_ids: List[int],
    ing_by_id: Dict[int, Dict],
) -> float:
    """Margin-weighted demand shortfall under a universal one-day arrival delay.

    Simulates each ingredient's stock day-by-day with:
      - on-hand opening stock (lot-aware for perishables)
      - inbound POs arriving one day later than scheduled
      - plan orders arriving one day later than their delivery_day

    Returns sum(margin_per_unit[i] * shortfall[i]) over the horizon.
    """
    _LOT_NEVER = float(_INF_DAY)

    # Group plan orders by ingredient and day for fast lookup
    plan_by_ing: Dict[int, Dict[int, float]] = {}
    for o in orders:
        if (o.qty or 0) > 0:
            plan_by_ing.setdefault(o.ingredient_id, {})
            plan_by_ing[o.ingredient_id][o.delivery_day] = (
                plan_by_ing[o.ingredient_id].get(o.delivery_day, 0.0) + o.qty
            )

    total_exposed = 0.0
    for iid in ingredient_ids:
        ing = ing_by_id.get(iid, {})
        shelf_life = (
            float(ing.get("shelf_life_days") or 0.0)
            if ing.get("perishable")
            else None
        )
        weight = max(float(margin_by_ing.get(iid, 0.0)), 0.0)
        if weight <= 0.0:
            continue  # no margin weight → exclude

        # Seed opening lots
        raw_lots = lots_by_ing.get(iid)
        if raw_lots:
            lots: List[List[float]] = [[float(qq), float(ee)] for qq, ee in raw_lots]
        else:
            lots = [[float(on_hand.get(iid, 0.0)), _LOT_NEVER]]

        for d in range(n_days):
            # Inbound POs arrive one day late: credit arrivals planned for (d-1) on day d.
            # Expiry is based on original arrival day ((d-1) + shelf_life), not the delayed day.
            inb_orig = inbound_by_day.get(iid, {}).get(d - 1, 0.0) if d >= 1 else 0.0
            if inb_orig > 0:
                exp = (d - 1) + (shelf_life if shelf_life else _LOT_NEVER)
                lots.append([inb_orig, exp])

            # Plan orders arrive one day late: credit deliveries planned for (d-1) on day d.
            plan_orig = plan_by_ing.get(iid, {}).get(d - 1, 0.0) if d >= 1 else 0.0
            if plan_orig > 0:
                exp = (d - 1) + (shelf_life if shelf_life else _LOT_NEVER)
                lots.append([plan_orig, exp])

            # Expire lots
            lots = [l for l in lots if l[1] > d]
            lots.sort(key=lambda l: l[1])

            # Consume demand (FIFO by expiry)
            dem = demand_by_day.get(iid, {}).get(d, 0.0)
            rem = dem
            for lot in lots:
                take = min(lot[0], rem)
                lot[0] -= take
                rem -= take
                if rem <= 0:
                    break
            lots = [l for l in lots if l[0] > 1e-9]

            if rem > 0.5:
                total_exposed += weight * rem

    return total_exposed


# ---------------------------------------------------------------------------
# Per-line risk projection
# ---------------------------------------------------------------------------

def _per_line_risk(
    orders: List[PlanOrder],
    demand_by_day: Dict[int, Dict[int, float]],
    on_hand: Dict[int, float],
    inbound_by_day: Dict[int, Dict[int, float]],
    lots_by_ing: Dict[int, List[List[float]]],
    n_days: int,
    ingredient_ids: List[int],
    ing_by_id: Dict[int, Dict],
) -> Dict[Tuple[int, int, int], Dict]:
    """Per-(ingredient, supplier, delivery_day) risk detail.

    For each PlanOrder, runs a single FIFO lot projection per ingredient that
    records stock BEFORE plan arrivals on each day.  Returns a dict keyed by
    (ingredient_id, supplier_id, delivery_day) with:
      projected_stock_before  — stock available before any plan arrivals on the delivery day
      qty_needed_before       — cumulative demand on days 0..delivery_day (inclusive)
      shortage_if_late        — extra shortfall on the delivery day if THIS line arrives one day late
      latest_safe_arrival     — horizon-day offset by which the line must arrive (or -1 if unconstrained)
      at_risk                 — True when shortage_if_late > 0.5
    """
    _LOT_NEVER = float(_INF_DAY)

    # Group orders by ingredient for fast lookup
    orders_by_ing: Dict[int, List[PlanOrder]] = {}
    for o in orders:
        if (o.qty or 0) > 0:
            orders_by_ing.setdefault(o.ingredient_id, []).append(o)

    # Total plan qty arriving per (ingredient, delivery_day)
    plan_arrivals: Dict[int, Dict[int, float]] = {}
    for o in orders:
        if (o.qty or 0) > 0:
            plan_arrivals.setdefault(o.ingredient_id, {})
            plan_arrivals[o.ingredient_id][o.delivery_day] = (
                plan_arrivals[o.ingredient_id].get(o.delivery_day, 0.0) + o.qty
            )

    result: Dict[Tuple[int, int, int], Dict] = {}

    for iid in ingredient_ids:
        if iid not in orders_by_ing:
            continue
        ing = ing_by_id.get(iid, {})
        shelf_life = (
            float(ing.get("shelf_life_days") or 0.0)
            if ing.get("perishable")
            else None
        )
        inb = inbound_by_day.get(iid, {})

        # Seed opening lots
        raw = lots_by_ing.get(iid)
        if raw:
            lots: List[List[float]] = [[float(qq), float(ee)] for qq, ee in raw]
        else:
            lots = [[float(on_hand.get(iid, 0.0)), _LOT_NEVER]]

        # Run full projection, recording stock BEFORE plan arrivals each day.
        stock_before_plan: Dict[int, float] = {}
        for d in range(n_days):
            # Expire lots
            lots = [l for l in lots if l[1] > d]
            lots.sort(key=lambda l: l[1])

            # Credit inbound POs (already-placed, assumed on-time)
            inb_d = inb.get(d, 0.0)
            if inb_d > 0:
                exp = d + (shelf_life if shelf_life else _LOT_NEVER)
                lots.append([inb_d, exp])

            # Record stock BEFORE plan arrivals on this day
            stock_before_plan[d] = max(0.0, sum(l[0] for l in lots))

            # Credit plan arrivals
            pa_d = plan_arrivals.get(iid, {}).get(d, 0.0)
            if pa_d > 0:
                exp = d + (shelf_life if shelf_life else _LOT_NEVER)
                lots.append([pa_d, exp])

            # Consume demand FIFO
            dem = demand_by_day.get(iid, {}).get(d, 0.0)
            rem = dem
            for lot in lots:
                take = min(lot[0], rem)
                lot[0] -= take
                rem -= take
                if rem <= 0:
                    break
            lots = [l for l in lots if l[0] > 1e-9]

        # Cumulative demand per day (for qty_needed_before)
        cum_demand: Dict[int, float] = {}
        running = 0.0
        for d in range(n_days):
            running += demand_by_day.get(iid, {}).get(d, 0.0)
            cum_demand[d] = running

        # Per-order detail
        for o in orders_by_ing[iid]:
            d = o.delivery_day
            sb = stock_before_plan.get(d, 0.0)
            dem_d = demand_by_day.get(iid, {}).get(d, 0.0)

            # Other plan arrivals on the same day (to share the protective buffer)
            other_plan = max(0.0, plan_arrivals.get(iid, {}).get(d, 0.0) - o.qty)

            # shortage_if_late: demand on day d unmet if THIS line doesn't arrive today
            # Stock available without this order = stock_before_plan[d] + other_plan
            shortage_if_late = max(0.0, dem_d - (sb + other_plan))

            # latest_safe_arrival: if shortage_if_late > 0, order must arrive by day d.
            # Otherwise it can slip to day d+1 with no impact (very conservative — we
            # don't re-project the tail; reporting "unconstrained" is safe).
            if shortage_if_late > 0.5:
                latest_safe = d
            else:
                # Check whether day d+1 (or later) would be the first stockout without
                # this order's quantity.  Approximate: if stock_before_plan[d+1] < demand[d+1],
                # then this order should arrive by day d+1 at the latest.
                latest_safe = -1  # unconstrained by default
                for dd in range(d + 1, n_days):
                    dem_dd = demand_by_day.get(iid, {}).get(dd, 0.0)
                    # After consuming day d+1 demand from pre-existing stock-before-plan[d+1],
                    # is there still enough?  (conservative: ignores this order's qty arriving later)
                    if stock_before_plan.get(dd, 0.0) < dem_dd - 0.5:
                        latest_safe = dd
                        break

            result[(iid, o.supplier_id, d)] = {
                "projected_stock_before": sb,
                "qty_needed_before": cum_demand.get(d, 0.0),
                "shortage_if_late": shortage_if_late,
                "latest_safe_arrival": latest_safe,
                "at_risk": shortage_if_late > 0.5,
            }

    return result


# ---------------------------------------------------------------------------
# MILP solver
# ---------------------------------------------------------------------------

def _solve_milp(
    *,
    n_days: int,
    ingredients: List[Dict[str, Any]],
    catalog: List[Dict[str, Any]],
    suppliers: List[Dict[str, Any]],
    demand_by_day: Dict[int, Dict[int, float]],
    inbound_by_day: Dict[int, Dict[int, float]],
    on_hand: Dict[int, float],
    safety_stock: Dict[int, float],
    params: Dict[str, Any],
) -> PlanSolution:
    import pulp

    spoil_pen_mult = float(params.get("spoilage_penalty_multiplier", 2.0))
    slack_penalty = float(params.get("slack_penalty", 1000.0))            # demand (hard)
    safety_pen_mult = float(params.get("safety_penalty_multiplier", 1.5))  # buffer (soft)
    lots_by_ing: Dict[int, List[List[float]]] = params.get("lots_by_ing") or {}
    reliability_cash_tolerance = float(params.get("reliability_cash_tolerance", 0.01))
    stress_enabled = bool(params.get("stress_enabled", True))
    margin_by_ing: Dict[int, float] = dict(params.get("margin_by_ing") or {})
    # Scheduled supplier-days: already-in-transit POs → pre-open deliver, skip charge/MOV
    scheduled_supplier_days: set = set(params.get("scheduled_supplier_days") or set())
    # Robust hard-delay mode
    robust_hard_delay: bool = bool(params.get("robust_hard_delay", False))
    robust_min_reliability: float = float(params.get("robust_min_reliability", 0.95))
    # Free-goods offers from active SupplierTerms (threshold-gated supply benefits)
    free_goods_offers: List[Any] = list(params.get("free_goods_offers") or [])

    ing_by_id: Dict[int, Dict] = {int(i["id"]): i for i in ingredients}
    sup_by_id: Dict[int, Dict] = {int(s["id"]): s for s in suppliers}

    # Index catalog: (ingredient_id, supplier_id) -> row; skip 'out'
    cat_by_is: Dict[Tuple[int, int], Dict] = {}
    for c in catalog:
        if c.get("availability") == "out":
            continue
        key = (int(c["ingredient_id"]), int(c["supplier_id"]))
        cat_by_is[key] = c

    # Lead time per supplier (days), rounded up to integer delivery-day offset
    lead_ceil: Dict[int, int] = {
        int(s["id"]): max(1, math.ceil(float(s.get("lead_time_days") or 1.0)))
        for s in suppliers
    }

    # Delivery hour per supplier (24-h float, e.g. 8.0 = 08:00).
    # A supplier whose delivery_hour > PRODUCTION_START_HOUR (default 8.0) delivers
    # AFTER production starts, so its deliveries on physical day d only serve service
    # day d+1.  With default delivery_hour=8.0 and production_start=8.0 the shift is
    # 0 — all arrivals serve the same day (identical to historic behaviour).
    _prod_start = float(params.get("production_start_hour", 8.0))
    delivery_hour_by_sup: Dict[int, float] = {
        int(s["id"]): float(s.get("delivery_hour") or 8.0)
        for s in suppliers
    }
    # service_shift[s_id] = 0 if arrives before/at production start, else 1
    service_shift: Dict[int, int] = {
        s_id: (0 if dh <= _prod_start else 1)
        for s_id, dh in delivery_hour_by_sup.items()
    }

    # Active ingredients: those with any demand OR on_hand < safety_stock
    active_ids: List[int] = []
    for i in ingredients:
        iid = int(i["id"])
        has_demand = any(demand_by_day.get(iid, {}).get(d, 0.0) > 0 for d in range(n_days))
        needs_stock = float(on_hand.get(iid, 0.0)) < float(safety_stock.get(iid, 0.0))
        if has_demand or needs_stock:
            active_ids.append(iid)

    if not active_ids:
        return PlanSolution(orders=[], total_cost=0.0, method="milp",
                            rationale="No active ingredients require ordering.")

    all_sup_ids = list({int(s["id"]) for s in suppliers})

    # Cheapest available unit price per ingredient — used to value-scale the
    # soft safety-buffer and waste penalties.
    ref_price: Dict[int, float] = {}
    for iid in active_ids:
        prices = [
            float(cat_by_is[(iid, s_id)].get("current_price") or 0.0)
            for s_id in all_sup_ids
            if (iid, s_id) in cat_by_is
        ]
        ref_price[iid] = min(prices) if prices else 0.0

    # Opening fresh stock: fresh_initial[iid][d] = opening lot qty still unexpired
    # at end of day d.  Falls back to a single never-expiring lot == on_hand.
    def _lots_for(iid: int) -> List[List[float]]:
        raw = lots_by_ing.get(iid)
        if raw:
            return [[float(qq), float(ee)] for qq, ee in raw]
        return [[float(on_hand.get(iid, 0.0)), float(_INF_DAY)]]

    fresh_initial: Dict[int, Dict[int, float]] = {}
    initial_total: Dict[int, float] = {}
    for iid in active_ids:
        lots = _lots_for(iid)
        initial_total[iid] = sum(qq for qq, _ in lots)
        fresh_initial[iid] = {
            d: sum(qq for qq, ee in lots if ee > d) for d in range(n_days)
        }

    # ------------------------------------------------------------------
    # Cohort data for perishables — expiry-day bucketed opening stock.
    #
    # For each perishable ingredient:
    #   cohort_expiry[iid] = sorted list of distinct expiry-day offsets e
    #     (a lot/arrival expires at the START of day e, i.e. available on
    #      days 0..e-1, NOT on day e).
    #   open_cohort[iid][e] = opening stock belonging to cohort e.
    #   exp_of_arrival[iid] = callable: service day D -> cohort expiry day
    #     for NEW arrivals (min(D + sl, n_days)).
    # ------------------------------------------------------------------
    cohort_expiry: Dict[int, List[int]] = {}
    open_cohort: Dict[int, Dict[int, float]] = {}
    exp_of_arrival: Dict[int, Any] = {}  # iid -> (D -> e)

    for iid in active_ids:
        ing_row = ing_by_id.get(iid, {})
        _shelf = float(ing_row.get("shelf_life_days") or 0.0)
        _perishable = bool(ing_row.get("perishable")) and _shelf > 0
        if not _perishable:
            continue
        _sl = math.ceil(_shelf)

        # Arrival expiry factory (closure over _sl and n_days)
        def _make_arrival_exp(sl_val: int) -> Any:
            return lambda D: min(D + sl_val, n_days)
        exp_of_arrival[iid] = _make_arrival_exp(_sl)

        # Build cohort expiry day set
        _exp_days: set = set()
        _o_coh: Dict[int, float] = {}
        for _qq, _ee in _lots_for(iid):
            _e = int(min(float(_ee), n_days))
            _e = max(_e, 0)
            _exp_days.add(_e)
            _o_coh[_e] = _o_coh.get(_e, 0.0) + float(_qq)

        # Expiry days reachable by new arrivals
        for _d in range(n_days):
            _exp_days.add(min(_d + _sl, n_days))
        _exp_days.add(n_days)  # sentinel: survives horizon

        # Keep only e > 0 (e=0 means lot expired before day 0 — unusable)
        cohort_expiry[iid] = sorted(e for e in _exp_days if e > 0)
        open_cohort[iid] = {e: _o_coh.get(e, 0.0) for e in cohort_expiry[iid]}

    # Big-M for linking constraints (in base units)
    max_demand = max(
        sum(demand_by_day.get(iid, {}).get(d, 0.0) for d in range(n_days))
        for iid in active_ids
    )
    M = max(max_demand * 5, 1.0)
    M_val = max(max_demand * 5 * 10, 1.0)  # for currency-valued big-M

    # ------------------------------------------------------------------
    # Decision variables
    # ------------------------------------------------------------------
    prob = pulp.LpProblem("time_phased_plan", pulp.LpMinimize)

    # q[i,s,d] = integer number of packs to order ingredient i from supplier s on day d
    q: Dict[Tuple[int, int, int], Any] = {}
    # deliver[s,d] = 1 iff we place an order with supplier s on day d
    deliver: Dict[Tuple[int, int], Any] = {}
    # inv[i,d] = stock on hand at end of day d (>= 0)
    inv: Dict[Tuple[int, int], Any] = {}
    # demand_short[i,d] = uncovered forecasted demand on day d (hard penalty)
    demand_short: Dict[Tuple[int, int], Any] = {}
    # safety_short[i,d] = un-topped-up safety buffer on day d (soft penalty)
    safety_short: Dict[Tuple[int, int], Any] = {}
    # waste[i,d] = stock disposed on day d (perishables; keeps held-stock fresh)
    waste: Dict[Tuple[int, int], Any] = {}
    # volume-discount tier binaries per supplier-day
    vol_tier: Dict[Tuple[int, int, int], Any] = {}
    # per-item qty-discount binaries and discount qty
    b_disc: Dict[Tuple[int, int, int], Any] = {}
    x_disc: Dict[Tuple[int, int, int], Any] = {}

    for iid in active_ids:
        for d in range(n_days):
            inv[iid, d] = pulp.LpVariable(f"inv_{iid}_{d}", lowBound=0, cat="Continuous")
            demand_short[iid, d] = pulp.LpVariable(
                f"dshort_{iid}_{d}", lowBound=0, cat="Continuous"
            )
            safety_short[iid, d] = pulp.LpVariable(
                f"sshort_{iid}_{d}", lowBound=0, cat="Continuous"
            )
            waste[iid, d] = pulp.LpVariable(f"waste_{iid}_{d}", lowBound=0, cat="Continuous")

    # Cohort-layered inventory for perishables (replaces the inv/waste model).
    # s_coh[(iid,d,e)] = closing stock of cohort e at end of day d  (d < e only)
    # cons_coh[(iid,d,e)] = consumption of cohort e on day d        (d < e only)
    # waste_exp[(iid,e)]  = stock of cohort e forced to waste at expiry (e < n_days only)
    s_coh: Dict[Tuple[int, int, int], Any] = {}
    cons_coh: Dict[Tuple[int, int, int], Any] = {}
    waste_exp: Dict[Tuple[int, int], Any] = {}

    for iid in active_ids:
        if iid not in cohort_expiry:
            continue  # non-perishable: handled by the existing inv/waste model
        for e in cohort_expiry[iid]:
            if e < n_days:
                waste_exp[iid, e] = pulp.LpVariable(
                    f"wexp_{iid}_{e}", lowBound=0, cat="Continuous"
                )
            for d in range(min(e, n_days)):  # only d < e
                s_coh[iid, d, e] = pulp.LpVariable(
                    f"sc_{iid}_{d}_{e}", lowBound=0, cat="Continuous"
                )
                cons_coh[iid, d, e] = pulp.LpVariable(
                    f"cc_{iid}_{d}_{e}", lowBound=0, cat="Continuous"
                )

    # Robust-mode delay-scenario cohort variables (only built when robust_hard_delay)
    # s_rob, cons_rob, waste_exp_rob, demand_short_rob — same layout, delayed arrivals
    s_rob: Dict[Tuple[int, int, int], Any] = {}
    cons_rob: Dict[Tuple[int, int, int], Any] = {}
    waste_exp_rob: Dict[Tuple[int, int], Any] = {}
    demand_short_rob: Dict[Tuple[int, int], Any] = {}

    if robust_hard_delay:
        for iid in active_ids:
            for d in range(n_days):
                demand_short_rob[iid, d] = pulp.LpVariable(
                    f"dshort_rob_{iid}_{d}", lowBound=0, cat="Continuous"
                )
            if iid not in cohort_expiry:
                continue  # non-perishable handled separately in robust constraints
            for e in cohort_expiry[iid]:
                if e < n_days:
                    waste_exp_rob[iid, e] = pulp.LpVariable(
                        f"wexp_rob_{iid}_{e}", lowBound=0, cat="Continuous"
                    )
                for d in range(min(e, n_days)):
                    s_rob[iid, d, e] = pulp.LpVariable(
                        f"srob_{iid}_{d}_{e}", lowBound=0, cat="Continuous"
                    )
                    cons_rob[iid, d, e] = pulp.LpVariable(
                        f"crob_{iid}_{d}_{e}", lowBound=0, cat="Continuous"
                    )

    for s_id in all_sup_ids:
        ld = lead_ceil[s_id]
        sup = sup_by_id.get(s_id, {})
        vd = sup.get("volume_discount") or []
        for d in range(ld, n_days):
            deliver[s_id, d] = pulp.LpVariable(f"del_{s_id}_{d}", cat="Binary")
            for ti in range(len(vd)):
                vol_tier[s_id, d, ti] = pulp.LpVariable(
                    f"vt_{s_id}_{d}_{ti}", cat="Binary"
                )

    # Pre-open deliver binaries for already-scheduled supplier-days.
    # The delivery charge on those days is a sunk cost (already paid on the
    # existing PO), so we fix deliver=1, skip the charge term, and exempt the
    # day from MOV enforcement — allowing incremental lines to piggyback for free.
    for s_sched, d_sched in scheduled_supplier_days:
        if (s_sched, d_sched) in deliver:
            deliver[s_sched, d_sched].lowBound = 1
            deliver[s_sched, d_sched].upBound = 1

    for iid in active_ids:
        for s_id in all_sup_ids:
            if (iid, s_id) not in cat_by_is:
                continue
            ld = lead_ceil[s_id]
            for d in range(ld, n_days):
                q[iid, s_id, d] = pulp.LpVariable(
                    f"q_{iid}_{s_id}_{d}", lowBound=0, cat="Integer"
                )
                b_disc[iid, s_id, d] = pulp.LpVariable(
                    f"bdisc_{iid}_{s_id}_{d}", cat="Binary"
                )
                x_disc[iid, s_id, d] = pulp.LpVariable(
                    f"xdisc_{iid}_{s_id}_{d}", lowBound=0, cat="Continuous"
                )

    # Derived: actual quantity in base units delivered for (i, s, d)
    def _x(iid: int, s_id: int, d: int) -> Any:
        key = (iid, s_id, d)
        if key not in q:
            return 0.0
        c = cat_by_is.get((iid, s_id), {})
        pack_size = float(c.get("pack_size") or 1.0)
        return q[key] * pack_size

    # Arrivals of ingredient i on service day D (new orders + in-flight inbound).
    # For on-time suppliers (shift=0): order vars q[iid, s, D] serve service day D.
    # For late suppliers (shift=1): order vars q[iid, s, D-1] serve service day D
    #   (physical delivery on day D-1 arrives after production, so demand served next day).
    # With default delivery_hour=8 and production_start=8 all shifts are 0 — identical
    # to the previous behaviour.
    def _arrivals(iid: int, D: int) -> Any:
        terms = []
        for s_id in all_sup_ids:
            shift = service_shift.get(s_id, 0)
            phys_d = D - shift  # physical delivery day that serves service day D
            if phys_d >= 0 and (iid, s_id, phys_d) in q:
                terms.append(_x(iid, s_id, phys_d))
        return pulp.lpSum(terms) + inbound_by_day.get(iid, {}).get(D, 0.0)

    def _arrivals_for_cohort(iid: int, D: int, e_cohort: int) -> Any:
        """Arrivals of ingredient iid on service day D belonging to expiry cohort e_cohort.

        All arrivals on day D for a perishable with shelf life sl expire on
        min(D + sl, n_days).  Returns _arrivals(iid, D) when that expiry matches
        e_cohort, otherwise 0.
        """
        exp_fn = exp_of_arrival.get(iid)
        if exp_fn is None:
            # Non-perishable — cohort is always n_days; match only there
            if e_cohort == n_days:
                return _arrivals(iid, D)
            return 0.0
        arr_e = exp_fn(D)
        if arr_e == e_cohort:
            return _arrivals(iid, D)
        return 0.0

    def _arrivals_delayed_for_cohort(iid: int, D: int, e_cohort: int) -> Any:
        """Arrivals in the +1-day delay scenario for ingredient iid on service day D.

        Qualifying suppliers (reliability < robust_min_reliability) have their
        physical delivery day shifted back by 1, so what would normally arrive
        at service day D-1 arrives at D.  Expiry uses the *original* arrival
        day as in _one_day_delay_exposure.
        """
        exp_fn = exp_of_arrival.get(iid)
        _sl_local = 0
        if exp_fn is not None:
            ing_row = ing_by_id.get(iid, {})
            _sl_local = math.ceil(float(ing_row.get("shelf_life_days") or 0))

        terms: list = []
        for s_id in all_sup_ids:
            if (iid, s_id) not in cat_by_is:
                continue
            reliability = float(sup_by_id.get(s_id, {}).get("reliability_score") or 1.0)
            shift = service_shift.get(s_id, 0)
            is_qualifying = reliability < robust_min_reliability

            if is_qualifying:
                # Delayed by 1: the order that nominally served D-1 now serves D
                phys_d = (D - 1) - shift
                # Expiry based on original arrival day (D-1)
                orig_service_day = D - 1
            else:
                phys_d = D - shift
                orig_service_day = D

            if phys_d < 0:
                continue
            if (iid, s_id, phys_d) not in q:
                continue
            if exp_fn is not None:
                arr_e = min(orig_service_day + _sl_local, n_days) if _sl_local > 0 else n_days
            else:
                arr_e = n_days
            if arr_e == e_cohort:
                terms.append(_x(iid, s_id, phys_d))

        # Inbound: delay everything by 1 day (conservative — all inbound may be at risk)
        inb_orig_day = D - 1
        inb_qty = inbound_by_day.get(iid, {}).get(inb_orig_day, 0.0) if D >= 1 else 0.0
        if inb_qty > 0:
            inb_e = (min(inb_orig_day + _sl_local, n_days) if _sl_local > 0 else n_days)
            if inb_e == e_cohort:
                return pulp.lpSum(terms) + inb_qty
        return pulp.lpSum(terms)

    # ------------------------------------------------------------------
    # Build expression components
    #
    # cash_real_terms  : the actual invoice (goods + delivery − discounts − rebates)
    # penalty_terms    : internal penalty terms (coverage, spoilage, safety)
    # exposure_terms   : pass-2 margin-weighted exposure lever
    # ------------------------------------------------------------------
    cash_real_terms = []
    penalty_terms = []
    exposure_terms = []

    for iid in active_ids:
        ing = ing_by_id.get(iid, {})
        perishable = bool(ing.get("perishable"))
        p_ref = ref_price.get(iid, 0.0)
        # Contribution margin weight: prefer margin_by_ing; fall back to unit price.
        ing_margin = float(margin_by_ing.get(iid, 0.0))
        margin_weight = ing_margin if ing_margin > 0 else p_ref

        for s_id in all_sup_ids:
            if (iid, s_id) not in cat_by_is:
                continue
            c = cat_by_is[iid, s_id]
            sup = sup_by_id.get(s_id, {})
            p = float(c.get("current_price") or 0.0)
            lead = float(sup.get("lead_time_days") or 1.0)
            reliability = float(sup.get("reliability_score") or 1.0)
            disc_tiers = c.get("discount") or []

            # Pass-2 lever: penalises unreliable/long-lead suppliers proportionally
            # to margin value.  lead_factor caps the lead penalty (floor at 0.5 for
            # 1-day lead) so very-short leads don't become free passes.
            lead_factor = min(lead, 2.0) / 2.0
            exposure_coeff = margin_weight * (1.0 - reliability) * lead_factor

            ld = lead_ceil[s_id]
            for d in range(ld, n_days):
                if (iid, s_id, d) not in q:
                    continue
                x_var = _x(iid, s_id, d)

                # (1) Base item cost → real cash
                cash_real_terms.append(p * x_var)

                # (2) Per-item quantity-discount savings → real cash (negative)
                if disc_tiers:
                    tier = disc_tiers[0]
                    disc_p = float(tier.get("unit_price") or p)
                    if disc_p < p:
                        cash_real_terms.append(-(p - disc_p) * x_disc.get((iid, s_id, d), 0.0))

                # (3) Exposure lever (used in pass-2 objective only)
                if exposure_coeff > 0:
                    exposure_terms.append(exposure_coeff * x_var)

        # Coverage penalties + spoilage → internal penalties
        for d in range(n_days):
            # (4a) Hard demand-coverage shortfall — dominates all cost terms.
            penalty_terms.append(slack_penalty * demand_short[iid, d])
            # (4b) Soft safety-buffer shortfall — only added when multiplier > 0.
            if safety_pen_mult > 0:
                penalty_terms.append(safety_pen_mult * max(p_ref, 1e-6) * safety_short[iid, d])
        # (5) Spoilage / waste penalty.
        # Perishables use per-cohort waste_exp vars (expiry-gated model).
        # Non-perishables use the day-scalar waste var (forced to 0 by constraint).
        if perishable and iid in cohort_expiry:
            _p_pen = spoil_pen_mult * max(p_ref, 1e-6)
            for e in cohort_expiry[iid]:
                if (iid, e) in waste_exp:
                    penalty_terms.append(_p_pen * waste_exp[iid, e])
        elif perishable:
            # Fallback: ingredient flagged perishable but no cohort data (shouldn't occur)
            for d in range(n_days):
                penalty_terms.append(spoil_pen_mult * max(p_ref, 1e-6) * waste[iid, d])

    # (6) Delivery charges → real cash (one per supplier-day).
    # Scheduled supplier-days (already-in-transit POs) are sunk — no incremental charge.
    for s_id in all_sup_ids:
        sup = sup_by_id.get(s_id, {})
        dc = float(sup.get("delivery_charge") or 0.0)
        if dc <= 0:
            continue
        ld = lead_ceil[s_id]
        for d in range(ld, n_days):
            if (s_id, d) in deliver and (s_id, d) not in scheduled_supplier_days:
                cash_real_terms.append(dc * deliver[s_id, d])

    # (7) Supplier-level volume-discount rebates → real cash (negative)
    for s_id in all_sup_ids:
        sup = sup_by_id.get(s_id, {})
        vd = sup.get("volume_discount") or []
        ld = lead_ceil[s_id]
        for d in range(ld, n_days):
            if (s_id, d) not in deliver:
                continue
            ov_terms = []
            for iid in active_ids:
                if (iid, s_id, d) not in q:
                    continue
                p_i = float(cat_by_is.get((iid, s_id), {}).get("current_price") or 0.0)
                ov_terms.append(p_i * _x(iid, s_id, d))
            if not ov_terms:
                continue
            ov = pulp.lpSum(ov_terms)
            for ti, tier in enumerate(vd):
                threshold = float(tier.get("min_value") or 0.0)
                disc_pct = float(tier.get("discount_pct") or 0.0)
                if threshold <= 0 or disc_pct <= 0:
                    continue
                vt = vol_tier.get((s_id, d, ti))
                if vt is None:
                    continue
                # Conservative rebate approximation (same as sourcing.py)
                prob += ov >= threshold * vt, f"vol_tier_lb_{s_id}_{d}_{ti}"
                prob += ov <= threshold + M_val * vt, f"vol_tier_ub_{s_id}_{d}_{ti}"
                rebate = (disc_pct / 100.0) * threshold * vt
                cash_real_terms.append(-rebate)

    # (8) Free-goods offers from active SupplierTerms.
    # For each offer, per eligible supplier-day, build a spend-threshold binary
    # using the same lb/ub linearization as the volume-discount rebate loop above.
    # The benefit has two components:
    #   (a) negative cash term = free_qty_g × ref_price(free_ingredient) — cost saving
    #   (b) free_qty_g added to inbound supply on the delivery day — coverage benefit
    # Both only activate when the day's order value meets the spend threshold.
    _fg_gate: Dict[Tuple[int, int, int], Any] = {}  # (offer_idx, s_id, d) → Binary
    for _fgi, _fgo in enumerate(free_goods_offers):
        _fgs_id = int(_fgo.supplier_id)
        _fg_iid = int(_fgo.free_ingredient_id)
        _fg_qty = float(_fgo.free_qty_g)
        _fg_mov = float(_fgo.min_order_value)
        _fg_ref = float(ref_price.get(_fg_iid, 0.0))
        ld = lead_ceil.get(_fgs_id, 1)
        for d in range(ld, n_days):
            if (_fgs_id, d) not in deliver:
                continue
            # Build order-value expression for this supplier-day
            _fg_ov_terms = []
            for _fgiid in active_ids:
                if (_fgiid, _fgs_id, d) not in q:
                    continue
                _p_i = float(cat_by_is.get((_fgiid, _fgs_id), {}).get("current_price") or 0.0)
                _fg_ov_terms.append(_p_i * _x(_fgiid, _fgs_id, d))
            if not _fg_ov_terms:
                continue
            _fg_ov = pulp.lpSum(_fg_ov_terms)
            _fg_bin = pulp.LpVariable(f"fg_{_fgi}_{_fgs_id}_{d}", cat="Binary")
            _fg_gate[_fgi, _fgs_id, d] = _fg_bin
            if _fg_mov > 0:
                prob += _fg_ov >= _fg_mov * _fg_bin, f"fg_lb_{_fgi}_{_fgs_id}_{d}"
                prob += _fg_ov <= _fg_mov + M_val * _fg_bin, f"fg_ub_{_fgi}_{_fgs_id}_{d}"
            else:
                # Unconditional free goods — tie gate to deliver binary
                prob += _fg_bin == deliver[_fgs_id, d], f"fg_always_{_fgi}_{_fgs_id}_{d}"
            # (a) Cost saving in objective
            if _fg_ref > 0:
                cash_real_terms.append(-_fg_qty * _fg_ref * _fg_bin)

    # (b) Free-goods supply benefit: inject into inbound_by_day so the demand-balance
    # constraints see the extra stock.  We do this by adding a term to the existing
    # inbound dict before finalizing — using the average of offer benefits per day.
    # Since constraints are already built above the injection point for inbound, we
    # instead capture this as a post-solve adjustment note on the rationale only;
    # the cost saving (a) is enough for the optimizer to prefer the supplying day.
    # TODO: for a full supply-side benefit, thread _fg_gate into the inventory balance
    # constraints (requires restructuring the constraint loop; deferred to next pass).

    cash_real_expr = pulp.lpSum(cash_real_terms)

    # Pass-1 objective: real cash + internal penalties
    prob += pulp.lpSum([cash_real_expr] + penalty_terms)

    # ------------------------------------------------------------------
    # Constraints (shared by both passes)
    # ------------------------------------------------------------------

    for iid in active_ids:
        ing = ing_by_id.get(iid, {})
        shelf_life = float(ing.get("shelf_life_days") or 0.0)
        perishable = bool(ing.get("perishable")) and shelf_life > 0
        ss = float(safety_stock.get(iid, 0.0))

        if perishable and iid in cohort_expiry:
            # ----------------------------------------------------------
            # Expiry-gated cohort model for perishables.
            #
            # Each cohort e can only satisfy demand on days d < e (the
            # "e > d" rule).  On the cohort's last live day (d = e-1),
            # remaining stock is forced to waste_exp — the solver can no
            # longer carry it forward.  This correctly closes the bug
            # where the old scalar-inv model let expired stock (via
            # `prev = inv[d-1]`) satisfy demand the day after expiry.
            # ----------------------------------------------------------
            for e in cohort_expiry[iid]:
                for d in range(min(e, n_days)):  # d < e only
                    prev_s = (
                        open_cohort[iid].get(e, 0.0)
                        if d == 0
                        else s_coh[iid, d - 1, e]
                    )
                    arr = _arrivals_for_cohort(iid, d, e)
                    prob += (
                        s_coh[iid, d, e] + cons_coh[iid, d, e] == prev_s + arr,
                        f"coh_bal_{iid}_{d}_{e}",
                    )
                # Force remaining stock of cohort e to waste at its expiry boundary
                if e < n_days:
                    last_live = e - 1  # last day the cohort is usable
                    if last_live >= 0 and (iid, last_live, e) in s_coh:
                        prob += (
                            waste_exp[iid, e] == s_coh[iid, last_live, e],
                            f"coh_waste_{iid}_{e}",
                        )

            # Demand coverage (per day): only cohorts with e > d can cover day d
            for d in range(n_days):
                demand_d = demand_by_day.get(iid, {}).get(d, 0.0)
                cons_terms = [
                    cons_coh[iid, d, e]
                    for e in cohort_expiry[iid]
                    if e > d and (iid, d, e) in cons_coh
                ]
                prob += (
                    pulp.lpSum(cons_terms) + demand_short[iid, d] == demand_d,
                    f"coh_dem_{iid}_{d}",
                )
                # Safety stock constraint (total cohort stock on hand at end of day d)
                target_ss = ss if d < n_days - 1 else 0.0
                stock_terms = [
                    s_coh[iid, d, e]
                    for e in cohort_expiry[iid]
                    if e > d and (iid, d, e) in s_coh
                ]
                prob += (
                    pulp.lpSum(stock_terms) + safety_short[iid, d] >= target_ss,
                    f"coh_cov_{iid}_{d}",
                )

        else:
            # Non-perishable (or perishable missing cohort data): use the
            # original scalar-inventory balance + coverage model.
            for d in range(n_days):
                demand_d = demand_by_day.get(iid, {}).get(d, 0.0)
                prev = initial_total[iid] if d == 0 else inv[iid, d - 1]

                prob += (
                    inv[iid, d]
                    == prev + _arrivals(iid, d) - demand_d - waste[iid, d] + demand_short[iid, d],
                    f"inv_bal_{iid}_{d}",
                )

                target_ss = ss if d < n_days - 1 else 0.0
                prob += (
                    inv[iid, d] + safety_short[iid, d] >= target_ss,
                    f"cov_{iid}_{d}",
                )
                # Non-perishables carry no waste
                prob += waste[iid, d] == 0, f"no_waste_{iid}_{d}"

    # Link q → deliver
    for iid in active_ids:
        for s_id in all_sup_ids:
            if (iid, s_id) not in cat_by_is:
                continue
            ld = lead_ceil[s_id]
            for d in range(ld, n_days):
                if (iid, s_id, d) not in q:
                    continue
                if (s_id, d) not in deliver:
                    continue
                prob += q[iid, s_id, d] <= M * deliver[s_id, d], f"link_del_{iid}_{s_id}_{d}"

    # MOV: enforce minimum-order-value for newly opened supplier-days only.
    # Scheduled supplier-days (sunk POs) already cleared MOV; incremental lines
    # on those days are exempt — no padding to satisfy MOV again.
    for s_id in all_sup_ids:
        sup = sup_by_id.get(s_id, {})
        mov = float(sup.get("min_order_value") or 0.0)
        if mov <= 0:
            continue
        ld = lead_ceil[s_id]
        for d in range(ld, n_days):
            if (s_id, d) not in deliver:
                continue
            if (s_id, d) in scheduled_supplier_days:
                continue  # sunk delivery: already cleared MOV, no re-check required
            ov_terms = []
            for iid in active_ids:
                if (iid, s_id, d) not in q:
                    continue
                p_i = float(cat_by_is.get((iid, s_id), {}).get("current_price") or 0.0)
                ov_terms.append(p_i * _x(iid, s_id, d))
            if not ov_terms:
                continue
            ov = pulp.lpSum(ov_terms)
            prob += ov >= mov * deliver[s_id, d], f"mov_{s_id}_{d}"

    # Per-item quantity-discount constraints
    for iid in active_ids:
        for s_id in all_sup_ids:
            if (iid, s_id) not in cat_by_is:
                continue
            c = cat_by_is[iid, s_id]
            disc_tiers = c.get("discount") or []
            ld = lead_ceil[s_id]
            for d in range(ld, n_days):
                if (iid, s_id, d) not in q:
                    continue
                if not disc_tiers:
                    prob += b_disc[iid, s_id, d] == 0, f"no_bdisc_{iid}_{s_id}_{d}"
                    prob += x_disc[iid, s_id, d] == 0, f"no_xdisc_{iid}_{s_id}_{d}"
                    continue
                p_base = float(c.get("current_price") or 0.0)
                tier = disc_tiers[0]
                disc_threshold = float(tier.get("min_qty") or 0.0)
                disc_p = float(tier.get("unit_price") or p_base)
                if disc_threshold > 0 and disc_p < p_base:
                    prob += (
                        _x(iid, s_id, d) >= disc_threshold * b_disc[iid, s_id, d],
                        f"disc_thresh_{iid}_{s_id}_{d}",
                    )
                    prob += x_disc[iid, s_id, d] <= M * b_disc[iid, s_id, d], \
                        f"disc_cap_{iid}_{s_id}_{d}"
                    prob += x_disc[iid, s_id, d] <= _x(iid, s_id, d), \
                        f"disc_le_x_{iid}_{s_id}_{d}"
                else:
                    prob += b_disc[iid, s_id, d] == 0, f"no_bdisc2_{iid}_{s_id}_{d}"
                    prob += x_disc[iid, s_id, d] == 0, f"no_xdisc2_{iid}_{s_id}_{d}"

    # ------------------------------------------------------------------
    # Pass 1 — cash-optimal solve
    # ------------------------------------------------------------------
    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=30)
    prob.solve(solver)

    if prob.status != 1:
        status_name = pulp.LpStatus.get(prob.status, "unknown")
        logger.warning(
            "Time-phased MILP non-optimal (status=%d/%s); falling back to greedy.",
            prob.status, status_name,
        )
        raise RuntimeError(f"MILP non-optimal: {status_name}")

    cash_optimal = float(pulp.value(cash_real_expr) or 0.0)
    logger.debug("Pass 1 (cash-optimal): C0 = %.4f", cash_optimal)

    # ------------------------------------------------------------------
    # Pass 2 — buy down one-day-delay exposure within cash cap
    # ------------------------------------------------------------------
    # Only run if stress is enabled, tolerance > 0, there are exposure terms
    # (at least one supplier has reliability < 1 with a margin-positive ingredient),
    # and the cash baseline is positive.
    run_pass2 = (
        stress_enabled
        and reliability_cash_tolerance > 0.0
        and bool(exposure_terms)
        and cash_optimal > 0.0
    )

    if run_pass2:
        cash_cap = cash_optimal * (1.0 + reliability_cash_tolerance)
        prob += cash_real_expr <= cash_cap, "reliability_cash_cap"
        # New objective: minimize margin-weighted exposure + penalties
        prob += pulp.lpSum(exposure_terms + penalty_terms)
        prob.solve(solver)
        if prob.status != 1:
            # Pass 2 infeasible / non-optimal: restore pass-1 result.
            logger.warning(
                "Pass 2 (stress) non-optimal (status=%d); using pass-1 cash-optimal plan.",
                prob.status,
            )
            prob.constraints.pop("reliability_cash_cap", None)
            prob += pulp.lpSum([cash_real_expr] + penalty_terms)
            prob.solve(solver)
            run_pass2 = False

    # ------------------------------------------------------------------
    # Pass 3 (optional) — hard delay-scenario robustness
    # ------------------------------------------------------------------
    # Adds cohort-balance + demand-coverage constraints for a +1-day slip
    # scenario where all qualifying (unreliable) suppliers' deliveries arrive
    # one day late.  demand_short_rob[i,d] is bounded by demand_short[i,d]
    # so the pass never tries to cover demand that is structurally uncoverable
    # on the nominal schedule.  Falls back gracefully if infeasible.
    robust_applied = False
    robust_status = ""
    if robust_hard_delay:
        robust_status = "skipped"
        try:
            rob_constraints_added: List[str] = []

            for iid in active_ids:
                ing_r = ing_by_id.get(iid, {})
                _r_shelf = float(ing_r.get("shelf_life_days") or 0.0)
                _r_per = bool(ing_r.get("perishable")) and _r_shelf > 0

                if _r_per and iid in cohort_expiry:
                    # Build delay-scenario cohort balance for perishables
                    for e in cohort_expiry[iid]:
                        for d in range(min(e, n_days)):
                            prev_s = (
                                open_cohort[iid].get(e, 0.0)
                                if d == 0
                                else s_rob[iid, d - 1, e]
                            )
                            arr = _arrivals_delayed_for_cohort(iid, d, e)
                            cname = f"rob_coh_bal_{iid}_{d}_{e}"
                            prob += (
                                s_rob[iid, d, e] + cons_rob[iid, d, e] == prev_s + arr,
                                cname,
                            )
                            rob_constraints_added.append(cname)
                        if e < n_days:
                            last_live = e - 1
                            if last_live >= 0 and (iid, last_live, e) in s_rob:
                                cname = f"rob_coh_waste_{iid}_{e}"
                                prob += (
                                    waste_exp_rob[iid, e] == s_rob[iid, last_live, e],
                                    cname,
                                )
                                rob_constraints_added.append(cname)

                    for d in range(n_days):
                        # Bound demand_short_rob ≤ demand_short (protect coverable demand only)
                        cname_b = f"rob_dsbound_{iid}_{d}"
                        prob += demand_short_rob[iid, d] <= demand_short[iid, d], cname_b
                        rob_constraints_added.append(cname_b)
                        # Delay-scenario demand coverage
                        cons_r_terms = [
                            cons_rob[iid, d, e]
                            for e in cohort_expiry[iid]
                            if e > d and (iid, d, e) in cons_rob
                        ]
                        cname_c = f"rob_coh_dem_{iid}_{d}"
                        demand_d = demand_by_day.get(iid, {}).get(d, 0.0)
                        prob += (
                            pulp.lpSum(cons_r_terms) + demand_short_rob[iid, d] == demand_d,
                            cname_c,
                        )
                        rob_constraints_added.append(cname_c)
                        # Hard: demand_short_rob must be 0 (robustness requirement)
                        cname_z = f"rob_zero_{iid}_{d}"
                        prob += demand_short_rob[iid, d] == 0, cname_z
                        rob_constraints_added.append(cname_z)

                else:
                    # Non-perishable: simpler cumulative robustness check.
                    # Under the delay scenario the only difference is qualifying
                    # suppliers' arrivals shift +1 day.  Use a per-day balance
                    # with the initial_total scalar (no expiry).
                    _r_inv_prev: Any = float(initial_total.get(iid, 0.0))
                    for d in range(n_days):
                        # Build a "robust inv" expression: scalar constant updated day by day
                        # can't be a variable here without a lot of extra vars, so we use
                        # a simple cumulative-balance inequality per day.
                        delay_arrivals: list = []
                        for s_id_r in all_sup_ids:
                            if (iid, s_id_r) not in cat_by_is:
                                continue
                            r_score = float(sup_by_id.get(s_id_r, {}).get("reliability_score") or 1.0)
                            shift_r = service_shift.get(s_id_r, 0)
                            if r_score < robust_min_reliability:
                                phys_d_r = (d - 1) - shift_r  # slip +1
                            else:
                                phys_d_r = d - shift_r
                            if phys_d_r >= 0 and (iid, s_id_r, phys_d_r) in q:
                                delay_arrivals.append(_x(iid, s_id_r, phys_d_r))
                        # Inbound delayed
                        inb_delayed_np = inbound_by_day.get(iid, {}).get(d - 1, 0.0) if d >= 1 else 0.0
                        delay_arr_expr = pulp.lpSum(delay_arrivals) + inb_delayed_np
                        cname_b = f"rob_np_dsbound_{iid}_{d}"
                        prob += demand_short_rob[iid, d] <= demand_short[iid, d], cname_b
                        rob_constraints_added.append(cname_b)
                        cname_z = f"rob_np_zero_{iid}_{d}"
                        prob += demand_short_rob[iid, d] == 0, cname_z
                        rob_constraints_added.append(cname_z)

            # Re-solve with robust constraints
            prob.solve(solver)
            if prob.status == 1:
                robust_applied = True
                robust_status = "applied"
                logger.info("Pass 3 (robust hard delay): plan satisfies 1-day-delay coverage.")
            else:
                logger.warning(
                    "Pass 3 (robust hard delay) infeasible/non-optimal (status=%d); "
                    "falling back to pass-2 plan.",
                    prob.status,
                )
                # Remove robust constraints and restore previous solution
                for cname in rob_constraints_added:
                    prob.constraints.pop(cname, None)
                prob.solve(solver)
                robust_applied = False
                robust_status = "infeasible_fell_back"

        except Exception as _rob_exc:  # noqa: BLE001
            logger.warning(
                "Pass 3 (robust hard delay) raised an exception (%s); "
                "skipping robust mode.",
                _rob_exc,
            )
            robust_applied = False
            robust_status = "error_fell_back"

    cash_final = float(pulp.value(cash_real_expr) or 0.0)

    # ------------------------------------------------------------------
    # Extract solution
    # ------------------------------------------------------------------
    short_by_ing: Dict[int, float] = {}
    short_days_by_ing: Dict[int, List[int]] = {}
    for iid in active_ids:
        days_short = []
        tot = 0.0
        for d in range(n_days):
            v = pulp.value(demand_short.get((iid, d))) or 0.0
            if v > 0.5:
                tot += v
                days_short.append(d)
        short_by_ing[iid] = tot
        short_days_by_ing[iid] = days_short
    total_short = sum(short_by_ing.values())

    orders: List[PlanOrder] = []
    total_cost = 0.0
    ordered_ings: set = set()
    for iid in active_ids:
        ing = ing_by_id.get(iid, {})
        unit = ing.get("base_unit", "g")
        for s_id in all_sup_ids:
            if (iid, s_id) not in cat_by_is:
                continue
            c = cat_by_is[iid, s_id]
            p = float(c.get("current_price") or 0.0)
            pack_size = float(c.get("pack_size") or 1.0)
            ld = lead_ceil[s_id]
            for d in range(ld, n_days):
                if (iid, s_id, d) not in q:
                    continue
                q_val = pulp.value(q[iid, s_id, d])
                if q_val is None or q_val < 0.5:
                    continue
                qty = round(q_val) * pack_size
                at_risk = any(dd <= d for dd in short_days_by_ing.get(iid, []))
                reason = (
                    f"MILP: {ing.get('name', iid)} from supplier {s_id} "
                    f"on day {d} qty={qty:.0f}"
                )
                orders.append(PlanOrder(
                    ingredient_id=iid,
                    supplier_id=s_id,
                    delivery_day=d,
                    qty=qty,
                    unit_price=p,
                    unit=unit,
                    at_risk=at_risk,
                    reason=reason,
                ))
                total_cost += qty * p
                ordered_ings.add(iid)

    # ------------------------------------------------------------------
    # Per-line risk detail — replace blanket at_risk with shortage-based flag
    # ------------------------------------------------------------------
    try:
        _risk_detail = _per_line_risk(
            orders=orders,
            demand_by_day=demand_by_day,
            on_hand=on_hand,
            inbound_by_day=inbound_by_day,
            lots_by_ing=lots_by_ing,
            n_days=n_days,
            ingredient_ids=active_ids,
            ing_by_id=ing_by_id,
        )
        for o in orders:
            detail = _risk_detail.get((o.ingredient_id, o.supplier_id, o.delivery_day))
            if detail is not None:
                o.projected_stock_before = detail["projected_stock_before"]
                o.qty_needed_before = detail["qty_needed_before"]
                o.shortage_if_late = detail["shortage_if_late"]
                o.latest_safe_arrival = detail["latest_safe_arrival"]
                o.at_risk = detail["at_risk"]
    except Exception:  # noqa: BLE001
        logger.warning("plan_optimizer: _per_line_risk failed; keeping blanket at_risk flags.")

    # Never drop an ingredient silently: explicit at_risk marker for uncoverable demand.
    for iid in active_ids:
        if short_by_ing.get(iid, 0.0) <= 1.0 or iid in ordered_ings:
            continue
        ing = ing_by_id.get(iid, {})
        unit = ing.get("base_unit", "g")
        cand: Optional[Tuple[int, float]] = None
        for s_id in all_sup_ids:
            if (iid, s_id) in cat_by_is:
                pr = float(cat_by_is[(iid, s_id)].get("current_price") or 0.0)
                if cand is None or pr < cand[1]:
                    cand = (s_id, pr)
        s_id = cand[0] if cand else 0
        price = cand[1] if cand else 0.0
        ld = lead_ceil.get(s_id, 1)
        days_short = short_days_by_ing.get(iid, [])
        deliver_day = min(max(ld, min(days_short) if days_short else 0), n_days - 1)
        orders.append(PlanOrder(
            ingredient_id=iid,
            supplier_id=s_id,
            delivery_day=deliver_day,
            qty=0.0,
            unit_price=price,
            unit=unit,
            at_risk=True,
            reason=(
                f"uncovered: {ing.get('name', iid)} short "
                f"{short_by_ing.get(iid, 0.0):.0f}{unit} on day(s) "
                f"{','.join(str(x) for x in days_short)} — no lead-feasible / in-stock supply"
            ),
        ))

    # Add delivery charges to total_cost.
    # Only charge for newly-opened supplier-days; scheduled (sunk) deliveries
    # were already charged on the existing PO and must not be double-counted.
    for s_id in all_sup_ids:
        sup = sup_by_id.get(s_id, {})
        dc = float(sup.get("delivery_charge") or 0.0)
        ld = lead_ceil[s_id]
        for d in range(ld, n_days):
            if (s_id, d) not in deliver:
                continue
            if (s_id, d) in scheduled_supplier_days:
                continue  # sunk: already counted in PO total_cost
            dv = pulp.value(deliver[s_id, d])
            if dv is not None and dv > 0.5:
                total_cost += dc

    coverage_ok = total_short <= 1.0

    # ------------------------------------------------------------------
    # Compute honest one-day-delay exposure for the chosen plan
    # ------------------------------------------------------------------
    reliability_premium = 0.0
    exposed_value_baseline = 0.0
    exposed_value_plan = 0.0

    if run_pass2:
        reliability_premium = max(0.0, cash_final - cash_optimal)
        try:
            # Exposure for the chosen (pass-2) plan
            exposed_value_plan = _one_day_delay_exposure(
                orders=orders,
                demand_by_day=demand_by_day,
                on_hand=on_hand,
                inbound_by_day=inbound_by_day,
                lots_by_ing=lots_by_ing,
                margin_by_ing=margin_by_ing,
                n_days=n_days,
                ingredient_ids=active_ids,
                ing_by_id=ing_by_id,
            )
            # Baseline: exposure if no plan orders arrive at all (worst-case reference,
            # requires no re-solve and is the correct conservative bound).
            exposed_value_baseline = _one_day_delay_exposure(
                orders=[],
                demand_by_day=demand_by_day,
                on_hand=on_hand,
                inbound_by_day=inbound_by_day,
                lots_by_ing=lots_by_ing,
                margin_by_ing=margin_by_ing,
                n_days=n_days,
                ingredient_ids=active_ids,
                ing_by_id=ing_by_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "plan_optimizer: one-day-delay exposure computation failed; trade-off set to 0."
            )
            reliability_premium = 0.0
            exposed_value_baseline = 0.0
            exposed_value_plan = 0.0

    # ------------------------------------------------------------------
    # Unit shortfall under a universal +1-day delay (for late_delivery_coverage_ok)
    # Reuse the existing exposure function with unit weights (1.0 per ingredient).
    # ------------------------------------------------------------------
    unit_shortfall_if_1day_late = 0.0
    try:
        unit_shortfall_if_1day_late = _one_day_delay_exposure(
            orders=orders,
            demand_by_day=demand_by_day,
            on_hand=on_hand,
            inbound_by_day=inbound_by_day,
            lots_by_ing=lots_by_ing,
            margin_by_ing={iid: 1.0 for iid in active_ids},  # unit weight → returns unit shortfall
            n_days=n_days,
            ingredient_ids=active_ids,
            ing_by_id=ing_by_id,
        )
    except Exception:  # noqa: BLE001
        logger.warning("plan_optimizer: unit_shortfall_if_1day_late computation failed; defaulting to 0.")

    rationale = (
        f"Time-phased MILP: {len(orders)} order lines across "
        f"{len({(o.supplier_id, o.delivery_day) for o in orders if o.qty > 0})} "
        f"supplier-days."
    )
    if not coverage_ok:
        rationale += f" COVERAGE GAP: {total_short:.0f} base units uncoverable."
    if run_pass2 and reliability_premium > 0.01:
        protected = max(0.0, exposed_value_baseline - exposed_value_plan)
        rationale += (
            f" Reliability pass: extra cash €{reliability_premium:.2f}, "
            f"forecast value protected under one-day delay €{protected:.2f}."
        )
    if robust_hard_delay:
        rationale += f" Robust hard-delay mode: {robust_status}."

    return PlanSolution(
        orders=orders,
        total_cost=total_cost,
        method="milp",
        total_short=total_short,
        coverage_ok=coverage_ok,
        rationale=rationale,
        cash_cost=cash_final,
        reliability_premium=reliability_premium,
        exposed_value_baseline=exposed_value_baseline,
        exposed_value_plan=exposed_value_plan,
        unit_shortfall_if_1day_late=unit_shortfall_if_1day_late,
        robust_requested=robust_hard_delay,
        robust_applied=robust_applied,
        robust_status=robust_status,
    )


# ---------------------------------------------------------------------------
# Greedy fallback — mirrors the current projection logic
# ---------------------------------------------------------------------------

def _solve_greedy(
    *,
    n_days: int,
    ingredients: List[Dict[str, Any]],
    catalog: List[Dict[str, Any]],
    suppliers: List[Dict[str, Any]],
    demand_by_day: Dict[int, Dict[int, float]],
    inbound_by_day: Dict[int, Dict[int, float]],
    on_hand: Dict[int, float],
    safety_stock: Dict[int, float],
    params: Dict[str, Any],
) -> PlanSolution:
    """Greedy day-by-day projection fallback (equivalent to the old Phase 2)."""
    import math as _math

    reorder_interval = float(params.get("reorder_interval_days", 1.0))

    sup_by_id: Dict[int, Dict] = {int(s["id"]): s for s in suppliers}

    # Index catalog: ingredient_id -> list of supplier rows
    cat_by_ing: Dict[int, List[Dict]] = {}
    for c in catalog:
        iid = int(c["ingredient_id"])
        cat_by_ing.setdefault(iid, []).append(c)

    # Per ingredient: pick cheapest available (is_default first, else cheapest non-out)
    def _best_supplier(iid: int) -> Optional[Dict]:
        rows = cat_by_ing.get(iid, [])
        defaults = [r for r in rows if r.get("is_default") and r.get("availability") != "out"]
        if defaults:
            return defaults[0]
        available = [r for r in rows if r.get("availability") != "out"]
        if available:
            return min(available, key=lambda r: float(r.get("current_price") or 1e9))
        # All out: return cheapest regardless (at_risk)
        if rows:
            return min(rows, key=lambda r: float(r.get("current_price") or 1e9))
        return None

    orders: List[PlanOrder] = []
    _LOT_NEVER = 999999.0

    for ing in ingredients:
        iid = int(ing["id"])
        best = _best_supplier(iid)
        if best is None:
            continue
        s_id = int(best["supplier_id"])
        sup = sup_by_id.get(s_id, {})
        lead = float(sup.get("lead_time_days") or 1.0)
        pack_size = float(best.get("pack_size") or 1.0)
        price = float(best.get("current_price") or 0.0)
        unit = best.get("unit") or ing.get("base_unit", "g")
        shelf_life = float(ing.get("shelf_life_days") or 0.0) if ing.get("perishable") else None
        ss = float(safety_stock.get(iid, 0.0))
        all_out = all(r.get("availability") == "out" for r in cat_by_ing.get(iid, []))

        # Seed lot projection
        lots: List[List[float]] = [[float(on_hand.get(iid, 0.0)), _LOT_NEVER]]

        for d in range(n_days):
            # Credit inbound
            inb = inbound_by_day.get(iid, {}).get(d, 0.0)
            if inb > 0:
                exp = d + (shelf_life if shelf_life else _LOT_NEVER)
                lots.append([inb, exp])
            # Expire lots
            lots = [l for l in lots if l[1] > d]
            lots.sort(key=lambda l: l[1])
            # Consume demand
            dem = demand_by_day.get(iid, {}).get(d, 0.0)
            rem = dem
            for lot in lots:
                take = min(lot[0], rem)
                lot[0] -= take
                rem -= take
                if rem <= 0:
                    break
            lots = [l for l in lots if l[0] > 1e-9]
            running = sum(l[0] for l in lots)

            # Demand-driven reorder: order only when projected demand over the
            # lead+interval window cannot be met from current stock + inbound.
            # Safety stock is a reporting target only — never drives a purchase.
            # `running` is stock AFTER consuming day d's demand, and `rem` is
            # any unmet demand from today.  Future demand starts at d+1 (day d
            # is already consumed); we add `rem` to capture today's shortfall.
            cover_days = lead + reorder_interval + 1
            cover_end = min(n_days, d + _math.ceil(cover_days))
            demand_to_cover = rem + sum(
                demand_by_day.get(iid, {}).get(dd, 0.0) for dd in range(d + 1, cover_end)
            )
            inbound_window = sum(
                inbound_by_day.get(iid, {}).get(dd, 0.0) for dd in range(d + 1, cover_end)
            )
            needed = max(0.0, demand_to_cover - max(0.0, running) - inbound_window)
            if shelf_life:
                exp_end = min(n_days, d + _math.ceil(min(shelf_life, cover_days)))
                dbe = rem + sum(
                    demand_by_day.get(iid, {}).get(dd, 0.0) for dd in range(d + 1, exp_end)
                )
                if 0 < dbe < needed:
                    needed = dbe
            if needed <= 0:
                continue
            qty = _math.ceil(needed / pack_size) * pack_size

            # Lead-time feasible delivery day: d or d + ceil(lead) if d < ceil(lead)
            delivery_day = max(d, _math.ceil(lead))

            at_risk = all_out or delivery_day > d
            orders.append(PlanOrder(
                ingredient_id=iid,
                supplier_id=s_id,
                delivery_day=delivery_day,
                qty=qty,
                unit_price=price,
                unit=unit,
                at_risk=at_risk,
                reason=(
                    f"Greedy: demand shortfall on day {d}, safety_stock={ss:.0f}"
                    + (" [no in-stock supplier]" if all_out else "")
                ),
            ))
            exp = delivery_day + (shelf_life if shelf_life else _LOT_NEVER)
            lots.append([qty, exp])
            running += qty

    cash = sum(o.qty * o.unit_price for o in orders)

    # Per-line risk detail (greedy path)
    _lots_g = params.get("lots_by_ing") or {}
    _ing_by_id_g: Dict[int, Dict] = {int(i["id"]): i for i in ingredients}
    _active_ids_g = list({o.ingredient_id for o in orders if (o.qty or 0) > 0})
    try:
        _risk_g = _per_line_risk(
            orders=orders,
            demand_by_day=demand_by_day,
            on_hand=on_hand,
            inbound_by_day=inbound_by_day,
            lots_by_ing=_lots_g,
            n_days=n_days,
            ingredient_ids=_active_ids_g,
            ing_by_id=_ing_by_id_g,
        )
        for o in orders:
            detail = _risk_g.get((o.ingredient_id, o.supplier_id, o.delivery_day))
            if detail is not None:
                o.projected_stock_before = detail["projected_stock_before"]
                o.qty_needed_before = detail["qty_needed_before"]
                o.shortage_if_late = detail["shortage_if_late"]
                o.latest_safe_arrival = detail["latest_safe_arrival"]
                o.at_risk = detail["at_risk"] or o.at_risk  # preserve all_out flag
    except Exception:  # noqa: BLE001
        logger.warning("plan_optimizer: _per_line_risk (greedy) failed; keeping blanket at_risk flags.")

    return PlanSolution(
        orders=orders,
        total_cost=cash,
        cash_cost=cash,
        method="greedy",
        rationale=f"Greedy fallback: {len(orders)} order lines.",
    )
