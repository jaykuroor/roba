"""POS simulator (§10) — the live sales feed both tracks consume.

During ``RUNNING`` the simulator generates ``orders`` + ``order_lines`` with
Poisson arrivals whose rate follows the daypart curve. It writes the rows,
hands each created order to the :class:`~core.formatter.DataFormatter`
(``on_order``), and schedules the next arrival. Order lines are deliberately
*not* individual signals (they are high-volume); they live in ``order_lines``
and drive depletion via the in-process callback the formatter fires
(``bus.notify_order_line``). All *derived* events (waste, low-stock, …) are
real signals.
"""

from __future__ import annotations

import math
import random
from typing import Any, Callable, List, Optional, Tuple

from . import config
from .models import MenuItem, Order, OrderLine, SimSettings

# Operating window length used by the Poisson rate (§10): 54000 sim-seconds
# (08:00–23:00). Kept explicit because the rate formula divides by it verbatim.
WINDOW_SECONDS = 54000.0
# Catch up under high simulation speeds without letting one tick flood the DB.
MAX_ORDERS_PER_TICK = 25
ZERO_RATE_RETRY_SIM_S = 15.0
# Substrings that mark a ``Staff.role`` as kitchen labour for the ticket drain
# (seeds use cook / line_cook / fry_cook / grill_chef / head_chef).
COOK_ROLE_HINTS = ("cook", "chef")


def _hhmm_to_seconds(hhmm: str) -> int:
    """Convert an ``"HH:MM"`` clock string to seconds-into-day."""
    hours, minutes = hhmm.split(":")
    return int(hours) * 3600 + int(minutes) * 60


def active_injections(injections: Any, sim_time: float) -> List[dict]:
    """Return the ``anomaly_injections`` windows active at ``sim_time`` (§10).

    An injection is a dict ``{start?, end?, velocity_mult?, dish_mix_skew?}``
    with bounds in absolute sim-seconds; a missing ``start``/``end`` is treated
    as open on that side. Anything that is not a list of dicts yields ``[]``.
    """
    if not isinstance(injections, list):
        return []
    out: List[dict] = []
    for inj in injections:
        if not isinstance(inj, dict):
            continue
        start = inj.get("start")
        end = inj.get("end")
        try:
            if start is not None and sim_time < float(start):
                continue
            if end is not None and sim_time >= float(end):
                continue
        except (TypeError, ValueError):
            continue
        out.append(inj)
    return out


def _positive_mapping(raw: Any, fallback: dict) -> dict:
    if not isinstance(raw, dict):
        raw = {}
    normalized = {}
    for key, value in raw.items():
        try:
            weight = float(value)
        except (TypeError, ValueError):
            continue
        if weight > 0:
            normalized[str(key)] = weight
    return normalized or dict(fallback)


class _Settings:
    """A detached snapshot of the live, editable POS settings (§10).

    Reads from the ``sim_settings`` singleton, falling back to the §22 binding
    defaults when a field (or the row itself) is absent.
    """

    def __init__(self, row: Optional[SimSettings]):
        if row is not None and row.base_orders_per_day is not None:
            self.base_orders_per_day = float(row.base_orders_per_day)
        else:
            self.base_orders_per_day = float(config.BASE_ORDERS_PER_DAY)

        if row is not None and row.velocity is not None:
            self.velocity = float(row.velocity)
        else:
            self.velocity = 1.0

        self.dish_mix_weights = _positive_mapping(
            row.dish_mix_weights if row is not None else None,
            {},
        )
        self.channel_mix = _positive_mapping(
            row.channel_mix if row is not None else None,
            dict(config.CHANNEL_MIX),
        )
        self.anomaly_injections = (
            (row.anomaly_injections if row is not None else None) or []
        )
        # Optional weight overrides keyed by daypart name {name: weight}.
        # Time windows are always taken from config.DAYPARTS; only the weight
        # is overridden.  An absent or empty dict means "use config defaults".
        self.daypart_curve: dict = (
            (row.daypart_curve if row is not None else None) or {}
        )
        # "lifecycle" (default) runs the queued → cooking → served drain;
        # "instant" is the historical behaviour — every order is born served.
        mode = getattr(row, "kitchen_ticket_mode", None) if row is not None else None
        self.kitchen_ticket_mode: str = mode if mode in ("lifecycle", "instant") else "lifecycle"


