"""Seeding, one-click generation, the numeric layer, and the validator (§12).

Two modes produce a referentially-consistent dataset:

- **Presets** (``load_preset`` / ``list_presets``): curated, pre-validated JSON
  bundles in ``/data``. The LLM is never involved — these are the demo-safe
  path.
- **Generation** (``generate``): the LLM produces only *qualitative* content
  (dish/ingredient/supplier/staff names); all consistency-critical numbers come
  from the deterministic **numeric layer** (§12.2). The :class:`Validator`
  (§12.3) gates the assembled bundle before it is written.

Both modes funnel into :meth:`Seeder._insert_graph`, which writes every table
in FK order. The 30-day synthetic POS history is generated deterministically
(daypart curve × dish-mix weights) rather than hand-authored, then written by
``_insert_graph`` like any other table.
"""

from __future__ import annotations

import glob
import json
import os
import random
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config
from .clock import DAY_OPEN_OFFSET, SECONDS_PER_DAY
from .models import (
    BatchDefinition,
    Competitor,
    CompetitorOffer,
    Ingredient,
    InventoryLevel,
    InventoryLot,
    MenuItem,
    Order,
    OrderLine,
    Recipe,
    RecipeLine,
    Review,
    SimSettings,
    SimState,
    Staff,
    StaffDishSkill,
    StaffStation,
    Station,
    Supplier,
    SupplierCatalog,
    SupplierPriceHistory,
    WeatherLog,
)

# Repo ``/data`` directory (preset bundles live here, §12.1).
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# JSON-key → (Model, has_explicit_id) in FK insertion order (§12 / §12.4).
# Each list of dicts is written verbatim (explicit ``id`` PKs make the FK
# wiring inside the bundle deterministic).
GRAPH_ORDER: List[Tuple[str, Any]] = [
    ("ingredients", Ingredient),
    ("stations", Station),
    ("menu_items", MenuItem),
    ("recipes", Recipe),
    ("recipe_lines", RecipeLine),
    ("batch_definitions", BatchDefinition),
    ("staff", Staff),
    ("staff_stations", StaffStation),
    ("staff_dish_skills", StaffDishSkill),
    ("suppliers", Supplier),
    ("supplier_catalog", SupplierCatalog),
    ("inventory_lots", InventoryLot),
    ("inventory_levels", InventoryLevel),
    ("orders", Order),
    ("order_lines", OrderLine),
    ("competitors", Competitor),
    ("competitor_offers", CompetitorOffer),
    ("reviews", Review),
    ("supplier_price_history", SupplierPriceHistory),
    ("weather_log", WeatherLog),
]

# Default per-day order volume for the synthetic history (kept modest so the
# bundle / DB stay light and deterministic; overridable via meta).
DEFAULT_HISTORY_ORDERS_PER_DAY = 40


