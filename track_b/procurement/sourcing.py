"""MILP least-cost sourcing solver (call overhaul Phase 2).

Exposes two public entry-points with identical signatures:

  solve_sourcing(...)        -- PuLP/CBC MILP solve (falls back to greedy when
                               PuLP is unavailable or the solve is infeasible).
  solve_sourcing_greedy(...) -- greedy heuristic (pure-Python, always runnable).

Both return a SourcingSolution dataclass so callers are agnostic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import pulp as _pulp  # noqa: F401 -- checked at import time only
    _PULP_AVAILABLE = True
except ImportError:
    _PULP_AVAILABLE = False
    logger.warning("PuLP not available; sourcing will use the greedy fallback.")


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------

@dataclass
class SourcingSolution:
    """Outcome of a sourcing solve.

    assignments -- one dict per ingredient:
      {ingredient_id, supplier_id, qty, unit_price, landed_cost,
       is_default_change, reason}
    total_cost  -- sum of (unit_price * qty + amortised delivery) across all
      assignments.
    method      -- "milp" | "greedy" | "fallback"
    rationale   -- human-readable explanation of the main decisions.
    """
    assignments: List[Dict[str, Any]] = field(default_factory=list)
    total_cost: float = 0.0
    method: str = "fallback"
    rationale: str = ""


# ---------------------------------------------------------------------------
# Public entry-points
# ---------------------------------------------------------------------------

def solve_sourcing(
    ingredients: List[Dict[str, Any]],
    catalog: List[Dict[str, Any]],
    suppliers: List[Dict[str, Any]],
    demand: Dict[int, float],
    params: Optional[Dict[str, Any]] = None,
) -> SourcingSolution:
    """MILP least-cost sourcing solve.

    Falls back to solve_sourcing_greedy when PuLP is unavailable or the MILP
    is infeasible/errored.

    Args:
        ingredients: [{id, demand, perishable, shelf_life_days,
                        current_default_supplier_id}]
        catalog:     [{id, supplier_id, ingredient_id, current_price, pack_size,
                        availability, is_default, discount}]
                     ``discount`` is [{min_qty, unit_price}] or None.
        suppliers:   [{id, delivery_charge, min_order_value, lead_time_days,
                        reliability_score, volume_discount}]
        demand:      dict[ingredient_id -> float]
        params:      {switching_cost, horizon_days,
                      spoilage_penalty_multiplier=2.0, lead_risk_lambda=0.3}
    """
    if not _PULP_AVAILABLE:
        return solve_sourcing_greedy(ingredients, catalog, suppliers, demand, params)
    try:
        return _solve_milp(ingredients, catalog, suppliers, demand, params or {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("MILP solve failed (%s); falling back to greedy.", exc, exc_info=True)
        return solve_sourcing_greedy(ingredients, catalog, suppliers, demand, params)


def solve_sourcing_greedy(
    ingredients: List[Dict[str, Any]],
    catalog: List[Dict[str, Any]],
    suppliers: List[Dict[str, Any]],
    demand: Dict[int, float],
    params: Optional[Dict[str, Any]] = None,
) -> SourcingSolution:
    """Greedy least-cost sourcing solve (pure-Python, no external dependencies).

    For each ingredient: pick the cheapest available supplier; apply a
    switching-cost guard (only switch away from the current default when
    savings > switching_cost); amortise delivery charge across all ingredients
    sharing that supplier.

    Falls back to the pure-Python default-keeper when even greedy errors.
    """
    try:
        return _solve_greedy_impl(ingredients, catalog, suppliers, demand, params or {})
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Greedy solve failed (%s); keeping current defaults.", exc, exc_info=True
        )
        return _solve_fallback(ingredients, catalog, demand)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _empty_solution(method: str = "fallback") -> SourcingSolution:
    return SourcingSolution(
        assignments=[],
        total_cost=0.0,
        method=method,
        rationale="No ingredients to source or no demand data.",
    )


def _solve_fallback(
    ingredients: List[Dict[str, Any]],
    catalog: List[Dict[str, Any]],
    demand: Dict[int, float],
) -> SourcingSolution:
    """Pure-Python fallback: keep existing is_default=1 catalog assignments."""
    default_cat: Dict[int, Dict] = {}
    for c in catalog:
        if c.get("is_default"):
            default_cat[int(c["ingredient_id"])] = c

    assignments = []
    total_cost = 0.0
    for i in ingredients:
        iid = int(i["id"])
        d = float(demand.get(iid, 0.0))
        if d <= 0:
            continue
        c = default_cat.get(iid)
        if c is None:
            continue
        unit_price = float(c.get("current_price") or 0.0)
        landed = unit_price * d
        assignments.append({
            "ingredient_id": iid,
            "supplier_id": int(c["supplier_id"]),
            "qty": d,
            "unit_price": unit_price,
            "landed_cost": round(landed, 4),
            "is_default_change": False,
            "reason": "fallback_default",
        })
        total_cost += landed

    return SourcingSolution(
        assignments=assignments,
        total_cost=round(total_cost, 4),
        method="fallback",
        rationale="Fallback: retaining current default suppliers (no solve possible).",
    )


def _solve_greedy_impl(
    ingredients: List[Dict[str, Any]],
    catalog: List[Dict[str, Any]],
    suppliers: List[Dict[str, Any]],
    demand: Dict[int, float],
    params: Dict[str, Any],
) -> SourcingSolution:
    """Internal greedy implementation."""
    if not ingredients or not demand:
        return _empty_solution("greedy")

    switching_cost = float(params.get("switching_cost", 5.0))

    ing_by_id = {int(i["id"]): i for i in ingredients}
    sup_by_id = {int(s["id"]): s for s in suppliers}

    # Group available catalog entries by ingredient
    cat_by_ing: Dict[int, List[Dict]] = {}
    for c in catalog:
        if c.get("availability") == "out":
            continue
        iid = int(c["ingredient_id"])
        cat_by_ing.setdefault(iid, []).append(c)

    # First pass: cheapest supplier per ingredient
    tentative: Dict[int, Dict] = {}
    for iid_raw, d in demand.items():
        iid = int(iid_raw)
        if d <= 0:
            continue
        options = cat_by_ing.get(iid, [])
        if not options:
            continue
        best = min(options, key=lambda c: float(c.get("current_price") or 0.0))
        tentative[iid] = best

    # Second pass: apply switching-cost guard
    final: Dict[int, Dict] = {}
    for iid, best_cat in tentative.items():
        ing = ing_by_id.get(iid, {})
        cur_default = ing.get("current_default_supplier_id")
        new_s = int(best_cat["supplier_id"])

        if cur_default is None or int(cur_default) == new_s:
            final[iid] = best_cat
            continue

        d = float(demand.get(iid, 0.0))
        new_price = float(best_cat.get("current_price") or 0.0)

        # Find current default catalog entry
        default_cat = next(
            (c for c in cat_by_ing.get(iid, [])
             if int(c["supplier_id"]) == int(cur_default)),
            None,
        )
        if default_cat is None:
            # Current default unavailable; must switch
            final[iid] = best_cat
            continue

        old_price = float(default_cat.get("current_price") or 0.0)
        savings = (old_price - new_price) * d
        if savings > switching_cost:
            final[iid] = best_cat
        else:
            final[iid] = default_cat

    # Cover any ingredient not in tentative that has a default in stock
    for i in ingredients:
        iid = int(i["id"])
        d = float(demand.get(iid, 0.0))
        if d <= 0 or iid in final:
            continue
        cur_default = i.get("current_default_supplier_id")
        if cur_default is not None:
            c = next(
                (cc for cc in cat_by_ing.get(iid, [])
                 if int(cc["supplier_id"]) == int(cur_default)),
                None,
            )
            if c is not None:
                final[iid] = c

    # Count ingredients per supplier (delivery amortisation)
    sup_counts: Dict[int, int] = {}
    for iid, c in final.items():
        s_id = int(c["supplier_id"])
        sup_counts[s_id] = sup_counts.get(s_id, 0) + 1

    assignments = []
    total_cost = 0.0
    switch_notes: List[str] = []

    for iid, c in final.items():
        d = float(demand.get(iid, 0.0))
        if d <= 0:
            continue
        s_id = int(c["supplier_id"])
        unit_price = float(c.get("current_price") or 0.0)
        sup = sup_by_id.get(s_id, {})
        delivery_charge = float(sup.get("delivery_charge") or 0.0)
        n = max(sup_counts.get(s_id, 1), 1)
        amortised_delivery = delivery_charge / n
        landed = unit_price * d + amortised_delivery

        ing = ing_by_id.get(iid, {})
        cur_default = ing.get("current_default_supplier_id")
        is_change = cur_default is not None and int(cur_default) != s_id

        if is_change:
            switch_notes.append(f"ingredient {iid}: {cur_default}->>{s_id}")

        assignments.append({
            "ingredient_id": iid,
            "supplier_id": s_id,
            "qty": d,
            "unit_price": unit_price,
            "landed_cost": round(landed, 4),
            "is_default_change": is_change,
            "reason": "greedy_switch" if is_change else "greedy_default",
        })
        total_cost += landed

    if switch_notes:
        rationale = "Greedy switches: " + "; ".join(switch_notes) + "."
    else:
        rationale = "Greedy: all defaults kept (savings did not exceed switching cost)."

    return SourcingSolution(
        assignments=assignments,
        total_cost=round(total_cost, 4),
        method="greedy",
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# MILP implementation (PuLP required)
# ---------------------------------------------------------------------------

def _solve_milp(
    ingredients: List[Dict[str, Any]],
    catalog: List[Dict[str, Any]],
    suppliers: List[Dict[str, Any]],
    demand: Dict[int, float],
    params: Dict[str, Any],
) -> SourcingSolution:
    """Core MILP formulation using PuLP + CBC solver."""
    import pulp  # local import -- only reaches here when _PULP_AVAILABLE

    if not ingredients or not demand:
        return _empty_solution("milp")

    # Parameters
    switching_cost = float(params.get("switching_cost", 5.0))
    horizon_days = float(params.get("horizon_days", 7.0))
    spoil_pen_mult = float(params.get("spoilage_penalty_multiplier", 2.0))
    lead_risk_lambda = float(params.get("lead_risk_lambda", 0.3))

    ing_by_id = {int(i["id"]): i for i in ingredients}
    sup_by_id = {int(s["id"]): s for s in suppliers}

    # Filter out 'out' catalog entries; index by (ingredient_id, supplier_id)
    cat_by_is: Dict[tuple, Dict] = {}
    for c in catalog:
        if c.get("availability") == "out":
            continue
        key = (int(c["ingredient_id"]), int(c["supplier_id"]))
        cat_by_is[key] = c

    # Active ingredients: have demand > 0
    active_ings = [
        i for i in ingredients
        if float(demand.get(int(i["id"]), 0.0)) > 0
    ]
    if not active_ings:
        return _empty_solution("milp")

    # Map ingredient_id -> list of available supplier_ids
    ing_sups: Dict[int, List[int]] = {}
    for i in active_ings:
        iid = int(i["id"])
        ing_sups[iid] = [s_id for (ii, s_id) in cat_by_is if ii == iid]

    # Drop ingredients with zero available suppliers
    active_ings = [i for i in active_ings if ing_sups.get(int(i["id"]))]
    if not active_ings:
        return _empty_solution("milp")

    all_sup_ids = list({s_id for sups in ing_sups.values() for s_id in sups})

    # Big-M: 3x the largest single-ingredient demand
    M = max(float(demand.get(int(i["id"]), 0.0)) for i in active_ings) * 3.0
    M = max(M, 1.0)

    # ------------------------------------------------------------------
    # Decision variables
    # ------------------------------------------------------------------
    prob = pulp.LpProblem("sourcing", pulp.LpMinimize)

    x: Dict[tuple, Any] = {}      # qty of ingredient iid from supplier sid
    a: Dict[tuple, Any] = {}      # binary assignment
    b: Dict[tuple, Any] = {}      # binary, volume-discount tier reached
    x_disc: Dict[tuple, Any] = {} # qty billed at discount price
    w: Dict[tuple, Any] = {}      # spoilage excess

    for i in active_ings:
        iid = int(i["id"])
        for s_id in ing_sups[iid]:
            x[iid, s_id] = pulp.LpVariable(f"x_{iid}_{s_id}", lowBound=0, cat="Continuous")
            a[iid, s_id] = pulp.LpVariable(f"a_{iid}_{s_id}", cat="Binary")
            b[iid, s_id] = pulp.LpVariable(f"b_{iid}_{s_id}", cat="Binary")
            x_disc[iid, s_id] = pulp.LpVariable(f"xd_{iid}_{s_id}", lowBound=0, cat="Continuous")
            w[iid, s_id] = pulp.LpVariable(f"w_{iid}_{s_id}", lowBound=0, cat="Continuous")

    y: Dict[int, Any] = {
        s_id: pulp.LpVariable(f"y_{s_id}", cat="Binary") for s_id in all_sup_ids
    }

    # Supplier-level value-discount tier binaries (C4)
    # vol_tier[s_id][tier_idx] = binary: tier_idx-th threshold reached for supplier s_id
    vol_tier: Dict[int, List[Any]] = {}
    for s_id in all_sup_ids:
        sup = sup_by_id.get(s_id, {})
        vd = sup.get("volume_discount") or []
        vol_tier[s_id] = [
            pulp.LpVariable(f"vt_{s_id}_{ti}", cat="Binary")
            for ti in range(len(vd))
        ]

    # ------------------------------------------------------------------
    # Objective: minimise total landed cost
    # ------------------------------------------------------------------
    obj_terms = []

    for i in active_ings:
        iid = int(i["id"])
        cur_s = i.get("current_default_supplier_id")
        shelf_life = float(i.get("shelf_life_days") or 0.0)
        perishable = bool(i.get("perishable"))

        for s_id in ing_sups[iid]:
            c = cat_by_is[iid, s_id]
            sup = sup_by_id.get(s_id, {})
            p = float(c.get("current_price") or 0.0)

            # (1) Base item cost
            obj_terms.append(p * x[iid, s_id])

            # (6) Per-item volume-discount savings (qty threshold in grams)
            disc_tiers = c.get("discount") or []
            if disc_tiers:
                tier = disc_tiers[0]  # lowest threshold tier
                disc_p = float(tier.get("unit_price") or p)
                if disc_p < p:
                    obj_terms.append(-(p - disc_p) * x_disc[iid, s_id])

            # (5) Lead-time x reliability risk
            lead = float(sup.get("lead_time_days") or 1.0)
            reliability = float(sup.get("reliability_score") or 1.0)
            risk_coeff = lead_risk_lambda * lead * (1.0 - reliability)
            if risk_coeff > 0:
                obj_terms.append(risk_coeff * x[iid, s_id])

            # (4) Spoilage penalty for perishables with shelf < horizon
            if perishable and 0 < shelf_life < horizon_days:
                spoil_pen = spoil_pen_mult * p
                obj_terms.append(spoil_pen * w[iid, s_id])

        # (3) Switching cost: penalise moving away from current default
        if cur_s is not None:
            cur_s_int = int(cur_s)
            if cur_s_int in ing_sups[iid]:
                obj_terms.append(switching_cost * (1 - a[iid, cur_s_int]))

    # (2) Delivery charges
    for s_id in all_sup_ids:
        sup = sup_by_id.get(s_id, {})
        dc = float(sup.get("delivery_charge") or 0.0)
        if dc > 0:
            obj_terms.append(dc * y[s_id])

    # (7) Supplier-level value-discount rebates (C4)
    # When total spend for supplier s_id >= threshold, rebate discount_pct% of that spend.
    # Modelled with a linear approximation: rebate = discount_pct/100 * (order_value - threshold*tier)
    # using big-M linearisation to gate the tier binary.
    sup_order_val: Dict[int, Any] = {}
    for s_id in all_sup_ids:
        ings_from_s = [
            int(i["id"]) for i in active_ings
            if s_id in ing_sups.get(int(i["id"]), [])
        ]
        if ings_from_s:
            sup_order_val[s_id] = pulp.lpSum(
                float(cat_by_is.get((iid, s_id), {}).get("current_price") or 0.0)
                * x[iid, s_id]
                for iid in ings_from_s
            )
        sup = sup_by_id.get(s_id, {})
        vd = sup.get("volume_discount") or []
        for ti, tier in enumerate(vd):
            threshold = float(tier.get("min_value") or 0.0)
            disc_pct = float(tier.get("discount_pct") or 0.0)
            if threshold <= 0 or disc_pct <= 0 or s_id not in sup_order_val:
                continue
            vt = vol_tier[s_id][ti]
            ov = sup_order_val[s_id]
            # tier reached → order_value >= threshold
            prob += ov >= threshold * vt, f"vol_tier_lb_{s_id}_{ti}"
            # order_value not large enough → tier forced off (big-M)
            tier_M = M * 20  # order value is in currency, not grams
            prob += ov <= threshold + tier_M * vt, f"vol_tier_ub_{s_id}_{ti}"
            # rebate = discount_pct% of order value (only when tier reached)
            # approximated as: rebate = (disc_pct/100) * threshold * vt (conservative)
            rebate = (disc_pct / 100.0) * threshold * vt
            obj_terms.append(-rebate)

    prob += pulp.lpSum(obj_terms)

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    for i in active_ings:
        iid = int(i["id"])
        d = float(demand[iid])
        shelf_life = float(i.get("shelf_life_days") or 0.0)
        perishable = bool(i.get("perishable"))

        # Demand must be met
        prob += (
            pulp.lpSum(x[iid, s_id] for s_id in ing_sups[iid]) >= d,
            f"demand_{iid}",
        )

        # Single-source: exactly one supplier per ingredient
        prob += (
            pulp.lpSum(a[iid, s_id] for s_id in ing_sups[iid]) == 1,
            f"single_src_{iid}",
        )

        for s_id in ing_sups[iid]:
            c = cat_by_is[iid, s_id]
            p = float(c.get("current_price") or 0.0)

            # Link qty to assignment variable
            prob += x[iid, s_id] <= M * a[iid, s_id], f"link_qty_{iid}_{s_id}"

            # If assigned, supplier must be marked as used
            prob += y[s_id] >= a[iid, s_id], f"link_used_{iid}_{s_id}"

            # Availability cap for "limited" items
            if c.get("availability") == "limited":
                prob += x[iid, s_id] <= 2.0 * d, f"limited_{iid}_{s_id}"

            # Volume-discount tier constraints
            disc_tiers = c.get("discount") or []
            if disc_tiers:
                tier = disc_tiers[0]
                disc_threshold = float(tier.get("min_qty") or 0.0)
                disc_p = float(tier.get("unit_price") or p)
                if disc_threshold > 0 and disc_p < p:
                    prob += (
                        x[iid, s_id] >= disc_threshold * b[iid, s_id],
                        f"disc_thresh_{iid}_{s_id}",
                    )
                    prob += x_disc[iid, s_id] <= M * b[iid, s_id], f"disc_cap_{iid}_{s_id}"
                    prob += x_disc[iid, s_id] <= x[iid, s_id], f"disc_le_x_{iid}_{s_id}"
                else:
                    prob += b[iid, s_id] == 0, f"no_disc_b_{iid}_{s_id}"
                    prob += x_disc[iid, s_id] == 0, f"no_disc_xd_{iid}_{s_id}"
            else:
                prob += b[iid, s_id] == 0, f"no_disc_b_{iid}_{s_id}"
                prob += x_disc[iid, s_id] == 0, f"no_disc_xd_{iid}_{s_id}"

            # Spoilage excess variable
            if perishable and 0 < shelf_life < horizon_days:
                demand_before_expiry = d * shelf_life / horizon_days
                prob += (
                    w[iid, s_id] >= x[iid, s_id] - demand_before_expiry,
                    f"spoil_w_{iid}_{s_id}",
                )
            else:
                prob += w[iid, s_id] == 0, f"no_spoil_w_{iid}_{s_id}"

    # Min-order value constraints
    for s_id in all_sup_ids:
        sup = sup_by_id.get(s_id, {})
        min_order = float(sup.get("min_order_value") or 0.0)
        if min_order <= 0:
            continue
        ings_from_s = [
            int(i["id"]) for i in active_ings
            if s_id in ing_sups.get(int(i["id"]), [])
        ]
        if not ings_from_s:
            continue
        order_val = pulp.lpSum(
            float(cat_by_is.get((iid, s_id), {}).get("current_price") or 0.0)
            * x[iid, s_id]
            for iid in ings_from_s
        )
        # If supplier is used, order value must reach minimum
        prob += order_val >= min_order * y[s_id], f"min_order_{s_id}"

    # ------------------------------------------------------------------
    # Solve
    # ------------------------------------------------------------------
    solver = pulp.PULP_CBC_CMD(msg=0)
    prob.solve(solver)

    if prob.status != 1:  # 1 = Optimal
        status_name = pulp.LpStatus.get(prob.status, "unknown")
        logger.warning(
            "MILP not optimal (status=%d / %s); falling back to greedy.",
            prob.status,
            status_name,
        )
        # Surface infeasibility: run greedy but mark as fallback with a rationale
        greedy_sol = solve_sourcing_greedy(ingredients, catalog, suppliers, demand, params)
        greedy_sol.method = "fallback"
        greedy_sol.rationale = (
            f"MILP was {status_name} (status {prob.status}); greedy fallback used. "
            + (greedy_sol.rationale or "")
        ).strip()
        return greedy_sol

    # ------------------------------------------------------------------
    # Extract solution
    # ------------------------------------------------------------------
    sup_assignment_count: Dict[int, int] = {}
    assignments: List[Dict[str, Any]] = []
    total_cost = 0.0
    switch_notes: List[str] = []

    for i in active_ings:
        iid = int(i["id"])
        d = float(demand[iid])

        # Find the assigned supplier
        chosen_s: Optional[int] = None
        chosen_qty = 0.0
        for s_id in ing_sups[iid]:
            a_val = pulp.value(a[iid, s_id]) or 0.0
            if a_val > 0.5:
                chosen_s = s_id
                chosen_qty = float(pulp.value(x[iid, s_id]) or 0.0)
                break

        if chosen_s is None:
            # Should not happen at Optimal; pick cheapest as last resort
            chosen_s = min(
                ing_sups[iid],
                key=lambda s: float(cat_by_is.get((iid, s), {}).get("current_price") or 0.0),
            )
            chosen_qty = d

        sup_assignment_count[chosen_s] = sup_assignment_count.get(chosen_s, 0) + 1

        c = cat_by_is.get((iid, chosen_s), {})
        unit_price = float(c.get("current_price") or 0.0)
        cur_s = i.get("current_default_supplier_id")
        is_change = cur_s is not None and int(cur_s) != chosen_s

        if is_change:
            switch_notes.append(f"ingredient {iid}: {cur_s}->>{chosen_s}")

        assignments.append({
            "ingredient_id": iid,
            "supplier_id": chosen_s,
            "qty": round(chosen_qty, 4),
            "unit_price": unit_price,
            "landed_cost": 0.0,  # finalised below
            "is_default_change": is_change,
            "reason": "milp_switch" if is_change else "milp_optimal",
        })
        total_cost += unit_price * chosen_qty

    # Distribute delivery charges across assignments per supplier
    for asgn in assignments:
        s_id = asgn["supplier_id"]
        sup = sup_by_id.get(s_id, {})
        dc = float(sup.get("delivery_charge") or 0.0)
        n = max(sup_assignment_count.get(s_id, 1), 1)
        asgn["landed_cost"] = round(asgn["unit_price"] * asgn["qty"] + dc / n, 4)
        total_cost += dc / n

    if switch_notes:
        rationale = "MILP switches: " + "; ".join(switch_notes) + "."
    else:
        rationale = (
            "MILP: all assignments kept with current defaults or no cheaper "
            "alternative found after accounting for switching cost."
        )

    return SourcingSolution(
        assignments=assignments,
        total_cost=round(total_cost, 4),
        method="milp",
        rationale=rationale,
    )
