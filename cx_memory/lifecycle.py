"""Memory lifecycle: per-topic retention, supersession, expiry, erasure and legal hold.

Every memory the gateway writes gets a ledger record (id, topic, source, classification,
created/expiry time, status, version). The ledger is the system of record for *why* a memory
exists and when it must go. Memory Bank holds the text and the embedding.

  ACTIVE --(same subject written again)--> SUPERSEDED   e.g. dispute status changes
  ACTIVE --(TTL reached, sweep)----------> EXPIRED      e.g. trip ended 14 days ago
  ACTIVE --(right to erasure)------------> ERASED       blocked while a legal hold is set

On Vertex, explicit facts are created with a per-memory `ttl` (native expiry) and memory id = ledger id,
so supersede/erase is a delete by id. Memories that Memory Bank *generates* from conversation
(not in the ledger) fall under the engine-wide `ttl_config` set in scripts/setup_memory_bank.py.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from . import clock
from .governance import Assurance, Classification

DAY = 86400


@dataclass(frozen=True)
class TopicPolicy:
    topic: str
    ttl_days: int | None           # None = keep until superseded or erased (reviewed yearly)
    min_write_assurance: Assurance
    retention_basis: str
    priority: int                  # eviction order when a customer hits the memory cap (low goes first)


POLICIES: dict[str, TopicPolicy] = {p.topic: p for p in [
    TopicPolicy("user_personal_info", 365, Assurance.MEDIUM, "servicing context; re-verified yearly", 3),
    TopicPolicy("user_preferences", None, Assurance.MEDIUM, "until changed or consent withdrawn", 4),
    TopicPolicy("key_conversation_details", 90, Assurance.MEDIUM, "short-term conversational context", 1),
    TopicPolicy("explicit_instructions", None, Assurance.MEDIUM, "until the customer revokes it", 5),
    TopicPolicy("open_service_issues", 180, Assurance.MEDIUM, "until resolved + 30 days", 4),
    TopicPolicy("product_holdings", 30, Assurance.SYSTEM, "mirror of SOR; refreshed from SOR events", 2),
    TopicPolicy("travel_and_life_events", 180, Assurance.MEDIUM, "until event end date + 14 days", 3),
    TopicPolicy("contact_and_accessibility_needs", None, Assurance.MEDIUM, "until changed; RESTRICTED class", 5),
]}
RESOLVED_TTL_DAYS = 30
TRAVEL_GRACE_DAYS = 14

_REF = re.compile(r"\b(DSP|CASE)-\d+\b")
_TRIP = re.compile(r"\b(?:to|in|for)\s+([A-Z][a-z]{2,})")
_CONTACT_WINDOW = re.compile(r"\b(call|contact|reach)\w*\b.*\b(after|before|between|only|\d\s?(am|pm))\b", re.IGNORECASE)
_MONTHS = "january february march april may june july august september october november december".split()
_DATE_ISO = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
_DATE_TEXT = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d\d))?", re.IGNORECASE)


def subject_key(topic: str, text: str) -> str | None:
    """Two memories with the same subject key: the newer one supersedes the older one."""
    if m := _REF.search(text):
        return f"ref:{m.group(0)}"
    if topic == "contact_and_accessibility_needs" and _CONTACT_WINDOW.search(text):
        return "contact_window"
    if topic == "travel_and_life_events" and (m := _TRIP.search(text)):
        return f"trip:{m.group(1).lower()}"
    return None


def _dates(text: str) -> list[float]:
    out = []
    for y, mo, d in _DATE_ISO.findall(text):
        out.append(datetime(int(y), int(mo), int(d), tzinfo=timezone.utc).timestamp())
    for mon, d, y in _DATE_TEXT.findall(text):
        year = int(y) if y else clock.utcnow().year
        try:
            out.append(datetime(year, _MONTHS.index(mon.lower()) + 1, int(d), tzinfo=timezone.utc).timestamp())
        except ValueError:
            pass
    return out


def expiry_for(topic: str, text: str, created: float) -> float | None:
    pol = POLICIES.get(topic)
    if pol is None:
        return created + 90 * DAY
    if topic == "open_service_issues" and re.search(r"\b(resolved|closed|credit issued|refunded)\b", text, re.IGNORECASE):
        return created + RESOLVED_TTL_DAYS * DAY
    if topic == "travel_and_life_events":
        ds = [d for d in _dates(text) if d > created - 365 * DAY]
        if ds:
            return max(ds) + TRAVEL_GRACE_DAYS * DAY
    return None if pol.ttl_days is None else created + pol.ttl_days * DAY


@dataclass
class MemoryRecord:
    memory_id: str
    customer_token: str
    app_name: str
    topic: str
    text: str
    source_kind: str          # agent_tool | extraction | sor_seed | sor_event | crm_note
    source_channel: str
    classification: str
    created_at: float
    expires_at: float | None
    subject_key: str | None
    status: str = "ACTIVE"
    version: int = 1
    supersedes: str | None = None
    superseded_by: str | None = None
    status_changed_at: float | None = None
    redacted: list[str] = field(default_factory=list)

    def view(self) -> dict:
        d = asdict(self)
        d["created_at"] = clock.iso(self.created_at)
        d["expires_at"] = clock.iso(self.expires_at) if self.expires_at else "never (until changed)"
        d["status_changed_at"] = clock.iso(self.status_changed_at)
        d["retention_basis"] = POLICIES[self.topic].retention_basis if self.topic in POLICIES else "default 90 days"
        return d


class MemoryLedger:
    def __init__(self) -> None:
        self.records: dict[str, MemoryRecord] = {}
        self.legal_holds: set[str] = set()

    @staticmethod
    def new_id() -> str:
        return "m-" + uuid.uuid4().hex[:20]

    def add(self, rec: MemoryRecord) -> list[MemoryRecord]:
        """Store a record; returns the ACTIVE records it supersedes (same customer + subject)."""
        superseded = []
        if rec.subject_key:
            for old in self.active(rec.customer_token):
                if old.subject_key == rec.subject_key and old.app_name == rec.app_name:
                    old.status, old.superseded_by, old.status_changed_at = "SUPERSEDED", rec.memory_id, clock.now()
                    rec.version, rec.supersedes = old.version + 1, old.memory_id
                    superseded.append(old)
        self.records[rec.memory_id] = rec
        return superseded

    def get(self, memory_id: str | None) -> MemoryRecord | None:
        return self.records.get(memory_id) if memory_id else None

    def active(self, token: str) -> list[MemoryRecord]:
        return [r for r in self.records.values() if r.customer_token == token and r.status == "ACTIVE"]

    def all_for(self, token: str) -> list[MemoryRecord]:
        return sorted((r for r in self.records.values() if r.customer_token == token), key=lambda r: r.created_at)

    def due_for_expiry(self, now: float | None = None) -> list[MemoryRecord]:
        now = clock.now() if now is None else now
        return [r for r in self.records.values()
                if r.status == "ACTIVE" and r.expires_at is not None and r.expires_at <= now
                and r.customer_token not in self.legal_holds]

    def mark(self, rec: MemoryRecord, status: str) -> None:
        rec.status, rec.status_changed_at = status, clock.now()
        if status == "ERASED":
            rec.text = "[erased]"

    def lookup(self, token: str, memory_id: str | None, text: str) -> MemoryRecord | None:
        """Find the ledger record for a retrieved memory: by id, else by exact fact text
        (ADK's Vertex search results don't carry the memory id)."""
        rec = self.get(memory_id)
        if rec is not None:
            return rec
        matches = [r for r in self.records.values() if r.customer_token == token and r.text == text.strip()]
        return max(matches, key=lambda r: r.created_at) if matches else None

    @staticmethod
    def usable(rec: MemoryRecord | None, now: float | None = None) -> bool:
        """Read-time check (defence in depth against eventual consistency in the bank)."""
        if rec is None:
            return True  # generated by Memory Bank itself: governed by the engine TTL config
        now = clock.now() if now is None else now
        return rec.status == "ACTIVE" and (rec.expires_at is None or rec.expires_at > now)

    def is_usable(self, memory_id: str | None, now: float | None = None) -> bool:
        return self.usable(self.get(memory_id), now)

    def eviction_candidates(self, token: str) -> list[MemoryRecord]:
        return sorted(self.active(token), key=lambda r: (POLICIES.get(r.topic, POLICIES["key_conversation_details"]).priority, r.created_at))