class POSSimulator:
    """Poisson order-arrival simulator (§10)."""

    def __init__(
        self,
        bus: Any,
        db_session_factory: Callable[[], Any],
        clock: Any,
        formatter: Any = None,
        rng: Any = None,
        weather: Any = None,
    ):
        self.bus = bus
        self.db_session_factory = db_session_factory
        self.clock = clock
        self.formatter = formatter
        # Weather provider (§18.5): the POS applies the *channel* shift from the
        # current condition. Optional — when absent the channel mix is unshifted.
        self.weather = weather
        # ``random`` (the module) and ``random.Random()`` share the same API
        # surface used here (random / choices); default to the module so the
        # spec's ``random()`` call is literal and seedable in tests.
        self._rng = rng or random
        # Rolling sim-time at which the next order arrives; lazily initialised
        # on the first tick so the very first arrival fires immediately.
        self.next_order_due: Optional[float] = None
        # Sim-time of the previous tick, used to detect a backward clock jump
        # (stop / restart rewind sim_time) and restart the arrival schedule.
        self._last_tick_sim_time: Optional[float] = None
        # Fractional kitchen-drain capacity carried between ticks: at 1× a tick
        # is 15 sim-s, which buys well under one ticket, so whole tickets only
        # come due once enough ticks have accumulated.
        self._drain_credit: float = 0.0

    # -- formatter wiring ---------------------------------------------------

    def attach_formatter(self, formatter: Any) -> None:
        """Wire the formatter the simulator notifies on each created order."""
        self.formatter = formatter

    # -- daypart curve (§10 / §22) -----------------------------------------

    def daypart_weight(
        self, sim_time: float, settings: Optional["_Settings"] = None
    ) -> float:
        """Daypart weight for ``sim_time``.

        Maps the current time-of-day to the matching daypart's weight; outside
        operating hours the weight is ``0`` (the restaurant is shut, no orders).
        When ``settings`` carries a ``daypart_curve`` override, the stored
        weight for that daypart name replaces the ``config.DAYPARTS`` default.
        """
        tod = sim_time % 86400
        curve: dict = (settings.daypart_curve if settings is not None else {}) or {}
        for _name, (start, end, weight) in config.DAYPARTS.items():
            if _hhmm_to_seconds(start) <= tod < _hhmm_to_seconds(end):
                return float(curve.get(_name, weight))
        return 0.0

    # -- weather channel shift (§18.5) -------------------------------------

    def _weather_channel_shift(self) -> dict:
        """Per-channel multipliers for the current weather condition (§18.5).

        Reads the latest ``weather_log`` row via the wired weather provider and
        looks the condition up in :data:`config.WEATHER_CHANNEL_SHIFT`. Returns
        an empty dict (no shift) when no provider is wired, there is no current
        weather, or the condition has no defined shift (e.g. clear/clouds).
        """
        if self.weather is None:
            return {}
        try:
            current = self.weather.current()
        except Exception:  # weather is best-effort; never break order generation
            return {}
        if current is None:
            return {}
        return config.WEATHER_CHANNEL_SHIFT.get(current.condition, {})

    # -- arrival rate / inter-arrival (§10) --------------------------------

    def _rate(self, sim_time: float, settings: _Settings) -> float:
        """Poisson arrival rate at ``sim_time`` (orders per sim-second).

        Scaled by the product of ``velocity_mult`` over any ``anomaly_injections``
        active at ``sim_time`` (§10) — this is how scenario velocity surges spike
        and then subside at their window's end.
        """
        rate = (
            max(0.0, settings.base_orders_per_day)
            * max(0.0, settings.velocity)
            * max(0.0, self.daypart_weight(sim_time, settings))
            / WINDOW_SECONDS
        )
        for inj in active_injections(settings.anomaly_injections, sim_time):
            mult = inj.get("velocity_mult")
            if mult is not None:
                try:
                    rate *= max(0.0, float(mult))
                except (TypeError, ValueError):
                    continue
        return rate

    def _interval(self, sim_time: float, settings: Optional[_Settings] = None) -> float:
        """Exponential inter-arrival for ``sim_time`` (``inf`` when rate≤0)."""
        if settings is None:
            settings = self._read_settings()
        rate = self._rate(sim_time, settings)
        if rate <= 0.0:
            return math.inf
        # −log(random()) / rate; guard log(0) when random() returns 0.0.
        u = max(self._rng.random(), 1e-12)
        return -math.log(u) / rate

    def next_order_interval_sim_s(self) -> float:
        """Sim-seconds until the next Poisson arrival at the current sim-time."""
        return self._interval(self.clock.sim_time)

    # -- settings / menu reads ---------------------------------------------

    def _read_settings(self) -> _Settings:
        session = self.db_session_factory()
        try:
            return _Settings(session.get(SimSettings, 1))
        finally:
            session.close()

    def _active_menu_items(self, session: Any) -> List[MenuItem]:
        return list(
            session.query(MenuItem).filter(MenuItem.active == 1).all()
        )

    # -- order generation (§10) --------------------------------------------

    def generate_order(self, sim_time: float) -> Optional[Tuple[Order, List[OrderLine]]]:
        """Build (but do not persist) one ``Order`` and its ``OrderLine`` rows.

        Samples ``n_lines`` from :data:`config.LINES_PER_ORDER`, each line's
        menu item from ``sim_settings.dish_mix_weights`` (restricted to active
        items), the channel from ``sim_settings.channel_mix``, prices by channel
        (dine_in/takeout → dine_in_price, delivery → online_price), and applies
        :data:`config.CANCEL_RATE` per line (voided). The simulator persists the
        returned rows after this call. Returns ``None`` if no active item exists.
        """
        session = self.db_session_factory()
        try:
            settings = _Settings(session.get(SimSettings, 1))
            items = self._active_menu_items(session)
            if not items:
                return None
            session.expunge_all()
        finally:
            session.close()

        items_by_id = {it.id: it for it in items}

        # Build the dish-mix sampling population: active items present in the
        # weights map; fall back to a uniform mix over all active items.
        population: List[int] = []
        weights: List[float] = []
        for item_id, weight in settings.dish_mix_weights.items():
            try:
                iid = int(item_id)
            except (TypeError, ValueError):
                continue
            if iid in items_by_id and float(weight) > 0:
                population.append(iid)
                weights.append(float(weight))
        if not population:
            population = [it.id for it in items]
            weights = [1.0] * len(population)

        # Anomaly dish-mix skew (§10): multiply each item's weight by any
        # matching ``dish_mix_skew`` from injections active at ``sim_time``.
        injections = active_injections(settings.anomaly_injections, sim_time)
        for inj in injections:
            skew = inj.get("dish_mix_skew")
            if not isinstance(skew, dict):
                continue
            for idx, iid in enumerate(population):
                factor = skew.get(str(iid))
                if factor is not None:
                    try:
                        weights[idx] *= max(0.0, float(factor))
                    except (TypeError, ValueError):
                        continue
        if not any(weight > 0 for weight in weights):
            weights = [1.0] * len(population)

        # Channel sampling population, shifted by the current weather condition
        # (§18.5): ``random.choices`` does not require normalised weights, so a
        # plain per-channel multiply suffices.
        channels = list(settings.channel_mix.keys())
        channel_weights = [float(settings.channel_mix[c]) for c in channels]
        shift = self._weather_channel_shift()
        if shift:
            channel_weights = [
                w * max(0.0, float(shift.get(c, 1.0))) for c, w in zip(channels, channel_weights)
            ]
        if not channels or not any(weight > 0 for weight in channel_weights):
            channels = list(config.CHANNEL_MIX.keys())
            channel_weights = [float(config.CHANNEL_MIX[c]) for c in channels]
        channel = self._rng.choices(channels, weights=channel_weights, k=1)[0]

        # n_lines ~ LINES_PER_ORDER.
        line_counts = list(config.LINES_PER_ORDER.keys())
        line_count_weights = [config.LINES_PER_ORDER[n] for n in line_counts]
        n_lines = self._rng.choices(line_counts, weights=line_count_weights, k=1)[0]

        lines: List[OrderLine] = []
        order_total = 0.0
        for _ in range(n_lines):
            item_id = self._rng.choices(population, weights=weights, k=1)[0]
            item = items_by_id[item_id]
            if channel == "delivery":
                unit_price = float(item.online_price or 0.0)
            else:  # dine_in or takeout
                unit_price = float(item.dine_in_price or 0.0)

            voided = self._rng.random() < config.CANCEL_RATE
            status = "voided" if voided else "sold"
            qty = 1.0
            line_total = qty * unit_price
            if not voided:
                order_total += line_total

            lines.append(
                OrderLine(
                    menu_item_id=item_id,
                    qty=qty,
                    unit_price=unit_price,
                    modifiers=[],
                    discount=0.0,
                    line_total=line_total,
                    status=status,
                    sim_time=sim_time,
                )
            )

        # Kitchen ticket lifecycle: in "lifecycle" mode the order joins the pass
        # and is drained by :meth:`_drain_tickets`; in "instant" mode it is born
        # served (the pre-lifecycle behaviour).
        lifecycle = settings.kitchen_ticket_mode == "lifecycle"
        order = Order(
            sim_time=sim_time,
            service_mode=channel,
            table_no=None,
            staff_id=None,
            guest_count=1,
            status="closed",
            channel=channel,
            total=order_total,
            kitchen_status="queued" if lifecycle else "served",
            served_at=None if lifecycle else sim_time,
        )
        return order, lines

    def _persist(self, order: Order, lines: List[OrderLine]) -> None:
        """Persist the order then its lines (FK back-fill), leaving the objects
        usable (detached but fully loaded) after the session closes."""
        session = self.db_session_factory()
        try:
            session.add(order)
            session.flush()  # assigns order.id
            for line in lines:
                line.order_id = order.id
                session.add(line)
            session.commit()
            session.refresh(order)
            for line in lines:
                session.refresh(line)
            session.expunge_all()
        finally:
            session.close()

    # -- kitchen ticket lifecycle -------------------------------------------

    def _cooks_present(self, session: Any, sim_time: float) -> int:
        """Number of kitchen staff on shift right now.

        Presence comes from :func:`core.availability._staff_available` (the same
        attendance rules that unstaff a station), so a sick cook slows the pass
        and blocks their station consistently.
        """
        from .availability import _staff_available  # noqa: PLC0415 (circular at import time)
        from .models import Staff  # noqa: PLC0415
        from track_a.agents.forecaster import current_daypart  # noqa: PLC0415

        day = int(sim_time // 86400)
        daypart = current_daypart(sim_time)
        staff = session.query(Staff).filter(Staff.active == 1).all()
        cooks = [s for s in staff if any(h in str(s.role or "").lower() for h in COOK_ROLE_HINTS)]
        # A preset with no cook-ish role names still has a kitchen — everybody
        # counts rather than leaving capacity permanently at zero.
        return sum(1 for s in (cooks or staff) if _staff_available(session, int(s.id), day, daypart))

    def _drain_tickets(self, sim_time: float, delta_sim: float, mode: str) -> None:
        """Advance kitchen tickets oldest-first at a staffing-dependent rate.

        One unit of capacity serves one ticket already on the pass and pulls one
        more off the queue, so throughput is exactly
        ``cooks × KITCHEN_TICKETS_PER_COOK_PER_HOUR`` and every ticket spends one
        slot ``cooking``. With nobody in the kitchen capacity is zero and the
        queue simply grows.
        """
        session = self.db_session_factory()
        try:
            if mode != "lifecycle":
                # Instant mode: flush whatever the lifecycle left behind, so
                # flipping the toggle empties the backlog instead of freezing it.
                open_tickets = (
                    session.query(Order)
                    .filter(Order.kitchen_status.in_(("queued", "cooking")))
                    .all()
                )
                for order in open_tickets:
                    order.kitchen_status = "served"
                    order.served_at = sim_time
                if open_tickets:
                    session.commit()
                return

            open_tickets = (
                session.query(Order)
                .filter(Order.kitchen_status.in_(("queued", "cooking")))
                .count()
            )
            if not open_tickets:
                # Nothing on the pass: skip the attendance reads, and drop any
                # part-ticket of credit so a quiet night cannot bank capacity
                # and instantly clear the next morning's first orders.
                self._drain_credit = 0.0
                return

            cooks = self._cooks_present(session, sim_time)
            credit = self._drain_credit + (
                cooks
                * float(config.KITCHEN_TICKETS_PER_COOK_PER_HOUR)
                * max(delta_sim, 0.0)
                / 3600.0
            )
            capacity = int(credit)
            self._drain_credit = credit - capacity
            if capacity <= 0:
                return

            touched = False
            for status, nxt in (("cooking", "served"), ("queued", "cooking")):
                rows = (
                    session.query(Order)
                    .filter(Order.kitchen_status == status)
                    .order_by(Order.sim_time.asc(), Order.id.asc())
                    .limit(capacity)
                    .all()
                )
                for order in rows:
                    order.kitchen_status = nxt
                    if nxt == "served":
                        order.served_at = sim_time
                    touched = True
            if touched:
                session.commit()
        finally:
            session.close()

    # -- the tick (§10) -----------------------------------------------------

    def tick(self, sim_time: float) -> Optional[Order]:
        """Generate due orders up to a small safety cap.

        At high sim speeds one real tick can cross several Poisson arrivals.
        Catching those up keeps POS velocity realistic; returning only the last
        created order preserves the old public shape for callers that ignore it.
        """
        # A backward jump means the clock was reset (stop/restart rewinds
        # sim_time to the start of the day). Restart the arrival schedule from
        # the new sim_time so the first order fires immediately rather than
        # waiting for sim_time to climb back to the stale next_order_due.
        previous_sim_time = self._last_tick_sim_time
        if previous_sim_time is not None and sim_time < previous_sim_time:
            self.next_order_due = sim_time
            self._drain_credit = 0.0
            previous_sim_time = None
        self._last_tick_sim_time = sim_time

        settings = self._read_settings()
        # Drain before generating so an order is never served in the same tick
        # it arrives. The first tick has no elapsed window to drain over.
        if previous_sim_time is not None:
            self._drain_tickets(
                sim_time, sim_time - previous_sim_time, settings.kitchen_ticket_mode
            )

        # Lazy init: make the first arrival due immediately.
        if self.next_order_due is None:
            self.next_order_due = sim_time

        created: Optional[Order] = None
        generated_count = 0
        while self.next_order_due is not None and sim_time >= self.next_order_due:
            if generated_count >= MAX_ORDERS_PER_TICK:
                break

            due_at = float(self.next_order_due)
            interval_at = due_at
            interval = self._interval(interval_at, settings)
            if not math.isfinite(interval) and due_at < sim_time:
                interval_at = sim_time
                interval = self._interval(interval_at, settings)
            if not math.isfinite(interval):
                self.next_order_due = sim_time + ZERO_RATE_RETRY_SIM_S
                break

            generated = self.generate_order(interval_at)
            self.next_order_due = interval_at + max(interval, 1e-6)
            if generated is None:
                # No active menu items; keep time moving instead of retrying
                # the same due timestamp on every tick.
                self.next_order_due = max(self.next_order_due, sim_time + 1.0)
                break

            order, lines = generated
            self._persist(order, lines)
            if self.formatter is not None:
                self.formatter.on_order(order, lines)
            created = order
            generated_count += 1

        return created

    # -- orchestrator registration (§10 / §17) -----------------------------

    def register(self, orchestrator: Any, interval_sim_s: Optional[float] = None) -> Any:
        """Register an interval-style trigger that runs every tick (§10).

        The trigger reads the live sim-time and calls :meth:`tick`, which
        returns early until the next arrival is due. The default cadence is the
        per-tick sim-advance at 1× (``60 × 0.25 = 15`` sim-s) so it fires about
        once per tick.
        """
        if interval_sim_s is None:
            interval_sim_s = 60.0 * 1.0 * 0.25
        return orchestrator.register(
            "interval",
            lambda: self.tick(self.clock.sim_time),
            interval_sim_s=interval_sim_s,
            name="pos_simulator",
        )
