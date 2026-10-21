"""Global send pacer (collision-averse).

Serialises delivery across every campaign and worker so that:

  1. **no two emails are ever sent at the same instant** — every send reserves a
     strictly increasing slot from one shared, atomic counter;
  2. **a campaign never sends faster than its own rate** — its per-campaign
     cursor only ever moves forward, so spacing can only grow, never shrink;
  3. **collisions between campaigns are pushed forward by a random gap**
     (5-20 s by default) — overlapping schedules never transmit together and
     never compress each other's cadence.

The reservation is a single Redis Lua script, so concurrent workers can never
claim the same slot. When Redis is unavailable a process-local gate applies the
same rules (still preventing in-process collisions).
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_NEXT_KEY = "mailer:pacer:next"
_LAST_KEY = "mailer:pacer:last"
_LAST_CAMPAIGN_KEY = "mailer:pacer:last_campaign"

_GLOBAL_TTL_MS = 300_000  # 5 min — only needs to outlive a short collision gap
_CAMPAIGN_TTL_MS = 86_400_000  # 24 h — survives long cap-reset waits
_EPSILON = 0.001  # guarantees two slots are never identical

# Atomic reservation. KEYS: next, last, last_campaign, campaign_cursor.
_CLAIM_LUA = """
local now = tonumber(ARGV[1])
local own_gap = tonumber(ARGV[2])
local window = tonumber(ARGV[3])
local push = tonumber(ARGV[4])
local cid = ARGV[5]
local ttl_g = tonumber(ARGV[6])
local ttl_c = tonumber(ARGV[7])
local eps = tonumber(ARGV[8])

local next_free = tonumber(redis.call('GET', KEYS[1]) or '0')
local last_slot = tonumber(redis.call('GET', KEYS[2]) or '0')
local last_camp = redis.call('GET', KEYS[3]) or ''
local c_next = tonumber(redis.call('GET', KEYS[4]) or '0')

local slot = now
if c_next > slot then slot = c_next end
if next_free > slot then slot = next_free end

if last_camp ~= '' and last_camp ~= cid and (slot - last_slot) < window then
  slot = slot + push
end

redis.call('SET', KEYS[1], tostring(slot + eps), 'PX', ttl_g)
redis.call('SET', KEYS[2], tostring(slot), 'PX', ttl_g)
redis.call('SET', KEYS[3], cid, 'PX', ttl_g)
redis.call('SET', KEYS[4], tostring(slot + own_gap), 'PX', ttl_c)

return tostring(slot)
"""


def _redis_client():
    try:
        from app.extensions import _RedisHolder

        return _RedisHolder.get()
    except Exception:  # noqa: BLE001 - Redis is optional for pacing
        return None


class _LocalPacer:
    """Process-local fallback shared by every gate in this process."""

    _lock = threading.Lock()
    _next_free = 0.0
    _last_slot = 0.0
    _last_campaign = ""
    _cursors: dict[str, float] = {}

    @classmethod
    def reserve(
        cls, campaign_id: str, own_gap: float, window: float, push: float
    ) -> float:
        with cls._lock:
            now = time.time()
            slot = max(now, cls._cursors.get(campaign_id, 0.0), cls._next_free)
            if (
                cls._last_campaign
                and cls._last_campaign != campaign_id
                and (slot - cls._last_slot) < window
            ):
                slot += push
            cls._next_free = slot + _EPSILON
            cls._last_slot = slot
            cls._last_campaign = campaign_id
            cls._cursors[campaign_id] = slot + own_gap
            return slot

    @classmethod
    def reset(cls, campaign_id: str | None = None) -> None:
        with cls._lock:
            if campaign_id is None:
                cls._cursors.clear()
                cls._next_free = 0.0
                cls._last_slot = 0.0
                cls._last_campaign = ""
            else:
                cls._cursors.pop(campaign_id, None)


class PacingGate:
    """Paces a single campaign's sends against the global schedule."""

    def __init__(
        self,
        rate_per_minute: int,
        campaign_id: str = "default",
        collision_window: float = 5.0,
        gap_min: float = 5.0,
        gap_max: float = 20.0,
        rng: random.Random | None = None,
        client=None,
    ) -> None:
        self.rate = max(1, int(rate_per_minute))
        self.campaign_id = campaign_id or "default"
        self.own_gap = 60.0 / self.rate
        self.window = max(0.0, float(collision_window))
        self.gap_min = max(0.0, float(gap_min))
        self.gap_max = max(self.gap_min, float(gap_max))
        self._rng = rng or random
        self._client = client
        self.last_slot = 0.0

    # ------------------------------------------------------------------ #
    def _client_or_none(self):
        if self._client is not None:
            return self._client
        return _redis_client()

    def _reserve(self) -> float:
        push = self._rng.uniform(self.gap_min, self.gap_max)
        client = self._client_or_none()
        if client is not None:
            try:
                raw = client.eval(
                    _CLAIM_LUA,
                    4,
                    _NEXT_KEY,
                    _LAST_KEY,
                    _LAST_CAMPAIGN_KEY,
                    f"mailer:pacer:c:{self.campaign_id}",
                    time.time(),
                    self.own_gap,
                    self.window,
                    push,
                    self.campaign_id,
                    _GLOBAL_TTL_MS,
                    _CAMPAIGN_TTL_MS,
                    _EPSILON,
                )
                return float(raw)
            except Exception as exc:  # noqa: BLE001 - degrade to local pacing
                logger.warning("pacer Redis error, using local pacing: %s", exc)
        return _LocalPacer.reserve(
            self.campaign_id, self.own_gap, self.window, push
        )

    def wait(self) -> float:
        """Block until this campaign's next send slot, then return it (epoch)."""
        slot = self._reserve()
        delay = slot - time.time()
        if delay > 0:
            time.sleep(delay)
        self.last_slot = slot
        return slot

    def reset(self) -> None:
        """Forget this campaign's pacing cursor (call when it finishes)."""
        client = self._client_or_none()
        if client is not None:
            try:
                client.delete(f"mailer:pacer:c:{self.campaign_id}")
            except Exception:  # noqa: BLE001
                pass
        _LocalPacer.reset(self.campaign_id)


