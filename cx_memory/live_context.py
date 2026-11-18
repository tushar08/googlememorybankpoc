"""Tier 1: real-time cross-channel context.

Memory Bank extraction is asynchronous (seconds), so a customer who leaves web chat and
immediately calls in needs something faster. This store keeps the last N turns per customer
across ALL channels and is injected into every new turn's session state.

PoC: in-process dict guarded by an asyncio lock (single gateway instance).
Production: swap for Memorystore (Redis) or Firestore with the same interface.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass
class Turn:
    channel: str
    customer_text: str
    agent_text: str
    ts: float


class LiveContextStore:
    def __init__(self, max_turns: int = 6) -> None:
        self._turns: dict[str, deque[Turn]] = defaultdict(lambda: deque(maxlen=max_turns))
        self._lock = asyncio.Lock()

    async def append(self, customer_id: str, channel: str, customer_text: str, agent_text: str) -> None:
        async with self._lock:
            self._turns[customer_id].append(Turn(channel, customer_text, agent_text, time.time()))

    def render(self, customer_id: str) -> str:
        turns = list(self._turns.get(customer_id, []))
        if not turns:
            return "(no recent activity on any channel)"
        lines = []
        for t in turns:
            age = int(time.time() - t.ts)
            lines.append(f"- [{t.channel}, {age}s ago] customer: {t.customer_text!r} | agent: {t.agent_text[:160]!r}")
        return "\n".join(lines)

    def channels_seen(self, customer_id: str) -> set[str]:
        return {t.channel for t in self._turns.get(customer_id, [])}
