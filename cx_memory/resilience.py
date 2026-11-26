"""Resilience: the customer conversation must never fail because memory is slow or down.

Read path : timeout budget -> circuit breaker -> fall back to Tier 1 (live context) + SOR profile.
Write path: failed or throttled writes go to a durable outbox with an idempotency key,
            are retried with exponential backoff, and land in a dead-letter queue after N attempts.
Fault injection lets the demo show each failure mode on demand.

Production: outbox = Pub/Sub or a Firestore collection drained by a Cloud Run job; breaker state
per instance is fine (each instance learns quickly); DLQ alerts to the on-call channel.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


class MemoryUnavailable(RuntimeError):
    pass


class FaultInjector:
    MODES = ("ok", "slow", "down", "throttled")

    def __init__(self) -> None:
        self.memory_bank = "ok"
        self.slow_ms = 1500

    async def before_call(self) -> None:
        if self.memory_bank == "down":
            raise MemoryUnavailable("injected fault: Memory Bank unavailable (503)")
        if self.memory_bank == "throttled":
            raise MemoryUnavailable("injected fault: Memory Bank quota exceeded (429)")
        if self.memory_bank == "slow":
            await asyncio.sleep(self.slow_ms / 1000)

    def view(self) -> dict:
        return {"memory_bank": self.memory_bank, "slow_ms": self.slow_ms}


class CircuitBreaker:
    def __init__(self, fail_threshold: int = 3, reset_after_s: float = 15.0) -> None:
        self.fail_threshold, self.reset_after_s = fail_threshold, reset_after_s
        self.failures = 0
        self.opened_at: float | None = None
        self.trips = 0

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if time.monotonic() - self.opened_at >= self.reset_after_s else "open"

    def allow(self) -> bool:
        return self.state != "open"

    def success(self) -> None:
        self.failures, self.opened_at = 0, None

    def failure(self) -> None:
        self.failures += 1
        if self.state == "half_open" or self.failures >= self.fail_threshold:
            if self.opened_at is None or self.state == "half_open":
                self.trips += 1
            self.opened_at = time.monotonic()

    def reset(self) -> None:
        self.failures, self.opened_at = 0, None

    def view(self) -> dict:
        return {"state": self.state, "failures": self.failures, "trips": self.trips,
                "fail_threshold": self.fail_threshold, "reset_after_s": self.reset_after_s}


def idempotency_key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


@dataclass
class OutboxItem:
    key: str
    op: str
    description: str
    run: Callable[[], Awaitable[None]]
    attempts: int = 0
    next_at: float = 0.0
    last_error: str = ""
    customer_token: str | None = None
    enqueued_at: float = field(default_factory=time.monotonic)

    def view(self) -> dict:
        return {"key": self.key, "op": self.op, "description": self.description, "attempts": self.attempts,
                "last_error": self.last_error, "customer_token": self.customer_token}


class Outbox:
    def __init__(self, max_attempts: int = 5, base_backoff_s: float = 1.0) -> None:
        self.items: dict[str, OutboxItem] = {}
        self.dlq: list[OutboxItem] = []
        self.delivered = 0
        self.deduplicated = 0
        self.max_attempts, self.base_backoff_s = max_attempts, base_backoff_s

    def enqueue(self, item: OutboxItem) -> bool:
        if item.key in self.items:
            self.deduplicated += 1
            return False
        self.items[item.key] = item
        return True

    async def drain(self, *, force: bool = False) -> dict:
        done, failed = 0, 0
        for key, item in list(self.items.items()):
            if not force and item.next_at > time.monotonic():
                continue
            item.attempts += 1
            try:
                await item.run()
            except Exception as exc:  # retry later
                item.last_error = str(exc)[:200]
                failed += 1
                if item.attempts >= self.max_attempts:
                    self.dlq.append(self.items.pop(key))
                else:
                    item.next_at = time.monotonic() + self.base_backoff_s * 2 ** (item.attempts - 1)
                continue
            self.items.pop(key, None)
            self.delivered += 1
            done += 1
        return {"delivered": done, "failed": failed, "pending": len(self.items), "dead_lettered": len(self.dlq)}

    def view(self) -> dict:
        return {"pending": len(self.items), "delivered": self.delivered, "deduplicated": self.deduplicated,
                "dead_lettered": len(self.dlq), "items": [i.view() for i in list(self.items.values())[:20]],
                "dlq": [i.view() for i in self.dlq[:20]]}