# --------------------------------------------------------------------------- #
# Adaptive cycle planner
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CyclePlan:
    """How much to send in one cycle and how long to wait before the next.

    ``chunk`` is the number of recipients to claim/send. ``in_batch_jitter`` is
    the random 0..N second delay to apply *between* those sends. ``next_wait``
    is the delay before the next cycle and always carries the per-email jitter,
    so a large jitter never has to be slept inside a single blocking task.
    ``defer`` means "claim nothing this cycle" (caller should wait/retry).
    """

    chunk: int = 0
    in_batch_jitter: float = 0.0
    next_wait: float = 1.0
    defer: bool = False


def plan_cycle(
    *,
    rate: int,
    jitter: float,
    allowance: int,
    batch_size: int,
    max_sends_per_tick: int,
    cycle_budget: float = 45.0,
    window_remaining: float | None = None,
    stale_limit: float = 900.0,
    rng: random.Random | None = None,
) -> CyclePlan:
    """Plan one adaptive send cycle that honours the jitter exactly.

    Sizing is based on the *worst-case* time for each email (``gate + jitter``)
    so a cycle can never overrun the cycle budget, the stale-claim limit or the
    remaining delivery window. When a single email does not fit (because the
    jitter is large), the jitter is fully deferred to ``next_wait`` instead of
    being reduced or slept — the email is sent now, the next one later, and
    nothing is skipped.
    """
    rng = rng or random
    gate = 60.0 / max(1, int(rate))
    jitter = max(0.0, float(jitter or 0))

    if allowance <= 0:
        return CyclePlan(defer=True)

    # Honour the full jitter: if it cannot fit before the window closes, wait
    # for the next window instead of silently shrinking it.
    if window_remaining is not None and window_remaining <= gate + jitter:
        return CyclePlan(defer=True)

    next_wait = gate + rng.uniform(0.0, jitter)

    # A jitter longer than the stale-claim window must never hold a claim.
    if jitter >= stale_limit:
        return CyclePlan(chunk=1, in_batch_jitter=0.0, next_wait=next_wait)

    hard = float(cycle_budget)
    hard = min(hard, float(stale_limit))
    if window_remaining is not None:
        hard = min(hard, float(window_remaining))
    if hard <= 0:
        return CyclePlan(defer=True)

    chunk = int(hard // (gate + jitter))
    if chunk <= 0:
        # The single send fits (checked above); only its jitter cannot fit in
        # this cycle, so defer that jitter to the next cycle.
        return CyclePlan(chunk=1, in_batch_jitter=0.0, next_wait=next_wait)

    chunk = max(1, min(chunk, int(allowance), int(batch_size), int(max_sends_per_tick)))
    in_batch = jitter if chunk > 1 else 0.0
    return CyclePlan(chunk=chunk, in_batch_jitter=in_batch, next_wait=next_wait)


def throughput_per_hour(rate: int, jitter: float, hourly_cap: int | None = None) -> float:
    """Estimated sends/hour for a rate + average jitter (0 means unlimited cap)."""
    gate = 60.0 / max(1, int(rate))
    jitter = max(0.0, float(jitter or 0))
    estimate = 3600.0 / (gate + jitter / 2.0)
    if hourly_cap:
        estimate = min(estimate, float(hourly_cap))
    return round(estimate, 1)


def estimated_campaign_hours(
    recipients: int, rate: int, jitter: float, hourly_cap: int | None = None
) -> float:
    """Rough wall-clock hours to send ``recipients`` at the given pacing."""
    per_hour = throughput_per_hour(rate, jitter, hourly_cap)
    if per_hour <= 0:
        return 0.0
    return round(max(0, int(recipients or 0)) / per_hour, 2)
