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


@dataclass
class ApprovalPolicySnapshot:
    approval_threshold: float


class ApprovalPolicy:
    """Runtime-configurable threshold for PO approval routing."""

    def __init__(self, approval_threshold: float = 500.0):
        self._approval_threshold = float(approval_threshold)
        self._lock = RLock()

    @property
    def approval_threshold(self) -> float:
        with self._lock:
            return self._approval_threshold

    def set_approval_threshold(self, threshold: float) -> ApprovalPolicySnapshot:
        with self._lock:
            self._approval_threshold = float(threshold)
            return self.snapshot()

    def snapshot(self) -> ApprovalPolicySnapshot:
        with self._lock:
            return ApprovalPolicySnapshot(
                approval_threshold=self._approval_threshold
            )
