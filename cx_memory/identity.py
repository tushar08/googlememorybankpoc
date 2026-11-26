"""Identity resolution with assurance levels, shared numbers and step-up verification.

Channel identifiers prove different things:
  web chat  : authenticated online-banking session (Account ID)  -> HIGH
  voice     : caller number (ANI) matches the CRM                 -> MEDIUM (ANI can be spoofed)
  email     : sender address matches the CRM                     -> MEDIUM (can be spoofed)
  unknown or shared identifier                                   -> NONE: no profile, no memory

The realistic cases this handles:
  * customer calls from a NEW phone: unknown number -> step-up (account number + one-time passcode sent
    to the phone on file) -> on success the call proceeds at HIGH and the new number is linked to the
    customer (flagged as a new device for 30 days);
  * household shared landline: one number, two customers -> ambiguous -> step-up picks the right one;
  * contact-detail changes need HIGH, so a spoofed caller number can't take over an account.

Matching is deterministic (exact match after normalizing), which keeps results reproducible and auditable.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import clock
from .channels import Channel
from .governance import Assurance

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "source_systems"
NEW_DEVICE_DAYS = 30
OTP_TTL_S = 300
OTP_MAX_ATTEMPTS = 3


class IdentityNotResolved(LookupError):
    pass


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:  # assume US
        digits = "1" + digits
    return "+" + digits if digits else ""


def normalize_email(raw: str) -> str:
    raw = (raw or "").strip().lower()
    m = re.search(r"<([^>]+)>", raw)  # "Maya Patel <maya@x.com>"
    return m.group(1).strip() if m else raw


def normalize_account(raw: str) -> str:
    return re.sub(r"\s|-", "", (raw or "")).upper()


NORMALIZERS = {
    Channel.WEB_CHAT: normalize_account,
    Channel.VOICE: normalize_phone,
    Channel.EMAIL: normalize_email,
}

BASE_ASSURANCE = {Channel.WEB_CHAT: Assurance.HIGH, Channel.VOICE: Assurance.MEDIUM, Channel.EMAIL: Assurance.MEDIUM}
METHOD = {Channel.WEB_CHAT: "authenticated_online_session", Channel.VOICE: "caller_number_match",
          Channel.EMAIL: "sender_address_match"}


def mask(channel: Channel, identifier: str) -> str:
    key = NORMALIZERS[channel](identifier)
    if channel is Channel.EMAIL:
        local, _, dom = key.partition("@")
        return f"{local[:1]}***@{dom}"
    return f"***{key[-4:]}" if key else "***"


@dataclass
class Resolution:
    status: str                       # resolved | ambiguous | unknown
    customer_id: str | None
    assurance: Assurance
    method: str
    identifier_masked: str
    candidates: int = 0
    notes: list[str] = field(default_factory=list)

    def view(self) -> dict:
        return {"status": self.status, "assurance": self.assurance.name, "method": self.method,
                "identifier": self.identifier_masked, "candidates": self.candidates, "notes": self.notes}


@dataclass
class LinkedIdentifier:
    customer_id: str
    linked_at: float
    method: str


@dataclass
class IdentityResolver:
    index: dict[tuple[Channel, str], set[str]]
    accounts: dict[str, str] = field(default_factory=dict)          # normalized account -> customer
    contacts: dict[str, dict] = field(default_factory=dict)         # customer -> phones/emails on file
    linked: dict[tuple[Channel, str], LinkedIdentifier] = field(default_factory=dict)

    @classmethod
    def from_source_systems(cls, data_dir: Path = DATA_DIR) -> "IdentityResolver":
        idx: dict[tuple[Channel, str], set[str]] = {}
        accounts, contacts = {}, {}
        crm = json.loads((data_dir / "crm.json").read_text())
        for c in crm["customers"]:
            cid = c["customer_id"]
            contacts[cid] = {"phones": [normalize_phone(p) for p in c.get("phones", [])],
                             "emails": [normalize_email(e) for e in c.get("emails", [])]}
            for acct in c.get("account_ids", []):
                idx.setdefault((Channel.WEB_CHAT, normalize_account(acct)), set()).add(cid)
                accounts[normalize_account(acct)] = cid
            for phone in c.get("phones", []):
                idx.setdefault((Channel.VOICE, normalize_phone(phone)), set()).add(cid)
            for email in c.get("emails", []):
                idx.setdefault((Channel.EMAIL, normalize_email(email)), set()).add(cid)
        idx.pop((Channel.VOICE, "+"), None)
        idx.pop((Channel.VOICE, ""), None)
        return cls(idx, accounts, contacts)

    def resolve_contact(self, channel: Channel, identifier: str) -> Resolution:
        key = NORMALIZERS[channel](identifier)
        masked = mask(channel, identifier)
        cids = self.index.get((channel, key), set())
        if len(cids) == 1:
            cid = next(iter(cids))
            return Resolution("resolved", cid, BASE_ASSURANCE[channel], METHOD[channel], masked, 1)
        if len(cids) > 1:
            return Resolution("ambiguous", None, Assurance.NONE, "shared_identifier", masked, len(cids),
                              [f"{len(cids)} customers share this {channel.value} identifier (household line): step-up needed"])
        link = self.linked.get((channel, key))
        if link:
            age_days = (clock.now() - link.linked_at) / 86400
            notes = [f"identifier linked {age_days:.0f} days ago via {link.method}"]
            if age_days < NEW_DEVICE_DAYS:
                notes.append(f"NEW DEVICE (< {NEW_DEVICE_DAYS} days): extra caution on money movement and contact changes")
            return Resolution("resolved", link.customer_id, Assurance.MEDIUM, "linked_identifier", masked, 1, notes)
        return Resolution("unknown", None, Assurance.NONE, "no_match", masked, 0,
                          ["identifier not on file: no profile or memory until the caller verifies"])

    def resolve(self, channel: Channel, identifier: str) -> str:
        r = self.resolve_contact(channel, identifier)
        if r.status != "resolved":
            raise IdentityNotResolved(f"No unique customer for {channel.value} identifier '{identifier}' ({r.status})")
        return r.customer_id

    def link(self, channel: Channel, identifier: str, customer_id: str, method: str) -> None:
        self.linked[(channel, NORMALIZERS[channel](identifier))] = LinkedIdentifier(customer_id, clock.now(), method)


@dataclass
class Challenge:
    challenge_id: str
    channel: Channel
    identifier: str
    customer_id: str
    code: str
    delivered_to: str
    created_at: float
    attempts: int = 0
    status: str = "pending"


class StepUpService:
    """Account number (keyed in on the IVR) + one-time passcode sent to the phone ON FILE.

    The passcode goes to a contact point already on file, never to the new number: that's what stops
    someone with a stolen account number from verifying on their own phone.
    """

    def __init__(self, resolver: IdentityResolver, audit=None) -> None:
        self.resolver = resolver
        self.audit = audit
        self.challenges: dict[str, Challenge] = {}
        self.demo_reveal = os.environ.get("DEMO_REVEAL_OTP", "1") == "1"

    def start(self, channel: Channel, identifier: str, account_number: str) -> dict:
        cid = self.resolver.accounts.get(normalize_account(account_number))
        res = self.resolver.resolve_contact(channel, identifier)
        if cid is None:
            return {"status": "failed", "reason": "account number not recognised"}
        if res.status == "ambiguous" and cid not in self.resolver.index.get((channel, NORMALIZERS[channel](identifier)), set()):
            return {"status": "failed", "reason": "account does not belong to anyone on this shared line"}
        phones = self.resolver.contacts.get(cid, {}).get("phones", [])
        if not phones:
            return {"status": "failed", "reason": "no phone on file for a passcode"}
        ch = Challenge(str(uuid.uuid4()), channel, identifier, cid, f"{secrets.randbelow(10**6):06d}",
                       f"SMS to phone on file ending {phones[0][-4:]}", clock.now())
        self.challenges[ch.challenge_id] = ch
        if self.audit:
            self.audit.append("STEP_UP_STARTED", actor="identity-service", customer_token=None,
                              details={"challenge_id": ch.challenge_id, "channel": channel.value,
                                       "from": mask(channel, identifier), "delivered_to": ch.delivered_to})
        out = {"status": "code_sent", "challenge_id": ch.challenge_id, "delivered_to": ch.delivered_to,
               "expires_in_s": OTP_TTL_S, "max_attempts": OTP_MAX_ATTEMPTS}
        if self.demo_reveal:
            out["demo_code"] = ch.code  # demo only: stands in for the SMS the customer would receive
        return out

    def verify(self, challenge_id: str, code: str) -> tuple[dict, Challenge | None]:
        ch = self.challenges.get(challenge_id)
        if ch is None or ch.status != "pending":
            return {"status": "failed", "reason": "no pending challenge"}, None
        if clock.now() - ch.created_at > OTP_TTL_S:
            ch.status = "expired"
            return {"status": "failed", "reason": "passcode expired"}, None
        ch.attempts += 1
        if not secrets.compare_digest(ch.code, (code or "").strip()):
            if ch.attempts >= OTP_MAX_ATTEMPTS:
                ch.status = "locked"
                if self.audit:
                    self.audit.append("STEP_UP_LOCKED", actor="identity-service", details={"challenge_id": challenge_id})
                return {"status": "locked", "reason": "too many wrong codes: transfer to fraud team"}, None
            return {"status": "failed", "reason": "wrong code", "attempts_left": OTP_MAX_ATTEMPTS - ch.attempts}, None
        ch.status = "verified"
        if self.resolver.resolve_contact(ch.channel, ch.identifier).status == "unknown":
            self.resolver.link(ch.channel, ch.identifier, ch.customer_id, "one-time passcode")
            linked = True
        else:
            linked = False
        if self.audit:
            self.audit.append("STEP_UP_VERIFIED", actor="identity-service",
                              details={"challenge_id": challenge_id, "new_identifier_linked": linked})
        return {"status": "verified", "assurance": Assurance.HIGH.name, "new_identifier_linked": linked}, ch