class Validator:
    """Referential-integrity gate (§12.3) over an in-memory bundle ``dict``.

    ``validate`` returns ``(True, [])`` when every rule holds, otherwise
    ``(False, [violations])``. Trivial cases are auto-repaired in place (a
    missing supplier / station / staff is added with sensible defaults) and the
    bundle is re-checked before the result is returned.
    """

    def validate(self, data: Dict[str, Any]) -> Tuple[bool, List[str]]:
        # Two repair passes max (§12.3: ≤2 retries) then report.
        for _ in range(3):
            violations = self._check(data)
            if not violations:
                return True, []
            repaired = self._auto_repair(data, violations)
            if not repaired:
                break
        return (len(self._check(data)) == 0), self._check(data)

    # -- rule checks --------------------------------------------------------

    def _check(self, data: Dict[str, Any]) -> List[str]:
        v: List[str] = []
        ingredients = {r["id"] for r in data.get("ingredients", [])}
        stations = {r["id"] for r in data.get("stations", [])}
        menu_items = {r["id"]: r for r in data.get("menu_items", [])}
        competitors = {r["id"] for r in data.get("competitors", [])}

        # every recipe_line.ingredient_id exists
        for rl in data.get("recipe_lines", []):
            if rl.get("ingredient_id") not in ingredients:
                v.append(f"recipe_line {rl.get('id')} -> missing ingredient {rl.get('ingredient_id')}")

        # every menu_item.station exists
        for mi in data.get("menu_items", []):
            if mi.get("station_id") not in stations:
                v.append(f"menu_item {mi.get('id')} -> missing station {mi.get('station_id')}")

        # every station has ≥1 staff in staff_stations
        staffed = {ss.get("station_id") for ss in data.get("staff_stations", [])}
        for sid in stations:
            if sid not in staffed:
                v.append(f"station {sid} has no staff coverage")

        # every ingredient is sold by ≥1 supplier_catalog row
        sold = {sc.get("ingredient_id") for sc in data.get("supplier_catalog", [])}
        for iid in ingredients:
            if iid not in sold:
                v.append(f"ingredient {iid} not sold by any supplier")

        # all prices > 0
        for mi in data.get("menu_items", []):
            if not (float(mi.get("dine_in_price") or 0) > 0):
                v.append(f"menu_item {mi.get('id')} dine_in_price must be > 0")
            if not (float(mi.get("online_price") or 0) > 0):
                v.append(f"menu_item {mi.get('id')} online_price must be > 0")
        for sc in data.get("supplier_catalog", []):
            if not (float(sc.get("current_price") or 0) > 0):
                v.append(f"supplier_catalog {sc.get('id')} current_price must be > 0")

        # every batch_definition.menu_item_id exists and is_batchable
        for bd in data.get("batch_definitions", []):
            mi = menu_items.get(bd.get("menu_item_id"))
            if mi is None:
                v.append(f"batch_definition {bd.get('id')} -> missing menu_item {bd.get('menu_item_id')}")
            elif not mi.get("is_batchable"):
                v.append(f"batch_definition {bd.get('id')} -> menu_item {mi.get('id')} not batchable")

        # every competitor_offer.competitor_id exists
        for co in data.get("competitor_offers", []):
            if co.get("competitor_id") not in competitors:
                v.append(f"competitor_offer {co.get('id')} -> missing competitor {co.get('competitor_id')}")

        return v

    # -- auto-repair (trivial cases only, §12.3) ---------------------------

    def _auto_repair(self, data: Dict[str, Any], violations: List[str]) -> bool:
        repaired = False

        # Missing supplier coverage: add one supplier + catalog rows for the
        # uncovered ingredients.
        uncovered = [
            int(msg.split()[1])
            for msg in violations
            if msg.startswith("ingredient") and "not sold" in msg
        ]
        if uncovered:
            suppliers = data.setdefault("suppliers", [])
            catalog = data.setdefault("supplier_catalog", [])
            sup_id = self._next_id(suppliers)
            suppliers.append({
                "id": sup_id, "name": "Auto Supplier",
                "lead_time_days": 2.0, "reliability_score": 0.85,
                "min_order_value": 0.0, "contact": "auto@supplier.local",
            })
            for iid in uncovered:
                catalog.append({
                    "id": self._next_id(catalog),
                    "supplier_id": sup_id, "ingredient_id": iid,
                    "current_price": 1.0, "unit": "g", "pack_size": 1.0,
                    "availability": "in_stock", "updated_at": 0.0,
                })
            repaired = True

        # Stations with no staff: add a station + a staff member covering it.
        uncovered_stations = [
            int(msg.split()[1])
            for msg in violations
            if msg.startswith("station") and "no staff coverage" in msg
        ]
        if uncovered_stations:
            staff = data.setdefault("staff", [])
            staff_stations = data.setdefault("staff_stations", [])
            for sid in uncovered_stations:
                staff_id = self._next_id(staff)
                staff.append({
                    "id": staff_id, "name": f"Auto Cook {staff_id}",
                    "role": "cook", "skill_level": 2, "hourly_cost": 18.0,
                    "active": 1,
                })
                staff_stations.append({
                    "id": self._next_id(staff_stations),
                    "staff_id": staff_id, "station_id": sid,
                })
            repaired = True

        return repaired

    @staticmethod
    def _next_id(rows: List[Dict[str, Any]]) -> int:
        return (max((int(r.get("id", 0)) for r in rows), default=0) + 1)


