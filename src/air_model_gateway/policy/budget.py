"""Per-service daily spend, for the policy's `daily_budget_usd` caps.

Spend is tracked per service per UTC day. `MemorySpendLedger` keeps it in
process (one replica); `RedisSpendLedger` keeps it in Redis so every replica
sees the same total, and falls back to the in-memory ledger while Redis is
unreachable (each replica then caps on its own view, which undercounts).

The cap is soft by design: it is checked before a call and the cost added
after, so calls already in flight when the budget is reached still complete
and count. Holding a reservation per call would make every paid call two
Redis round trips and still guess the cost wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from air_model_gateway.redis_store import RedisStore, RedisUnavailable

# Keys outlive their day by a day, so a replica whose clock is slightly behind
# still finds yesterday's total, and nothing accumulates.
_KEY_TTL_SECONDS = 2 * 24 * 3600


def utc_day(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).date().isoformat()


def seconds_until_next_utc_day(now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    return max(1, int((tomorrow - now).total_seconds()))


class SpendLedger(Protocol):
    async def spent_today(self, service: str) -> float: ...

    async def add(self, service: str, usd: float) -> None: ...


class MemorySpendLedger:
    def __init__(self) -> None:
        self._spent: dict[tuple[str, str], float] = {}

    async def spent_today(self, service: str) -> float:
        return self._spent.get((service, utc_day()), 0.0)

    async def add(self, service: str, usd: float) -> None:
        day = utc_day()
        # Drop earlier days as a new one starts, so the map stays one day wide.
        for key in [k for k in self._spent if k[1] != day]:
            del self._spent[key]
        self._spent[(service, day)] = self._spent.get((service, day), 0.0) + usd


class RedisSpendLedger:
    def __init__(self, store: RedisStore) -> None:
        self._store = store
        self._fallback = MemorySpendLedger()

    def _key(self, service: str) -> str:
        return self._store.key("spend", service, utc_day())

    async def spent_today(self, service: str) -> float:
        try:
            raw = await self._store.run("spend_get", lambda r: r.get(self._key(service)))
        except RedisUnavailable:
            return await self._fallback.spent_today(service)
        return float(raw) if raw is not None else 0.0

    async def add(self, service: str, usd: float) -> None:
        key = self._key(service)

        async def incr(r):  # type: ignore[no-untyped-def]
            async with r.pipeline(transaction=True) as pipe:
                pipe.incrbyfloat(key, usd)
                pipe.expire(key, _KEY_TTL_SECONDS)
                return await pipe.execute()

        try:
            await self._store.run("spend_add", incr)
        except RedisUnavailable:
            await self._fallback.add(service, usd)
