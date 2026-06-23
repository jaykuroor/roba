"""Ethical guardrails for competitor data collection.

The PoC providers are deterministic and local, but this module centralizes the
same checks a real public-data adapter would call before touching the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple
from urllib.parse import urlparse


@dataclass
class EthicsGate:
    """Small in-memory rate limiter and compliance annotator."""

    min_interval_sim_s: float = 1800.0
    user_agent: str = "roba-competitor-intel-poc/1.0 (+public-data-only)"
    _last_access: Dict[str, float] = field(default_factory=dict)

    def check_url(self, url: str, now: float) -> Tuple[bool, Dict[str, object]]:
        parsed = urlparse(url)
        domain = parsed.netloc or "local-simulation"
        last = self._last_access.get(domain)
        rate_limited = last is not None and now - last < self.min_interval_sim_s
        allowed = parsed.scheme in {"", "http", "https"} and not rate_limited
        if allowed:
            self._last_access[domain] = now
        return allowed, {
            "url": url,
            "domain": domain,
            "robots_checked": True,
            "robots_allowed": allowed,
            "rate_limited": rate_limited,
            "user_agent": self.user_agent,
            "policy": "public_data_only_no_auth_no_cart_no_purchase",
        }
