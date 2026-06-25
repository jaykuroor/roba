"""Runtime policy switches for demo-safe operational controls."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock


@dataclass
class InventorySignalPolicySnapshot:
    shortage_signals_enabled: bool


class InventorySignalPolicy:
    """In-memory switch for Track B shortage signal emission."""

    def __init__(self, shortage_signals_enabled: bool = True):
        self._shortage_signals_enabled = bool(shortage_signals_enabled)
        self._lock = RLock()

    @property
    def shortage_signals_enabled(self) -> bool:
        with self._lock:
            return self._shortage_signals_enabled

    def set_shortage_signals_enabled(self, enabled: bool) -> InventorySignalPolicySnapshot:
        with self._lock:
            self._shortage_signals_enabled = bool(enabled)
            return self.snapshot()

    def snapshot(self) -> InventorySignalPolicySnapshot:
        with self._lock:
            return InventorySignalPolicySnapshot(
                shortage_signals_enabled=self._shortage_signals_enabled
            )
