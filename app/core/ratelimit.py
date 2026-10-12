"""Send-rate pacing gate.

Enforces a minimum interval between individual email sends so the platform can
never hammer the SMTP provider, even if someone sets an aggressive rate.

The gate combines two limits:
  * a per-process throttle at the campaign's *effective* rate, and
  * a best-effort **shared** throttle stored in Redis at the *global* ceiling,
    so that multiple concurrent campaigns / workers also respect the aggregate
    cap.

The shared limit is advisory: if Redis is unavailable the local throttle still
protects the provider.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_GATE_KEY = "mailer:send_gate:last"
_GATE_TTL_MS = 120_000


class RateGate:
    """Paces sends to at most ``rate_per_minute`` emails per minute."""

    def __init__(self, rate_per_minute: int, global_rate_per_minute: int | None = None):
        self.rate = max(1, int(rate_per_minute))
        self.global_rate = max(self.rate, int(global_rate_per_minute or self.rate))
        self._local_gap = 60.0 / self.rate
        self._global_gap = 60.0 / self.global_rate
        self._last_local = 0.0

    def wait(self) -> None:
        """Block until the next send is allowed."""
        now = time.monotonic()

        local_remaining = max(0.0, self._local_gap - (now - self._last_local))
        global_remaining = self._global_remaining()

        delay = max(local_remaining, global_remaining)
        if delay > 0:
            time.sleep(delay)

        self._last_local = time.monotonic()
        self._mark_global(self._last_local)

    # -- shared (Redis) helpers, best-effort ---------------------------- #
    def _client(self):
        try:
            from app.extensions import _RedisHolder

            return _RedisHolder.get()
        except Exception:  # noqa: BLE001 - Redis is optional for pacing
            return None

    def _global_remaining(self) -> float:
        client = self._client()
        if client is None:
            return 0.0
        try:
            raw = client.get(_GATE_KEY)
            if raw is None:
                return 0.0
            last = float(raw)
        except Exception:  # noqa: BLE001
            return 0.0
        # Store wall-clock time (shared across processes).
        return max(0.0, self._global_gap - (time.time() - last))

    def _mark_global(self, _monotonic: float) -> None:
        client = self._client()
        if client is None:
            return
        try:
            client.set(_GATE_KEY, time.time(), px=_GATE_TTL_MS)
        except Exception:  # noqa: BLE001
            pass
