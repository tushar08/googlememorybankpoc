"""Governance and compliance controls applied to every memory read and write.

* Assurance levels: what the caller proved about who they are decides what memory they can use or change.
* Redaction: full card numbers (Luhn-checked), SSNs, full account numbers, phone numbers and email
  addresses are masked before anything reaches Memory Bank.
* Special-category data (health, religion, ...) is never stored, with one exception: accessibility
  needs under the contact_and_accessibility_needs topic, which servicing needs for accommodation.
* Contact-detail changes (new phone, email or address) need HIGH assurance because they are an
  account-takeover vector. Contact *preferences* (call me after 6pm) need only MEDIUM.
* Consent: a customer who withdraws memory consent gets session-only memory; nothing is written.
* Audit log: append-only and hash-chained, so any edit or deletion of an entry is detectable.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import IntEnum

from . import clock


class Assurance(IntEnum):
    NONE = 0      # unknown identifier: no memory, no profile
    MEDIUM = 2    # possession of a known identifier (caller number, email address): spoofable
    HIGH = 3      # authenticated online session, or step-up (one-time passcode) passed
    SYSTEM = 9    # internal writers: SOR events, seeding, operations


class Classification(IntEnum):
    INTERNAL = 1
    CONFIDENTIAL = 2
    RESTRICTED = 3   # accessibility / vulnerability data: kept to servicing use only


# --------------------------------------------------------------------------- redaction

_PAN_CANDIDATE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_ACCOUNT = re.compile(r"\bACC-?(\d{4})(\d{4})\b", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?(\d{4})(?!\d)")
_EMAIL = re.compile(r"\b[\w.+-]+@([\w-]+\.[\w.-]+)\b")


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for d in reversed(digits):
        n = int(d)
        if alt:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
        alt = not alt
    return total % 10 == 0


def redact(text: str) -> tuple[str, list[str]]:
    """Mask sensitive identifiers. Returns (redacted_text, list of what was masked)."""
    found: list[str] = []

    def pan(m: re.Match) -> str:
        raw = m.group(0)
        tail = raw[len(raw.rstrip(" -")):]  # keep the separator the match swallowed
        digits = re.sub(r"\D", "", raw)
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            found.append("card_number")
            return f"[card ending {digits[-4:]}]{tail}"
        found.append("long_number")
        return f"[REDACTED-NUMBER]{tail}"

    text = _PAN_CANDIDATE.sub(pan, text)
    if _SSN.search(text):
        found.append("ssn")
        text = _SSN.sub("[REDACTED-SSN]", text)
    if _ACCOUNT.search(text):
        found.append("account_number")
        text = _ACCOUNT.sub(lambda m: f"account ending {m.group(2)}", text)
    if _PHONE.search(text):
        found.append("phone_number")
        text = _PHONE.sub(lambda m: f"phone ending {m.group(1)}", text)
    if _EMAIL.search(text):
        found.append("email_address")
        text = _EMAIL.sub(lambda m: f"[email at {m.group(1)}]", text)
    return text, found


# --------------------------------------------------------------------------- special categories

SPECIAL_CATEGORIES = {
    "health": r"\b(diagnos\w*|cancer|chemo\w*|pregnan\w*|hospital\w*|surgery|therapy|depress\w*|anxiety|medication|illness|disease)\b",
    "religion": r"\b(church|mosque|synagogue|temple|religio\w*|ramadan)\b",
    "sexual_orientation": r"\b(gay|lesbian|bisexual|transgender)\b",
    "political_opinion": r"\b(democrat|republican|political part\w*)\b",
    "criminal_record": r"\b(arrest\w*|convict\w*|criminal record|jail|prison)\b",
}
ACCESSIBILITY = r"\b(hard of hearing|deaf|blind|visually impaired|wheelchair|dyslexi\w*|screen reader|speech impair\w*)\b"


def special_category(text: str) -> str | None:
    for cat, pattern in SPECIAL_CATEGORIES.items():
        if re.search(pattern, text, re.IGNORECASE):
            return cat
    return None


CONTACT_CHANGE = re.compile(
    r"\b(new|change|changed|update|updated)\b.{0,30}\b(phone|mobile|number|email|e-mail|address)\b"
    r"|\b(phone|mobile|number|email|address)\b.{0,20}\b(is now|changed to|has changed)\b",
    re.IGNORECASE,
)


@dataclass
class WriteDecision:
    allowed: bool
    text: str
    classification: Classification = Classification.CONFIDENTIAL
    reasons: list[str] = field(default_factory=list)
    redacted: list[str] = field(default_factory=list)
    needs_step_up: bool = False


def evaluate_write(text: str, topic: str, assurance: Assurance, consent: bool, min_assurance: Assurance) -> WriteDecision:
    """Policy decision for one memory write. Order matters: consent, assurance, content."""
    raw = (text or "").strip()
    if not consent:
        return WriteDecision(False, "", reasons=["customer has withdrawn memory consent: session-only"])
    if assurance < min_assurance and min_assurance is Assurance.SYSTEM:
        return WriteDecision(False, "", reasons=[f"topic {topic} is owned by the system of record; agents cannot write it"])
    if assurance < min_assurance:
        return WriteDecision(False, "", reasons=[f"topic {topic} needs {min_assurance.name} assurance, caller has {assurance.name}"],
                             needs_step_up=True)
    if CONTACT_CHANGE.search(raw) and assurance < Assurance.HIGH:
        return WriteDecision(False, "", needs_step_up=True,
                             reasons=["contact-detail change needs HIGH assurance (account-takeover protection); offer step-up"])
    clean, found = redact(raw)
    if len(clean) < 8:
        return WriteDecision(False, "", reasons=["fact too short"])
    cls = Classification.CONFIDENTIAL
    if re.search(ACCESSIBILITY, clean, re.IGNORECASE):
        if topic != "contact_and_accessibility_needs":
            return WriteDecision(False, "", reasons=["accessibility data may only be stored under contact_and_accessibility_needs"])
        cls = Classification.RESTRICTED
    cat = special_category(clean)
    if cat:
        return WriteDecision(False, "", reasons=[f"special-category data ({cat}) is never stored in memory"], redacted=found)
    return WriteDecision(True, clean, cls, redacted=found)


# --------------------------------------------------------------------------- audit log

GENESIS = "0" * 64


class AuditLog:
    """Append-only, hash-chained audit trail (in production: write to an immutable sink,
    e.g. BigQuery with a bucket lock / WORM storage, and ship to the SIEM)."""

    def __init__(self) -> None:
        self.entries: list[dict] = []

    def append(self, action: str, *, actor: str, customer_token: str | None = None, details: dict | None = None) -> dict:
        prev = self.entries[-1]["hash"] if self.entries else GENESIS
        body = {
            "seq": len(self.entries) + 1,
            "at": clock.iso(clock.now()),
            "action": action,
            "actor": actor,
            "customer_token": customer_token,
            "details": details or {},
            "prev_hash": prev,
        }
        body["hash"] = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
        self.entries.append(body)
        return body

    def verify(self) -> dict:
        prev = GENESIS
        for e in self.entries:
            body = {k: v for k, v in e.items() if k != "hash"}
            if body["prev_hash"] != prev:
                return {"ok": False, "broken_at": e["seq"], "why": "prev_hash mismatch"}
            if hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest() != e["hash"]:
                return {"ok": False, "broken_at": e["seq"], "why": "entry was modified"}
            prev = e["hash"]
        return {"ok": True, "entries": len(self.entries), "head": prev[:16]}

    def for_customer(self, token: str) -> list[dict]:
        return [e for e in self.entries if e["customer_token"] == token]

    def tail(self, n: int = 50, token: str | None = None) -> list[dict]:
        rows = self.for_customer(token) if token else self.entries
        return rows[-n:]
