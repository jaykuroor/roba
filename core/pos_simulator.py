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


def _hhmm_to_seconds(hhmm: str) -> int:
    """Convert an ``"HH:MM"`` clock string to seconds-into-day."""
    hours, minutes = hhmm.split(":")
    return int(hours) * 3600 + int(minutes) * 60


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

        self.dish_mix_weights = (row.dish_mix_weights if row is not None else None) or {}
        self.channel_mix = (
            (row.channel_mix if row is not None else None) or dict(config.CHANNEL_MIX)
        )


class POSSimulator:
    """Poisson order-arrival simulator (§10)."""

    def __init__(
        self,
        bus: Any,
        db_session_factory: Callable[[], Any],
        clock: Any,
        formatter: Any = None,
        rng: Any = None,
    ):
        self.bus = bus
        self.db_session_factory = db_session_factory
        self.clock = clock
        self.formatter = formatter
        # ``random`` (the module) and ``random.Random()`` share the same API
        # surface used here (random / choices); default to the module so the
        # spec's ``random()`` call is literal and seedable in tests.
        self._rng = rng or random
        # Rolling sim-time at which the next order arrives; lazily initialised
        # on the first tick so the very first arrival fires immediately.
        self.next_order_due: Optional[float] = None

    # -- formatter wiring ---------------------------------------------------

    def attach_formatter(self, formatter: Any) -> None:
        """Wire the formatter the simulator notifies on each created order."""
        self.formatter = formatter

    # -- daypart curve (§10 / §22) -----------------------------------------

    def daypart_weight(self, sim_time: float) -> float:
        """Daypart weight for ``sim_time`` from ``config.DAYPARTS``.

        Maps the current time-of-day to the matching daypart's weight; outside
        operating hours the weight is ``0`` (the restaurant is shut, no orders).
        """
        tod = sim_time % 86400
        for _name, (start, end, weight) in config.DAYPARTS.items():
            if _hhmm_to_seconds(start) <= tod < _hhmm_to_seconds(end):
                return float(weight)
        return 0.0

    # -- arrival rate / inter-arrival (§10) --------------------------------

    def _rate(self, sim_time: float, settings: _Settings) -> float:
        """Poisson arrival rate at ``sim_time`` (orders per sim-second)."""
        return (
            settings.base_orders_per_day
            * settings.velocity
            * self.daypart_weight(sim_time)
            / WINDOW_SECONDS
        )

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

        # Channel sampling population.
        channels = list(settings.channel_mix.keys())
        channel_weights = [float(settings.channel_mix[c]) for c in channels]
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

        order = Order(
            sim_time=sim_time,
            service_mode=channel,
            table_no=None,
            staff_id=None,
            guest_count=1,
            status="closed",
            channel=channel,
            total=order_total,
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

    # -- the tick (§10) -----------------------------------------------------

    def tick(self, sim_time: float) -> Optional[Order]:
        """Generate at most one order when ``sim_time`` reaches the next due
        arrival; persist it, notify the formatter, and schedule the next.

        Returns the created :class:`Order` (or ``None`` when not yet due / no
        order could be generated).
        """
        # Lazy init: make the first arrival due immediately.
        if self.next_order_due is None:
            self.next_order_due = sim_time

        if sim_time < self.next_order_due:
            return None

        generated = self.generate_order(sim_time)
        if generated is None:
            # No active menu items; retry on the next tick.
            return None

        order, lines = generated
        self._persist(order, lines)

        if self.formatter is not None:
            self.formatter.on_order(order, lines)

        self.next_order_due = sim_time + self._interval(sim_time)
        return order

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
