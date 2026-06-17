"""Voice intake pipeline (§11).

``VoiceProcessor.process`` runs the full §11 pipeline on a piece of transcribed
text:

1. **Extract** a structured fact with the LLM using the §11 extraction schema
   (``intent, entity_type, entity_ref, attribute, value, effective_window?,
   confidence``). When the LLM is unavailable (canned fallback) or unsure, a
   best-effort regex parser recovers the obvious intents (e.g. a "leave"
   keyword → ``set_leave``).
2. **Apply** the fact deterministically per intent (``_apply``) — never
   bypassing validation.
3. **Persist** a ``user_facts`` row (raw_text, extracted JSON, resulting_writes).
4. **Emit** a ``USER_FACT`` signal to all groups.
5. **Return** ``{extracted, resulting_writes, signal_id}``.

Voice is ``core`` infrastructure; per §11 it performs the receipt / leave /
count writes itself (the deeper, track-owned reactions happen when each agent
consumes the ``USER_FACT``).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

from .clock import DAY_CLOSE_OFFSET, DAY_OPEN_OFFSET, SECONDS_PER_DAY
from .config import EVENT_MULT
from .llm import CANNED_NOTE
from .models import (
    Attendance,
    EventLog,
    Ingredient,
    InventoryLedger,
    InventoryLevel,
    InventoryLot,
    MenuItem,
    Recipe,
    RecipeLine,
    Staff,
    Station,
    Supplier,
    SupplierCatalog,
    UserFact,
)
from .signals import SignalType

# §11 extraction schema (passed to the LLM as JSON mode).
EXTRACTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "entity_type": {"type": "string"},
        "entity_ref": {"type": "string"},
        "attribute": {"type": "string"},
        "value": {"type": "string"},
        "effective_window": {"type": "object"},
        "confidence": {"type": "number"},
    },
    "required": ["intent", "confidence"],
}

# Valid intents (§11).
INTENTS = {
    "add_menu_item", "edit_menu_item", "set_recipe", "add_inventory_count",
    "record_receipt", "set_attendance", "set_leave", "add_event",
    "set_supplier_price", "set_competitor", "add_review",
    "set_operational_constraint", "other",
}

_NUMBER_RE = r"(\d+(?:\.\d+)?)"
_UNIT_WORDS = {
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg", "kilogram": "kg",
    "kilograms": "kg", "g": "g", "gram": "g", "grams": "g",
    "l": "ml", "liter": "ml", "liters": "ml", "litre": "ml", "litres": "ml",
    "ml": "ml", "each": "each", "unit": "each", "units": "each",
    "piece": "each", "pieces": "each",
}


class VoiceProcessor:
    """STT text → LLM extraction → deterministic writes → ``USER_FACT`` (§11)."""

    def __init__(self, llm: Any, bus: Any, db_session_factory: Callable[[], Any]):
        self.llm = llm
        self.bus = bus
        self.db_session_factory = db_session_factory

    # -- public API ---------------------------------------------------------

    def process(self, raw_text: str) -> Dict[str, Any]:
        """Run the full §11 pipeline; see module docstring for the steps."""
        extracted = self._extract(raw_text)
        resulting_writes = self._apply(extracted)
        self._write_user_fact(raw_text, extracted, resulting_writes)
        signal_id = self._emit_user_fact(extracted, raw_text)
        return {
            "extracted": extracted,
            "resulting_writes": resulting_writes,
            "signal_id": signal_id,
        }

    # -- (1) extraction -----------------------------------------------------

    def _extract(self, raw_text: str) -> Dict[str, Any]:
        """Ask the LLM to extract the §11 schema; fall back to regex when the
        LLM is unavailable (canned) or returns an unusable result."""
        messages = [
            {
                "role": "system",
                "content": (
                    "You extract a single operational fact from a restaurant "
                    "manager's spoken note. Respond with JSON matching: intent, "
                    "entity_type, entity_ref, attribute, value, "
                    "effective_window (optional {start_sim,end_sim}), confidence "
                    "(0..1). intent is one of: " + ", ".join(sorted(INTENTS)) + "."
                ),
            },
            {"role": "user", "content": raw_text},
        ]
        result = self.llm.complete(
            messages, json_schema=EXTRACTION_SCHEMA, max_tokens=400, use_site="voice"
        )

        if not isinstance(result, dict):
            return self._regex_extract(raw_text)

        # LLM unavailable / unsure -> deterministic regex parse.
        intent = result.get("intent")
        if (
            result.get("note") == CANNED_NOTE
            or intent in (None, "", "other")
            or intent not in INTENTS
            or float(result.get("confidence") or 0.0) < 0.35
        ):
            regex = self._regex_extract(raw_text)
            # Prefer the regex result when it recognised a concrete intent.
            if regex.get("intent") != "other":
                return regex
            # Otherwise keep the (neutral) LLM dict but normalise its shape.
            return self._normalise(result, raw_text)

        return self._normalise(result, raw_text)

    def _normalise(self, extracted: Dict[str, Any], raw_text: str) -> Dict[str, Any]:
        """Ensure every §11 field is present (defaults for missing keys)."""
        out = {
            "intent": extracted.get("intent") or "other",
            "entity_type": extracted.get("entity_type") or "",
            "entity_ref": extracted.get("entity_ref"),
            "attribute": extracted.get("attribute") or "",
            "value": extracted.get("value"),
            "effective_window": extracted.get("effective_window"),
            "confidence": float(extracted.get("confidence") or 0.0),
        }
        return out

    # -- regex fallback (§11 — best-effort for obvious intents) ------------

    def _regex_extract(self, raw_text: str) -> Dict[str, Any]:
        text = raw_text.strip()
        low = text.lower()

        # set_leave: "<Name> is on leave/sick ..." / "... day off ..."
        if (
            "leave" in low
            or "is off" in low
            or "day off" in low
            or "sick" in low
            or "off sick" in low
            or "absent" in low
            or "unavailable" in low
            or "missing" in low
        ):
            name = self._first_name(text)
            window = self._window_from_text(low)
            status = "sick" if "sick" in low else "leave"
            if self._looks_like_station_absence(low, name):
                return {
                    "intent": "set_operational_constraint",
                    "entity_type": "station_or_skill",
                    "entity_ref": self._constraint_target(text, low),
                    "attribute": "capacity_absence",
                    "value": {
                        "raw_text": text,
                        "all_qualified_staff": self._all_qualified_staff_absent(low),
                    },
                    "effective_window": window,
                    "confidence": 0.65,
                }
            return {
                "intent": "set_leave",
                "entity_type": "staff",
                "entity_ref": name,
                "attribute": status,
                "value": status,
                "effective_window": window,
                "confidence": 0.7,
            }

        # record_receipt: "received 20 kg of tomatoes from GreenFarm at 2 ..."
        if ("receiv" in low or "got" in low or "delivery" in low) and " of " in low:
            receipt = self._parse_receipt(text, low)
            if receipt is not None:
                return receipt

        # add_event: "there's a parade ... Monday"
        if any(w in low for w in ("parade", "festival", "event", "concert", "match", "holiday")):
            window = self._window_from_text(low)
            return {
                "intent": "add_event",
                "entity_type": "event",
                "entity_ref": self._event_label(low),
                "attribute": "demand_multiplier",
                "value": EVENT_MULT,
                "effective_window": window,
                "confidence": 0.6,
            }

        # add_inventory_count: "we have 12 kg of flour left" / "count ..."
        if "count" in low or ("we have" in low and " of " in low) or "stock of" in low:
            count = self._parse_count(text, low)
            if count is not None:
                return count

        # add_menu_item: "add a Margherita pizza for 12 dollars"
        if low.startswith("add ") and ("menu" in low or "for" in low or "pizza" in low or "$" in low or "dollar" in low):
            item = self._parse_menu_item(text, low)
            if item is not None:
                return item

        return {
            "intent": "other",
            "entity_type": "",
            "entity_ref": None,
            "attribute": "",
            "value": text,
            "effective_window": None,
            "confidence": 0.2,
        }

    @staticmethod
    def _looks_like_station_absence(low: str, name: Optional[str]) -> bool:
        absence = any(w in low for w in ("absent", "unavailable", "missing", "off sick", "sick"))
        operational = any(
            w in low
            for w in (
                "station", "counter", "line", "cook", "chef", "worker",
                "staff", "making", "make", "prep",
            )
        )
        # If a real named staff member was found, the structured attendance
        # path is more precise. Avoid treating sentence-openers ("The") as a
        # staff name.
        has_staff_name = bool(name and name.lower() not in {"the", "all", "every", "no"})
        return absence and operational and not has_staff_name

    @staticmethod
    def _all_qualified_staff_absent(low: str) -> bool:
        return any(
            phrase in low
            for phrase in (
                "all ", "every ", "no one", "nobody", "none of",
                "all the possible", "everyone",
            )
        )

    @staticmethod
    def _constraint_target(text: str, low: str) -> str:
        patterns = [
            r"(?:the\s+)?([A-Za-z0-9 '&-]+?)\s+station",
            r"(?:making|make|prep|prepping)\s+([A-Za-z0-9 '&-]+?)(?:\s+are|\s+is|\s+was|\s+were|\s+absent|$)",
            r"([A-Za-z0-9 '&-]+?)\s+(?:cook|chef|worker|staff)",
        ]
        for pattern in patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if not m:
                continue
            target = m.group(1).strip(" .,'\"")
            if target and target.lower() not in {"the", "all", "possible", "station"}:
                return target
        return low

    # -- regex helpers ------------------------------------------------------

    @staticmethod
    def _first_name(text: str) -> Optional[str]:
        m = re.match(r"\s*([A-Z][a-zA-Z]+)", text)
        if m:
            return m.group(1)
        m = re.search(r"([A-Z][a-zA-Z]+)\s+is\b", text)
        return m.group(1) if m else None

    @staticmethod
    def _event_label(low: str) -> str:
        for w in ("parade", "festival", "concert", "match", "holiday", "event"):
            if w in low:
                return w
        return "event"

    def _window_from_text(self, low: str) -> Optional[Dict[str, float]]:
        """Map common phrases ("next week", "this Monday", "today") to a
        sim-second window using the current sim day geometry (§6.1)."""
        now = float(self.bus.sim_time)
        day_number = int(now // SECONDS_PER_DAY)
        dow = day_number % 7  # 0 = Monday

        def day_start(d: int) -> float:
            return d * SECONDS_PER_DAY + DAY_OPEN_OFFSET

        def day_end(d: int) -> float:
            return d * SECONDS_PER_DAY + DAY_CLOSE_OFFSET

        if "next week" in low:
            days_to_mon = (7 - dow) if dow != 0 else 7
            mon = day_number + days_to_mon
            return {"start": day_start(mon), "end": day_end(mon + 6)}

        if "this week" in low:
            mon = day_number - dow
            return {"start": day_start(mon), "end": day_end(mon + 6)}

        # A named weekday ("this Monday", "on Friday").
        weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday",
                    "saturday", "sunday"]
        for idx, name in enumerate(weekdays):
            if name in low:
                delta = (idx - dow) % 7
                # "next <weekday>" pushes a week out; a bare/"this" weekday that
                # already passed this week also rolls to the next occurrence.
                if "next" in low or delta == 0:
                    delta = delta or 7
                target = day_number + delta
                return {"start": day_start(target), "end": day_end(target)}

        if "tomorrow" in low:
            return {"start": day_start(day_number + 1), "end": day_end(day_number + 1)}
        if "today" in low:
            return {"start": day_start(day_number), "end": day_end(day_number)}

        return None

    def _parse_receipt(self, text: str, low: str) -> Optional[Dict[str, Any]]:
        m = re.search(
            _NUMBER_RE + r"\s*([a-zA-Z]+)?\s+of\s+([a-zA-Z ]+?)\s+from\s+([A-Za-z0-9 &'.-]+?)"
            r"(?:\s+(?:at|for)\s+\$?" + _NUMBER_RE + r")?(?:\s|$|\.)",
            text,
            re.IGNORECASE,
        )
        if not m:
            return None
        qty = float(m.group(1))
        unit = _UNIT_WORDS.get((m.group(2) or "").lower(), (m.group(2) or "each").lower())
        ingredient = m.group(3).strip()
        supplier = m.group(4).strip()
        price = float(m.group(5)) if m.group(5) else None
        # Trim trailing connective words that the loose supplier capture grabbed.
        supplier = re.sub(r"\s+(at|for)$", "", supplier, flags=re.IGNORECASE).strip()
        return {
            "intent": "record_receipt",
            "entity_type": "ingredient",
            "entity_ref": ingredient,
            "attribute": "receipt",
            "value": {
                "qty": qty, "unit": unit, "supplier": supplier, "price": price,
            },
            "effective_window": None,
            "confidence": 0.7,
        }

    def _parse_count(self, text: str, low: str) -> Optional[Dict[str, Any]]:
        m = re.search(
            _NUMBER_RE + r"\s*([a-zA-Z]+)?\s+of\s+([a-zA-Z ]+?)(?:\s+(?:left|remaining|in stock))?(?:\s|$|\.)",
            text,
            re.IGNORECASE,
        )
        if not m:
            return None
        qty = float(m.group(1))
        unit = _UNIT_WORDS.get((m.group(2) or "").lower(), (m.group(2) or "each").lower())
        ingredient = m.group(3).strip()
        return {
            "intent": "add_inventory_count",
            "entity_type": "ingredient",
            "entity_ref": ingredient,
            "attribute": "count",
            "value": {"qty": qty, "unit": unit},
            "effective_window": None,
            "confidence": 0.6,
        }

    def _parse_menu_item(self, text: str, low: str) -> Optional[Dict[str, Any]]:
        m = re.search(
            r"add\s+(?:a|an|the)?\s*([A-Za-z0-9 '&-]+?)\s+(?:for|at)\s+\$?" + _NUMBER_RE,
            text,
            re.IGNORECASE,
        )
        name = None
        price = None
        if m:
            name = m.group(1).strip()
            price = float(m.group(2))
        else:
            m2 = re.search(r"add\s+(?:a|an|the)?\s*([A-Za-z0-9 '&-]+)", text, re.IGNORECASE)
            if m2:
                name = m2.group(1).strip()
        if not name:
            return None
        # Strip a trailing "to the menu".
        name = re.sub(r"\s+to the menu$", "", name, flags=re.IGNORECASE).strip()
        return {
            "intent": "add_menu_item",
            "entity_type": "menu_item",
            "entity_ref": name,
            "attribute": "price",
            "value": price if price is not None else 0.0,
            "effective_window": None,
            "confidence": 0.6,
        }

    # -- (2) apply (deterministic per intent, §11) -------------------------

    def _apply(self, extracted: Dict[str, Any]) -> List[str]:
        intent = extracted.get("intent")
        handler = {
            "set_leave": self._apply_set_leave,
            "set_attendance": self._apply_set_leave,
            "record_receipt": self._apply_record_receipt,
            "add_event": self._apply_add_event,
            "add_menu_item": self._apply_add_menu_item,
            "add_inventory_count": self._apply_add_inventory_count,
        }.get(intent)
        if handler is None:
            return ["stored"]
        try:
            return handler(extracted)
        except Exception as exc:  # never let a bad parse crash the demo
            return [f"error:{type(exc).__name__}"]

    def _apply_set_leave(self, extracted: Dict[str, Any]) -> List[str]:
        """Write structured ``attendance`` rows — one per affected day in the
        window — as the queryable source of truth (§11). The Staff agent
        (Track A) joins these with ``staff_stations`` to compute coverage. A
        single human-readable ``event_log`` row is also written for the
        narrative feed (display only, §16)."""
        name = extracted.get("entity_ref")
        status = self._leave_status(extracted)
        window = extracted.get("effective_window") or self._window_from_text("today")
        start = float(window["start"])
        end = float(window["end"])
        start_day = int(start // SECONDS_PER_DAY)
        end_day = int(end // SECONDS_PER_DAY)
        reason = str(extracted.get("attribute") or status)

        writes: List[str] = []
        session = self.db_session_factory()
        try:
            staff = None
            if name:
                staff = (
                    session.query(Staff)
                    .filter(Staff.name.ilike(str(name)))
                    .first()
                )
            staff_id = staff.id if staff is not None else None
            staff_name = staff.name if staff is not None else (name or "unknown")

            for day in range(start_day, end_day + 1):
                row = Attendance(
                    staff_id=staff_id,
                    date_sim_day=day,
                    status=status,
                    daypart=None,  # whole day
                    reason=reason,
                    sim_time=day * SECONDS_PER_DAY + DAY_OPEN_OFFSET,
                )
                session.add(row)
                session.flush()
                writes.append(f"attendance:{row.id}")

            # Display-only narrative row (the queryable truth is in attendance).
            log = EventLog(
                sim_time=float(self.bus.sim_time),
                category="attendance",
                actor="voice",
                summary=(
                    f"{staff_name} marked {status} for "
                    f"day{start_day}" + (f"–day{end_day}" if end_day > start_day else "")
                ),
                detail={
                    "staff_id": staff_id,
                    "staff_name": staff_name,
                    "status": status,
                    "start_day": start_day,
                    "end_day": end_day,
                },
            )
            session.add(log)
            session.flush()
            writes.append(f"event_log:{log.id}")
            session.commit()
        finally:
            session.close()
        return writes

    @staticmethod
    def _leave_status(extracted: Dict[str, Any]) -> str:
        """Resolve ``leave`` vs ``sick`` (vs ``present``) from the extracted
        fact's text fields (default ``leave``)."""
        blob = " ".join(
            str(extracted.get(k) or "") for k in ("attribute", "value", "intent")
        ).lower()
        if "sick" in blob:
            return "sick"
        if "present" in blob or "back" in blob:
            return "present"
        return "leave"

    def _apply_record_receipt(self, extracted: Dict[str, Any]) -> List[str]:
        """Create an ``InventoryLot`` + a ``receipt`` ledger row (§11)."""
        value = extracted.get("value") or {}
        ing_name = extracted.get("entity_ref")
        qty = float(value.get("qty") or 0.0)
        unit = value.get("unit") or "each"
        supplier_name = value.get("supplier")
        price = value.get("price")
        now = float(self.bus.sim_time)

        writes: List[str] = []
        session = self.db_session_factory()
        try:
            ingredient = self._resolve_ingredient(session, ing_name, unit, create=True)
            supplier = self._resolve_supplier(session, supplier_name, create=True)

            # Ensure validation invariant: a newly-introduced ingredient must be
            # sold by ≥1 supplier (§12.3) — wire a catalog row if none exists.
            self._ensure_catalog(session, ingredient, supplier, price, unit)

            prior_on_hand = self._ledger_on_hand(session, ingredient.id)
            shelf_life = float(ingredient.shelf_life_days or 5.0)
            lot = InventoryLot(
                ingredient_id=ingredient.id,
                qty_on_hand=qty,
                unit=unit,
                purchase_price=float(price) if price is not None else 0.0,
                purchase_date=now,
                received_date=now,
                expiry_date=now + shelf_life * SECONDS_PER_DAY,
                supplier_id=supplier.id if supplier is not None else None,
                storage_location="main",
                status="active",
            )
            session.add(lot)
            session.flush()

            balance_after = prior_on_hand + qty
            ledger = InventoryLedger(
                ingredient_id=ingredient.id,
                lot_id=lot.id,
                delta_qty=qty,
                reason="receipt",
                ref_id=lot.id,
                sim_time=now,
                balance_after=balance_after,
            )
            session.add(ledger)
            session.flush()

            self._update_level_cache(session, ingredient.id, balance_after)

            writes.append(f"inventory_lot:{lot.id}")
            writes.append(f"inventory_ledger:{ledger.id}")
            session.commit()
        finally:
            session.close()
        return writes

    def _apply_add_event(self, extracted: Dict[str, Any]) -> List[str]:
        """An event is stored as the ``USER_FACT`` itself (with its
        ``effective_window`` + demand multiplier); the Forecaster reads it
        (§18.4). Ensure the multiplier is carried on the fact."""
        if extracted.get("value") in (None, ""):
            extracted["value"] = EVENT_MULT
        if not extracted.get("attribute"):
            extracted["attribute"] = "demand_multiplier"
        return ["event_stored"]

    def _apply_add_menu_item(self, extracted: Dict[str, Any]) -> List[str]:
        """Create a ``menu_items`` row and an LLM-drafted (validated) recipe
        (§11). Prices must be > 0 (§12.3) so a missing price is rejected."""
        name = extracted.get("entity_ref")
        price = extracted.get("value")
        try:
            price = float(price)
        except (TypeError, ValueError):
            price = 0.0
        if not name or price <= 0:
            return ["rejected:invalid_menu_item"]

        writes: List[str] = []
        session = self.db_session_factory()
        try:
            station = session.query(Station).first()
            station_id = station.id if station is not None else None
            item = MenuItem(
                name=str(name),
                category="main",
                station_id=station_id,
                dine_in_price=price,
                online_price=round(price * 1.15, 2),
                prep_time_min=10.0,
                is_batchable=0,
                active=1,
                weather_tags=[],
                description=f"Added via voice: {name}",
            )
            session.add(item)
            session.flush()
            writes.append(f"menu_item:{item.id}")

            recipe = Recipe(menu_item_id=item.id)
            session.add(recipe)
            session.flush()
            writes.append(f"recipe:{recipe.id}")

            for line in self._draft_recipe_lines(session, str(name)):
                session.add(
                    RecipeLine(
                        recipe_id=recipe.id,
                        ingredient_id=line["ingredient_id"],
                        qty=line["qty"],
                        unit=line["unit"],
                        optional=0,
                    )
                )
                writes.append(f"recipe_line:{line['ingredient_id']}")
            session.commit()
        finally:
            session.close()
        return writes

    def _apply_add_inventory_count(self, extracted: Dict[str, Any]) -> List[str]:
        """Update ``inventory_levels.last_counted_*`` and write a
        ``reconciliation`` ledger delta (§11 / §18.8)."""
        value = extracted.get("value") or {}
        ing_name = extracted.get("entity_ref")
        counted = float(value.get("qty") or 0.0)
        now = float(self.bus.sim_time)

        writes: List[str] = []
        session = self.db_session_factory()
        try:
            ingredient = self._resolve_ingredient(session, ing_name, "each", create=False)
            if ingredient is None:
                return ["rejected:unknown_ingredient"]

            ledger_on_hand = self._ledger_on_hand(session, ingredient.id)
            delta = counted - ledger_on_hand

            level = (
                session.query(InventoryLevel)
                .filter(InventoryLevel.ingredient_id == ingredient.id)
                .first()
            )
            if level is None:
                level = InventoryLevel(
                    ingredient_id=ingredient.id,
                    par_level=0.0, reorder_point=0.0, safety_stock=0.0,
                    yield_factor=1.0, on_hand_cached=counted,
                )
                session.add(level)
            level.last_counted_at = now
            level.last_counted_qty = counted
            level.on_hand_cached = counted
            session.flush()
            writes.append(f"inventory_level:{ingredient.id}:count")

            ledger = InventoryLedger(
                ingredient_id=ingredient.id,
                lot_id=None,
                delta_qty=delta,
                reason="reconciliation",
                ref_id=ingredient.id,
                sim_time=now,
                balance_after=counted,
            )
            session.add(ledger)
            session.flush()
            writes.append(f"inventory_ledger:{ledger.id}")
            session.commit()
        finally:
            session.close()
        return writes

    # -- DB resolve / helpers ----------------------------------------------

    @staticmethod
    def _singular(name: str) -> str:
        n = name.strip().lower()
        if n.endswith("ies"):
            return n[:-3] + "y"
        if n.endswith("ses"):
            return n[:-2]
        if n.endswith("s") and not n.endswith("ss"):
            return n[:-1]
        return n

    def _resolve_ingredient(
        self, session: Any, name: Optional[str], unit: str, create: bool
    ) -> Optional[Ingredient]:
        if not name:
            return None
        target = self._singular(name)
        for ing in session.query(Ingredient).all():
            if self._singular(ing.name or "") == target:
                return ing
        if not create:
            return None
        base_unit = unit if unit in ("g", "ml", "each") else "g"
        ing = Ingredient(
            name=name.strip().title(),
            category="other",
            base_unit=base_unit,
            perishable=1,
            shelf_life_days=5.0,
            allergen_flags=[],
            weather_tags=[],
            notes="created via voice",
        )
        session.add(ing)
        session.flush()
        return ing

    def _resolve_supplier(
        self, session: Any, name: Optional[str], create: bool
    ) -> Optional[Supplier]:
        if not name:
            return None
        existing = (
            session.query(Supplier).filter(Supplier.name.ilike(name.strip())).first()
        )
        if existing is not None:
            return existing
        if not create:
            return None
        sup = Supplier(
            name=name.strip(),
            lead_time_days=2.0,
            reliability_score=0.9,
            min_order_value=0.0,
            contact="",
        )
        session.add(sup)
        session.flush()
        return sup

    def _ensure_catalog(
        self,
        session: Any,
        ingredient: Ingredient,
        supplier: Optional[Supplier],
        price: Optional[float],
        unit: str,
    ) -> None:
        if supplier is None:
            return
        existing = (
            session.query(SupplierCatalog)
            .filter(SupplierCatalog.ingredient_id == ingredient.id)
            .first()
        )
        if existing is not None:
            return
        session.add(
            SupplierCatalog(
                supplier_id=supplier.id,
                ingredient_id=ingredient.id,
                current_price=float(price) if price else 1.0,
                unit=unit,
                pack_size=1.0,
                availability="in_stock",
                updated_at=float(self.bus.sim_time),
            )
        )
        session.flush()

    @staticmethod
    def _ledger_on_hand(session: Any, ingredient_id: int) -> float:
        """Authoritative on-hand = Σ ledger deltas (§18.4)."""
        rows = (
            session.query(InventoryLedger.delta_qty)
            .filter(InventoryLedger.ingredient_id == ingredient_id)
            .all()
        )
        return float(sum((r[0] or 0.0) for r in rows))

    @staticmethod
    def _update_level_cache(session: Any, ingredient_id: int, on_hand: float) -> None:
        level = (
            session.query(InventoryLevel)
            .filter(InventoryLevel.ingredient_id == ingredient_id)
            .first()
        )
        if level is None:
            level = InventoryLevel(
                ingredient_id=ingredient_id,
                par_level=0.0, reorder_point=0.0, safety_stock=0.0,
                yield_factor=1.0, on_hand_cached=on_hand,
            )
            session.add(level)
        else:
            level.on_hand_cached = on_hand
        session.flush()

    def _draft_recipe_lines(self, session: Any, dish_name: str) -> List[Dict[str, Any]]:
        """Ask the LLM for an ingredient list; map names to existing ingredients
        (skip unknowns so the recipe always validates). Canned/empty → no lines
        (a recipe with zero lines trivially satisfies §12.3)."""
        messages = [
            {
                "role": "system",
                "content": (
                    "Draft a simple recipe ingredient list for a dish. Respond "
                    "with JSON {\"ingredients\": [{\"name\":str,\"qty\":number,"
                    "\"unit\":\"g|ml|each\"}]}."
                ),
            },
            {"role": "user", "content": f"Dish: {dish_name}"},
        ]
        schema = {
            "type": "object",
            "properties": {"ingredients": {"type": "array"}},
            "required": ["ingredients"],
        }
        drafted = self.llm.complete(
            messages, json_schema=schema, max_tokens=300, use_site="generation"
        )
        ingredients = []
        if isinstance(drafted, dict):
            ingredients = drafted.get("ingredients") or []

        lines: List[Dict[str, Any]] = []
        for entry in ingredients:
            if not isinstance(entry, dict):
                continue
            ing = self._resolve_ingredient(
                session, entry.get("name"), entry.get("unit") or "g", create=False
            )
            if ing is None:
                continue
            try:
                qty = float(entry.get("qty") or 0.0)
            except (TypeError, ValueError):
                qty = 0.0
            if qty <= 0:
                continue
            lines.append(
                {"ingredient_id": ing.id, "qty": qty, "unit": entry.get("unit") or ing.base_unit}
            )
        return lines

    # -- (3) persist the user fact -----------------------------------------

    def _write_user_fact(
        self, raw_text: str, extracted: Dict[str, Any], resulting_writes: List[str]
    ) -> int:
        session = self.db_session_factory()
        try:
            applied = 1 if resulting_writes and resulting_writes != ["stored"] else 0
            row = UserFact(
                raw_text=raw_text,
                source="voice",
                extracted=extracted,
                applied=applied,
                resulting_writes=resulting_writes,
                sim_time=float(self.bus.sim_time),
            )
            session.add(row)
            session.commit()
            return int(row.id)
        finally:
            session.close()

    # -- (4) emit USER_FACT -------------------------------------------------

    def _emit_user_fact(self, extracted: Dict[str, Any], raw_text: str) -> Optional[str]:
        window = extracted.get("effective_window")
        ttl = None
        if window and window.get("end") is not None:
            ttl = max(float(window["end"]) - float(self.bus.sim_time), 1.0)

        entity_ref = extracted.get("entity_ref")
        payload = {
            "intent": extracted.get("intent") or "other",
            "entity_type": extracted.get("entity_type") or "",
            "entity_ref": entity_ref if entity_ref is not None else "",
            "attribute": extracted.get("attribute") or "",
            "value": extracted.get("value"),
            "effective_window": window,
            "raw_text": raw_text,
        }
        signal = self.bus.emit(
            SignalType.USER_FACT, payload, source="voice", ttl=ttl
        )
        return signal.signal_id if signal is not None else None
