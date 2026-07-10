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

Perishability is modelled as a *held-inventory* cap (standard perishable
lot-sizing): the stock on hand at the end of each day may not exceed what
arrived within the ingredient's shelf life plus the still-fresh opening stock.
Excess is disposed via a penalised ``waste`` variable.  This never forbids
ordering a whole pack (the old per-order expiry cap did, silently dropping
ingredients whose pack exceeded the shrinking before-expiry window), and it
naturally schedules a fresh late-week delivery for short-shelf items.

Coverage is two-tier: forecasted demand is a hard requirement (heavily
penalised ``demand_short``) while ``safety_stock`` is a soft buffer topped up
only when cheap (lightly, value-scaled ``safety_short``).  Any demand that is
physically impossible to cover (early days before any delivery can arrive, or
all suppliers out) is surfaced as ``at_risk`` — never hidden.

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
    at_risk: bool = False      # True when coverage cannot be guaranteed
    reason: str = ""


@dataclass
class PlanSolution:
    """Result of solve_time_phased_plan."""
    orders: List[PlanOrder] = field(default_factory=list)
    total_cost: float = 0.0
    method: str = "greedy"    # "milp" | "greedy"
    rationale: str = ""
    total_short: float = 0.0  # total un-coverable forecasted demand (base units)
    coverage_ok: bool = True  # False when any forecasted demand is left uncovered


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
                            lead_risk_lambda (default 0.3)
                            spoilage_penalty_multiplier (default 2.0)
                            slack_penalty (default 1000.0)          -- demand
                            safety_penalty_multiplier (default 1.5) -- soft buffer
                            lots_by_ing: {ing_id: [[qty, exp_day_offset], ...]}
                              dated opening lots for expiry-aware fresh stock;
                              exp_day_offset counts days from now (>= n_days or
                              missing => never expires within horizon).  Absent
                              => a single never-expiring lot equal to on_hand.
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

    lead_risk_lambda = float(params.get("lead_risk_lambda", 0.3))
    spoil_pen_mult = float(params.get("spoilage_penalty_multiplier", 2.0))
    slack_penalty = float(params.get("slack_penalty", 1000.0))            # demand (hard)
    safety_pen_mult = float(params.get("safety_penalty_multiplier", 1.5))  # buffer (soft)
    lots_by_ing: Dict[int, List[List[float]]] = params.get("lots_by_ing") or {}

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
    # soft safety-buffer and waste penalties (so they never justify a dedicated
    # delivery just to top up a buffer, but are filled when piggybacking).
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

    # Arrivals of ingredient i on day d (new orders + in-flight inbound).
    def _arrivals(iid: int, d: int) -> Any:
        terms = [
            _x(iid, s_id, d)
            for s_id in all_sup_ids
            if (iid, s_id, d) in q
        ]
        return pulp.lpSum(terms) + inbound_by_day.get(iid, {}).get(d, 0.0)

    # ------------------------------------------------------------------
    # Objective
    # ------------------------------------------------------------------
    obj_terms = []

    for iid in active_ids:
        ing = ing_by_id.get(iid, {})
        perishable = bool(ing.get("perishable"))
        p_ref = ref_price.get(iid, 0.0)

        for s_id in all_sup_ids:
            if (iid, s_id) not in cat_by_is:
                continue
            c = cat_by_is[iid, s_id]
            sup = sup_by_id.get(s_id, {})
            p = float(c.get("current_price") or 0.0)
            lead = float(sup.get("lead_time_days") or 1.0)
            reliability = float(sup.get("reliability_score") or 1.0)
            risk_coeff = lead_risk_lambda * lead * (1.0 - reliability)
            disc_tiers = c.get("discount") or []

            ld = lead_ceil[s_id]
            for d in range(ld, n_days):
                if (iid, s_id, d) not in q:
                    continue
                x_var = _x(iid, s_id, d)

                # (1) Base item cost
                obj_terms.append(p * x_var)

                # (5) Lead-time x reliability risk
                if risk_coeff > 0:
                    obj_terms.append(risk_coeff * x_var)

                # (6) Per-item quantity-discount savings
                if disc_tiers:
                    tier = disc_tiers[0]
                    disc_p = float(tier.get("unit_price") or p)
                    if disc_p < p:
                        obj_terms.append(-(p - disc_p) * x_disc.get((iid, s_id, d), 0.0))

        # Coverage penalties + spoilage
        for d in range(n_days):
            # (3a) Hard demand-coverage shortfall — dominates all cost terms.
            obj_terms.append(slack_penalty * demand_short[iid, d])
            # (3b) Soft safety-buffer shortfall — only added to objective when
            #      safety_pen_mult > 0.  When the caller sets it to 0.0, the safety
            #      buffer becomes a pure reporting target and never drives a purchase.
            if safety_pen_mult > 0:
                obj_terms.append(safety_pen_mult * max(p_ref, 1e-6) * safety_short[iid, d])
            # (4) Spoilage / waste penalty (perishables carry real waste vars).
            if perishable:
                obj_terms.append(spoil_pen_mult * max(p_ref, 1e-6) * waste[iid, d])

    # (2) Delivery charges — one per supplier-day
    for s_id in all_sup_ids:
        sup = sup_by_id.get(s_id, {})
        dc = float(sup.get("delivery_charge") or 0.0)
        if dc <= 0:
            continue
        ld = lead_ceil[s_id]
        for d in range(ld, n_days):
            if (s_id, d) in deliver:
                obj_terms.append(dc * deliver[s_id, d])

    # (7) Supplier-level volume-discount rebates (per delivery day)
    for s_id in all_sup_ids:
        sup = sup_by_id.get(s_id, {})
        vd = sup.get("volume_discount") or []
        ld = lead_ceil[s_id]
        for d in range(ld, n_days):
            if (s_id, d) not in deliver:
                continue
            # order value for supplier s on day d
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
                # Tier reached iff order value >= threshold
                prob += ov >= threshold * vt, f"vol_tier_lb_{s_id}_{d}_{ti}"
                prob += ov <= threshold + M_val * vt, f"vol_tier_ub_{s_id}_{d}_{ti}"
                # Conservative rebate approximation (same as sourcing.py:481-483)
                rebate = (disc_pct / 100.0) * threshold * vt
                obj_terms.append(-rebate)

    prob += pulp.lpSum(obj_terms)

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------

    # Inventory balance + coverage + perishable held-stock cap
    for iid in active_ids:
        ing = ing_by_id.get(iid, {})
        shelf_life = float(ing.get("shelf_life_days") or 0.0)
        perishable = bool(ing.get("perishable")) and shelf_life > 0
        sl = math.ceil(shelf_life) if perishable else 0
        ss = float(safety_stock.get(iid, 0.0))

        for d in range(n_days):
            demand_d = demand_by_day.get(iid, {}).get(d, 0.0)
            prev = initial_total[iid] if d == 0 else inv[iid, d - 1]

            # Stock balance:
            #   inv[d] = prev + arrivals[d] - demand[d] - waste[d] + demand_short[d]
            # demand_short bumps inv to >= 0 when demand cannot be met (lost
            # sales — backlog is not carried).  waste disposes aged stock.
            prob += (
                inv[iid, d]
                == prev + _arrivals(iid, d) - demand_d - waste[iid, d] + demand_short[iid, d],
                f"inv_bal_{iid}_{d}",
            )

            # Soft safety-buffer target (relaxed to 0 on the last horizon day).
            target_ss = ss if d < n_days - 1 else 0.0
            prob += (
                inv[iid, d] + safety_short[iid, d] >= target_ss,
                f"cov_{iid}_{d}",
            )

            if perishable:
                # Held-stock expiry cap: what is on hand at end of day d cannot
                # exceed stock that arrived within the last `sl` days plus the
                # opening stock still unexpired at day d.  This never blocks
                # ordering a whole pack; it just forces disposal of aged stock.
                window_arrivals = pulp.lpSum(
                    _arrivals(iid, dd)
                    for dd in range(max(0, d - sl + 1), d + 1)
                )
                prob += (
                    inv[iid, d] <= window_arrivals + fresh_initial[iid].get(d, 0.0),
                    f"expiry_hold_{iid}_{d}",
                )
            else:
                # Non-perishables never spoil.
                prob += waste[iid, d] == 0, f"no_waste_{iid}_{d}"

    # Link q → deliver: can only order on an open delivery day
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

    # MOV: if a supplier delivers on day d, order value must meet min_order_value
    for s_id in all_sup_ids:
        sup = sup_by_id.get(s_id, {})
        mov = float(sup.get("min_order_value") or 0.0)
        if mov <= 0:
            continue
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
    # Solve
    # ------------------------------------------------------------------
    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=30)
    prob.solve(solver)

    if prob.status != 1:  # 1 = Optimal
        status_name = pulp.LpStatus.get(prob.status, "unknown")
        logger.warning(
            "Time-phased MILP non-optimal (status=%d/%s); falling back to greedy.",
            prob.status, status_name,
        )
        raise RuntimeError(f"MILP non-optimal: {status_name}")

    # ------------------------------------------------------------------
    # Extract solution
    # ------------------------------------------------------------------
    orders: List[PlanOrder] = []
    total_cost = 0.0

    # Per-ingredient hard shortfall (uncoverable forecasted demand).
    short_by_ing: Dict[int, float] = {}
    short_days_by_ing: Dict[int, List[int]] = {}
    for iid in active_ids:
        days = []
        tot = 0.0
        for d in range(n_days):
            v = pulp.value(demand_short.get((iid, d))) or 0.0
            if v > 0.5:
                tot += v
                days.append(d)
        short_by_ing[iid] = tot
        short_days_by_ing[iid] = days
    total_short = sum(short_by_ing.values())

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
                # at_risk if a hard demand shortfall exists on/before this
                # delivery day (early demand that cannot be filled in time).
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

    # Never drop an ingredient silently: any ingredient with uncoverable
    # forecasted demand but no order line gets an explicit at_risk marker.
    for iid in active_ids:
        if short_by_ing.get(iid, 0.0) <= 1.0 or iid in ordered_ings:
            continue
        ing = ing_by_id.get(iid, {})
        unit = ing.get("base_unit", "g")
        # Pick the cheapest catalog supplier (for context) even if unusable.
        cand: Optional[Tuple[int, float]] = None
        for s_id in all_sup_ids:
            if (iid, s_id) in cat_by_is:
                pr = float(cat_by_is[(iid, s_id)].get("current_price") or 0.0)
                if cand is None or pr < cand[1]:
                    cand = (s_id, pr)
        s_id = cand[0] if cand else 0
        price = cand[1] if cand else 0.0
        ld = lead_ceil.get(s_id, 1)
        days = short_days_by_ing.get(iid, [])
        deliver_day = min(max(ld, min(days) if days else 0), n_days - 1)
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
                f"{','.join(str(x) for x in days)} — no lead-feasible / in-stock supply"
            ),
        ))

    # Add delivery charges to total_cost
    for s_id in all_sup_ids:
        sup = sup_by_id.get(s_id, {})
        dc = float(sup.get("delivery_charge") or 0.0)
        ld = lead_ceil[s_id]
        for d in range(ld, n_days):
            if (s_id, d) not in deliver:
                continue
            dv = pulp.value(deliver[s_id, d])
            if dv is not None and dv > 0.5:
                total_cost += dc

    coverage_ok = total_short <= 1.0
    return PlanSolution(
        orders=orders,
        total_cost=total_cost,
        method="milp",
        total_short=total_short,
        coverage_ok=coverage_ok,
        rationale=(
            f"Time-phased MILP: {len(orders)} order lines across "
            f"{len({(o.supplier_id, o.delivery_day) for o in orders if o.qty > 0})} "
            f"supplier-days."
            + ("" if coverage_ok else f" COVERAGE GAP: {total_short:.0f} base units uncoverable.")
        ),
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

    return PlanSolution(
        orders=orders,
        total_cost=sum(o.qty * o.unit_price for o in orders),
        method="greedy",
        rationale=f"Greedy fallback: {len(orders)} order lines.",
    )