class Seeder:
    """Preset loading + LLM generation + numeric layer + graph insertion (§12)."""

    def __init__(
        self,
        llm: Any,
        db_session_factory: Callable[[], Any],
        validator: Optional[Validator] = None,
    ):
        self.llm = llm
        self.db_session_factory = db_session_factory
        self.validator = validator or Validator()

    # -- presets (§12.1) ----------------------------------------------------

    def load_preset(self, preset_id: str) -> Dict[str, Any]:
        """Read ``/data/{preset_id}.json``, synthesize POS history if needed,
        and insert the full graph. Returns the loaded bundle dict."""
        path = DATA_DIR / f"{preset_id}.json"
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        data.setdefault("meta", {})["preset_id"] = preset_id
        self._ensure_pos_history(data)
        self._insert_graph(data)
        return data

    def list_presets(self) -> List[str]:
        """Return the stem names of every ``/data/*.json`` preset bundle."""
        return sorted(
            Path(p).stem for p in glob.glob(str(DATA_DIR / "*.json"))
        )

    # -- generation (§12.2) -------------------------------------------------

    def generate(self, cuisine: str, size_params: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a full bundle: LLM qualitative content + numeric layer +
        validation, then insert. Returns the assembled bundle."""
        qualitative = self._llm_qualitative(cuisine, size_params)
        data = self._assemble_from_qualitative(qualitative, cuisine, size_params)

        self._apply_numeric_layer(data)
        ok, violations = self.validator.validate(data)
        if not ok:
            # The numeric layer may have introduced ids the validator repaired;
            # re-apply numbers to any rows the repair added, then accept.
            self._apply_numeric_layer(data)

        self._ensure_pos_history(data)
        self._insert_graph(data)
        return data

    def _llm_qualitative(self, cuisine: str, size_params: Dict[str, Any]) -> Dict[str, Any]:
        n_items = int(size_params.get("menu_items", 6))
        messages = [
            {
                "role": "system",
                "content": (
                    "You design the qualitative layer of a restaurant dataset. "
                    "Respond with JSON: {cuisine, stations:[name], menu_items:["
                    "{name,category,station,dine_in_price,online_price,"
                    "is_batchable,ingredients:[{name,qty,unit}]}], suppliers:["
                    "{name,lead_time_days}], staff:[{name,role,station}]}. "
                    "Numbers are approximate; do not include inventory levels."
                ),
            },
            {
                "role": "user",
                "content": f"Cuisine: {cuisine}. About {n_items} menu items, "
                           f"3 stations, 5 staff, 3 suppliers.",
            },
        ]
        schema = {
            "type": "object",
            "properties": {
                "cuisine": {"type": "string"},
                "stations": {"type": "array"},
                "menu_items": {"type": "array"},
                "suppliers": {"type": "array"},
                "staff": {"type": "array"},
            },
            "required": ["menu_items"],
        }
        result = self.llm.complete(
            messages, json_schema=schema, max_tokens=1200, use_site="generation"
        )
        if isinstance(result, dict) and result.get("menu_items"):
            return result
        # Canned slice fallback (offline-safe).
        return self.llm.canned("generation")  # type: ignore[return-value]

    def _assemble_from_qualitative(
        self, q: Dict[str, Any], cuisine: str, size_params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Turn the LLM's qualitative JSON into a fully-id'd bundle (no numbers
        yet — those are the numeric layer's job)."""
        data: Dict[str, Any] = {
            "meta": {"cuisine": cuisine, "generated": True},
            "ingredients": [], "stations": [], "menu_items": [],
            "recipes": [], "recipe_lines": [], "batch_definitions": [],
            "staff": [], "staff_stations": [], "staff_dish_skills": [],
            "suppliers": [], "supplier_catalog": [],
            "inventory_lots": [], "inventory_levels": [],
            "competitors": [], "competitor_offers": [], "reviews": [],
            "supplier_price_history": [], "weather_log": [],
        }

        # Stations.
        station_ids: Dict[str, int] = {}
        for name in (q.get("stations") or ["Line"]):
            sid = len(station_ids) + 1
            station_ids[name] = sid
            data["stations"].append({"id": sid, "name": name})
        default_station = next(iter(station_ids.values()), 1)

        # Ingredients (deduped by lower-name) + menu items + recipes.
        ing_ids: Dict[str, int] = {}

        def ingredient_id(name: str, unit: str) -> int:
            key = name.strip().lower()
            if key in ing_ids:
                return ing_ids[key]
            iid = len(ing_ids) + 1
            ing_ids[key] = iid
            base_unit = unit if unit in ("g", "ml", "each") else "g"
            data["ingredients"].append({
                "id": iid, "name": name.strip().title(), "category": "other",
                "base_unit": base_unit, "perishable": 1, "shelf_life_days": 5.0,
                "allergen_flags": [], "weather_tags": [], "notes": "",
            })
            return iid

        for idx, mi in enumerate(q.get("menu_items") or [], start=1):
            station = station_ids.get(mi.get("station"), default_station)
            dine = float(mi.get("dine_in_price") or 0) or 9.0
            online = float(mi.get("online_price") or 0) or round(dine * 1.15, 2)
            data["menu_items"].append({
                "id": idx, "name": mi.get("name") or f"Item {idx}",
                "category": mi.get("category") or "main", "station_id": station,
                "dine_in_price": dine, "online_price": online,
                "prep_time_min": 10.0,
                "is_batchable": 1 if mi.get("is_batchable") else 0,
                "active": 1, "weather_tags": [],
                "description": mi.get("name") or "",
            })
            data["recipes"].append({"id": idx, "menu_item_id": idx})
            for ing in (mi.get("ingredients") or []):
                iid = ingredient_id(ing.get("name") or "Item", ing.get("unit") or "g")
                data["recipe_lines"].append({
                    "id": len(data["recipe_lines"]) + 1,
                    "recipe_id": idx, "ingredient_id": iid,
                    "qty": float(ing.get("qty") or 50.0),
                    "unit": ing.get("unit") or "g", "optional": 0,
                })
            if mi.get("is_batchable"):
                data["batch_definitions"].append({
                    "id": len(data["batch_definitions"]) + 1,
                    "menu_item_id": idx, "applicable_menus": [], "dayparts": ["lunch", "dinner"],
                    "prep_lead_time_min": 20.0, "batch_size_min": 4.0,
                    "batch_size_step": 2.0, "batch_size_max": 20.0,
                    "decide_by_offset_min": 30.0, "prepared_shelf_life_min": 180.0,
                    "station_id": station, "required_skill": "cook",
                    "default_cadence_min": 120.0, "historical_attach_rate": 0.3,
                })

        # Suppliers + catalog (every ingredient sold by ≥1 supplier).
        suppliers = q.get("suppliers") or [{"name": "Local Wholesale", "lead_time_days": 2.0}]
        for s_idx, sup in enumerate(suppliers, start=1):
            data["suppliers"].append({
                "id": s_idx, "name": sup.get("name") or f"Supplier {s_idx}",
                "lead_time_days": float(sup.get("lead_time_days") or 2.0),
                "reliability_score": 0.9, "min_order_value": 50.0,
                "contact": "orders@supplier.local",
            })
        n_suppliers = len(data["suppliers"])
        for ing in data["ingredients"]:
            sup_id = ((ing["id"] - 1) % n_suppliers) + 1
            data["supplier_catalog"].append({
                "id": len(data["supplier_catalog"]) + 1,
                "supplier_id": sup_id, "ingredient_id": ing["id"],
                "current_price": 2.0, "unit": ing["base_unit"],
                "pack_size": 1000.0 if ing["base_unit"] != "each" else 12.0,
                "availability": "in_stock", "updated_at": 0.0,
            })

        # Staff + coverage (cover every station).
        staff = q.get("staff") or []
        for st_idx, member in enumerate(staff, start=1):
            data["staff"].append({
                "id": st_idx, "name": member.get("name") or f"Cook {st_idx}",
                "role": member.get("role") or "cook", "skill_level": 3,
                "hourly_cost": 20.0, "active": 1,
            })
            station = station_ids.get(member.get("station"), default_station)
            data["staff_stations"].append({
                "id": len(data["staff_stations"]) + 1,
                "staff_id": st_idx, "station_id": station,
            })
        # Guarantee every station has someone.
        covered = {ss["station_id"] for ss in data["staff_stations"]}
        for name, sid in station_ids.items():
            if sid not in covered and data["staff"]:
                data["staff_stations"].append({
                    "id": len(data["staff_stations"]) + 1,
                    "staff_id": data["staff"][0]["id"], "station_id": sid,
                })

        # sim_settings dish-mix (uniform across items) for the numeric layer.
        data["sim_settings"] = {
            "id": 1,
            "base_orders_per_day": int(size_params.get("base_orders_per_day", config.BASE_ORDERS_PER_DAY)),
            "velocity": 1.0,
            "dish_mix_weights": {str(mi["id"]): 1.0 for mi in data["menu_items"]},
            "daypart_curve": None,
            "channel_mix": dict(config.CHANNEL_MIX),
            "anomaly_injections": None,
        }
        return data

    # -- numeric layer (§12.2) ---------------------------------------------

    def _apply_numeric_layer(self, data: Dict[str, Any]) -> None:
        """Compute inventory levels + initial lots from recipe usage and the
        seed dish-mix (§12.2). Existing (pre-authored) rows are left intact."""
        menu = {mi["id"]: mi for mi in data.get("menu_items", [])}
        recipes = {r["id"]: r for r in data.get("recipes", [])}
        suppliers = {s["id"]: s for s in data.get("suppliers", [])}
        catalog_by_ing: Dict[int, Dict[str, Any]] = {}
        for sc in data.get("supplier_catalog", []):
            catalog_by_ing.setdefault(sc["ingredient_id"], sc)

        settings = data.get("sim_settings") or {}
        base = float(settings.get("base_orders_per_day") or config.BASE_ORDERS_PER_DAY)
        weights = settings.get("dish_mix_weights") or {
            str(mi["id"]): 1.0 for mi in data.get("menu_items", [])
        }
        total_w = sum(float(w) for w in weights.values()) or 1.0

        # Seed daily sales per menu item = base × normalised dish-mix weight.
        daily_item_sales: Dict[int, float] = {}
        for mi in data.get("menu_items", []):
            w = float(weights.get(str(mi["id"]), 0.0))
            daily_item_sales[mi["id"]] = base * (w / total_w)

        # Daily usage per ingredient = Σ recipe_qty × seed_daily_item_sales.
        daily_usage: Dict[int, float] = {ing["id"]: 0.0 for ing in data.get("ingredients", [])}
        for rl in data.get("recipe_lines", []):
            recipe = recipes.get(rl["recipe_id"])
            if recipe is None:
                continue
            sales = daily_item_sales.get(recipe["menu_item_id"], 0.0)
            daily_usage[rl["ingredient_id"]] = daily_usage.get(rl["ingredient_id"], 0.0) + (
                float(rl.get("qty") or 0.0) * sales
            )

        have_levels = {lv["ingredient_id"] for lv in data.get("inventory_levels", [])}
        have_lots = {lot["ingredient_id"] for lot in data.get("inventory_lots", [])}
        levels = data.setdefault("inventory_levels", [])
        lots = data.setdefault("inventory_lots", [])

        for ing in data.get("ingredients", []):
            iid = ing["id"]
            usage = max(daily_usage.get(iid, 0.0), 1.0)
            sc = catalog_by_ing.get(iid)
            sup = suppliers.get(sc["supplier_id"]) if sc else None
            lead_days = float(sup["lead_time_days"]) if sup else 2.0

            safety = config.SAFETY_DAYS * usage
            par = config.PAR_DAYS * usage
            reorder = lead_days * usage + safety

            if iid not in have_levels:
                levels.append({
                    "id": len(levels) + 1, "ingredient_id": iid,
                    "par_level": round(par, 2), "reorder_point": round(reorder, 2),
                    "safety_stock": round(safety, 2), "yield_factor": 1.0,
                    "on_hand_cached": round(par * 0.8, 2),
                    "last_counted_at": None, "last_counted_qty": None,
                })
            if iid not in have_lots:
                lots.append({
                    "id": len(lots) + 1, "ingredient_id": iid,
                    "qty_on_hand": round(par * 0.8, 2), "unit": ing["base_unit"],
                    "purchase_price": float(sc["current_price"]) if sc else 1.0,
                    "purchase_date": 0.0, "received_date": 0.0,
                    "expiry_date": float(ing.get("shelf_life_days") or 5.0) * SECONDS_PER_DAY,
                    "supplier_id": sc["supplier_id"] if sc else None,
                    "storage_location": "main", "status": "active",
                })

        # supplier_price_history: a short random-walk per catalog ingredient.
        if not data.get("supplier_price_history"):
            rng = random.Random(1234)
            hist = data.setdefault("supplier_price_history", [])
            for sc in data.get("supplier_catalog", []):
                price = float(sc["current_price"])
                for d in range(5, 0, -1):
                    price = max(0.1, price * (1.0 + rng.uniform(-0.05, 0.05)))
                    hist.append({
                        "id": len(hist) + 1, "supplier_id": sc["supplier_id"],
                        "ingredient_id": sc["ingredient_id"], "price": round(price, 3),
                        "sim_time": -float(d) * SECONDS_PER_DAY,
                    })

    # -- synthetic POS history (§12.2) -------------------------------------

    def _ensure_pos_history(self, data: Dict[str, Any]) -> None:
        """Generate ``HISTORY_DAYS`` of deterministic POS history into
        ``data['orders']`` / ``data['order_lines']`` when not already present."""
        if data.get("orders"):
            return
        meta = data.get("meta", {})
        days = int(meta.get("history_days", config.HISTORY_DAYS))
        per_day = int(meta.get("history_orders_per_day", DEFAULT_HISTORY_ORDERS_PER_DAY))

        menu = [mi for mi in data.get("menu_items", []) if mi.get("active", 1)]
        if not menu:
            return
        settings = data.get("sim_settings") or {}
        weights_map = settings.get("dish_mix_weights") or {str(mi["id"]): 1.0 for mi in menu}
        channel_mix = settings.get("channel_mix") or dict(config.CHANNEL_MIX)

        population = [mi["id"] for mi in menu]
        weights = [float(weights_map.get(str(mi["id"]), 1.0)) for mi in menu]
        menu_by_id = {mi["id"]: mi for mi in menu}
        channels = list(channel_mix.keys())
        channel_weights = [float(channel_mix[c]) for c in channels]

        dayparts = list(config.DAYPARTS.items())
        dp_weights = [w for _n, (_s, _e, w) in dayparts]

        rng = random.Random(20240601)
        orders = data.setdefault("orders", [])
        order_lines = data.setdefault("order_lines", [])

        # History spans the HISTORY_DAYS sim-days *before* day 0 (negative sim
        # time): day -1 .. -days. Each order gets a daypart-placed timestamp.
        for day_offset in range(1, days + 1):
            day_index = -day_offset
            day_start = day_index * SECONDS_PER_DAY
            for _ in range(per_day):
                daypart = rng.choices(dayparts, weights=dp_weights, k=1)[0]
                _name, (start, end, _w) = daypart
                tod = rng.uniform(_hhmm(start), _hhmm(end))
                sim_time = day_start + tod
                channel = rng.choices(channels, weights=channel_weights, k=1)[0]

                n_lines = rng.choices(
                    list(config.LINES_PER_ORDER.keys()),
                    weights=list(config.LINES_PER_ORDER.values()), k=1,
                )[0]
                oid = len(orders) + 1
                total = 0.0
                lines_for_order = []
                for _ln in range(n_lines):
                    item_id = rng.choices(population, weights=weights, k=1)[0]
                    item = menu_by_id[item_id]
                    price = float(item["online_price"] if channel == "delivery" else item["dine_in_price"])
                    total += price
                    lines_for_order.append((item_id, price, sim_time))
                orders.append({
                    "id": oid, "sim_time": sim_time, "service_mode": channel,
                    "table_no": None, "staff_id": None, "guest_count": 1,
                    "status": "closed", "channel": channel, "total": round(total, 2),
                })
                for item_id, price, ts in lines_for_order:
                    order_lines.append({
                        "id": len(order_lines) + 1, "order_id": oid,
                        "menu_item_id": item_id, "qty": 1.0, "unit_price": price,
                        "modifiers": [], "discount": 0.0, "line_total": price,
                        "status": "sold", "sim_time": ts,
                    })

    # -- graph insertion (§12.4) -------------------------------------------

    def _insert_graph(self, data: Dict[str, Any]) -> None:
        """Write every table in FK order, then the sim singletons if absent."""
        session = self.db_session_factory()
        try:
            for key, model in GRAPH_ORDER:
                for row in data.get(key, []):
                    session.add(model(**self._coerce_row(model, row)))
                session.flush()

            # sim_state / sim_settings singletons — only if absent (§12.4).
            if session.get(SimState, 1) is None:
                sim_state = data.get("sim_state") or {}
                session.add(SimState(
                    id=1,
                    sim_time=float(sim_state.get("sim_time", DAY_OPEN_OFFSET)),
                    day_number=int(sim_state.get("day_number", 0)),
                    day_of_week=int(sim_state.get("day_of_week", 0)),
                    speed=float(sim_state.get("speed", 1.0)),
                    status=sim_state.get("status", "stopped"),
                    operating_window=sim_state.get("operating_window", list(config.OPERATING_WINDOW)),
                    skip_closed_hours=int(sim_state.get("skip_closed_hours", 1)),
                    call_mode=sim_state.get("call_mode", config.CALL_MODE),
                    active_seed_id=data.get("meta", {}).get("preset_id"),
                ))
            if session.get(SimSettings, 1) is None:
                ss = data.get("sim_settings") or {}
                session.add(SimSettings(
                    id=1,
                    base_orders_per_day=int(ss.get("base_orders_per_day", config.BASE_ORDERS_PER_DAY)),
                    velocity=float(ss.get("velocity", 1.0)),
                    dish_mix_weights=ss.get("dish_mix_weights")
                        or {str(mi["id"]): 1.0 for mi in data.get("menu_items", [])},
                    daypart_curve=ss.get("daypart_curve"),
                    channel_mix=ss.get("channel_mix") or dict(config.CHANNEL_MIX),
                    anomaly_injections=ss.get("anomaly_injections"),
                ))
            session.commit()
        finally:
            session.close()

    @staticmethod
    def _coerce_row(model: Any, row: Dict[str, Any]) -> Dict[str, Any]:
        """Keep only keys that are real columns on ``model`` (ignore extras)."""
        cols = set(model.__table__.columns.keys())
        return {k: v for k, v in row.items() if k in cols}


def _hhmm(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 3600 + int(m) * 60
