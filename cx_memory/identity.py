"""Deterministic identity resolution: channel identifier -> customer_id.

Web chat -> Account ID, Voice -> Caller number (E.164), Email -> Sender address.
The mapping is built from the synthetic source-system extracts (no fuzzy matching:
the PoC is deliberately deterministic so results are reproducible and auditable).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .channels import Channel

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "source_systems"


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


@dataclass
class IdentityResolver:
    index: dict[tuple[Channel, str], str]

    @classmethod
    def from_source_systems(cls, data_dir: Path = DATA_DIR) -> "IdentityResolver":
        idx: dict[tuple[Channel, str], str] = {}
        crm = json.loads((data_dir / "crm.json").read_text())
        for c in crm["customers"]:
            cid = c["customer_id"]
            for acct in c.get("account_ids", []):
                cls._put(idx, Channel.WEB_CHAT, normalize_account(acct), cid)
            for phone in c.get("phones", []):
                cls._put(idx, Channel.VOICE, normalize_phone(phone), cid)
            for email in c.get("emails", []):
                cls._put(idx, Channel.EMAIL, normalize_email(email), cid)
        return cls(idx)

    @staticmethod
    def _put(idx, channel, key, cid):
        if key in ("", "+"):
            return
        existing = idx.get((channel, key))
        if existing and existing != cid:
            raise ValueError(f"Ambiguous identifier {channel.value}:{key} -> {existing} / {cid}")
        idx[(channel, key)] = cid

    def resolve(self, channel: Channel, identifier: str) -> str:
        key = NORMALIZERS[channel](identifier)
        try:
            return self.index[(channel, key)]
        except KeyError:
            raise IdentityNotResolved(f"No customer for {channel.value} identifier '{identifier}'") from None
