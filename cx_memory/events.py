"""Interaction event bus (Kafka stand-in) and interaction taxonomy.

The memory layer is a *consumer and a publisher* on the enterprise interaction stream, not a new pipeline:
  * publishes one standardized interaction event per turn (taxonomy code, channel, token, refs; NO raw text);
  * consumes system-of-record events (case/dispute updated) and supersedes stale memories;
  * publishes memory lifecycle events (written, superseded, expired, erased) for audit and analytics.

Partition key = customer token, so all events for one customer stay ordered on one partition.
Production: Kafka / Pub/Sub topics with schema registry; consumers are idempotent on event_id.
"""
from __future__ import annotations

import re
import uuid
import zlib
from collections import defaultdict
from typing import Awaitable, Callable

from . import clock

TOPIC_INTERACTIONS = "cx.interaction.v1"
TOPIC_SOR_CASE = "sor.case.updated.v1"
TOPIC_MEMORY = "cx.memory.lifecycle.v1"
TOPIC_CRM = "crm.activity.v1"
N_PARTITIONS = 6

# Standardized interaction taxonomy (domain.subdomain.action). First match wins.
TAXONOMY: list[tuple[str, str]] = [
    ("SERVICING.DISPUTE.STATUS_INQUIRY", r"\b(dispute|disputed|hotel charge|DSP-\d+)\b"),
    ("CARD.LOST_STOLEN.REPLACEMENT", r"\b(replacement|lost|stolen|new card)\b"),
    ("CARD.AUTHORIZATION.DECLINE", r"\b(declined|decline)\b"),
    ("TRAVEL.NOTICE.CREATE", r"\b(trip|travel|flying|fly|abroad|lisbon)\b"),
    ("PROFILE.CONTACT_DETAILS.CHANGE", r"\b(new|change|update)\b.{0,30}\b(phone|number|email|address)\b"),
    ("PROFILE.CONTACT_PREFERENCE.UPDATE", r"\b(call me|contact me|email me|after \d|before \d)\b"),
    ("PRIVACY.MEMORY_CONSENT.CHANGE", r"\b(forget|stop remembering|delete my data|don'?t remember)\b"),
    ("MERCHANT_SERVICES.PRICING.INQUIRY", r"\b(pos|terminal|merchant)\b"),
]


def classify(text: str) -> str:
    for code, pattern in TAXONOMY:
        if re.search(pattern, text or "", re.IGNORECASE):
            return code
    return "GENERAL.INQUIRY.OTHER"


Handler = Callable[[dict], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self.log: dict[str, list[dict]] = defaultdict(list)
        self._subs: dict[str, list[Handler]] = defaultdict(list)
        self._seen: set[str] = set()

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._subs[topic].append(handler)

    async def publish(self, topic: str, key: str, payload: dict) -> dict:
        rec = {
            "topic": topic,
            "partition": zlib.crc32(key.encode()) % N_PARTITIONS,
            "offset": len(self.log[topic]),
            "key": key,
            "event_id": payload.get("event_id") or str(uuid.uuid4()),
            "published_at": clock.iso(clock.now()),
            "payload": payload,
        }
        self.log[topic].append(rec)
        if rec["event_id"] in self._seen:  # idempotent consumers: ignore redelivery
            return rec
        self._seen.add(rec["event_id"])
        for h in self._subs[topic]:
            await h(rec)
        return rec

    def tail(self, topic: str | None = None, n: int = 30) -> list[dict]:
        rows = self.log[topic] if topic else sorted((r for t in self.log.values() for r in t), key=lambda r: r["published_at"])
        return rows[-n:]
