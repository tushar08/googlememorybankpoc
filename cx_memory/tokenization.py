"""Tokenized customer identity.

Memory Bank, Sessions, the event bus and the audit log never see the internal customer ID.
They see a customer token: a keyed HMAC of the ID. The token is
  * deterministic, so every channel lands in the same memory scope;
  * non-reversible without the key, so a leaked memory export cannot be joined back to a customer;
  * partition-scoped, so the same customer gets unrelated tokens in different partitions.

Only the gateway holds the vault and can detokenize, and each detokenize call is audited with a purpose.

Production: keep the HMAC key in Cloud KMS / an HSM (or use the enterprise tokenization service),
rotate it with a key version prefix (ctk1_, ctk2_) and re-key memory scopes during rotation.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os

log = logging.getLogger(__name__)

_DEV_KEY = "dev-only-not-a-secret"


class TokenVault:
    def __init__(self, key: str | None = None, key_version: int = 1, audit=None) -> None:
        key = key or os.environ.get("TOKEN_HMAC_KEY") or _DEV_KEY
        if key == _DEV_KEY:
            log.info("TokenVault is using the built-in development key; set TOKEN_HMAC_KEY outside the PoC")
        self._key = key.encode()
        self.key_version = key_version
        self._reverse: dict[str, str] = {}
        self.audit = audit

    def tokenize(self, customer_id: str, partition: str = "default") -> str:
        mac = hmac.new(self._key, f"{partition}|{customer_id}".encode(), hashlib.sha256).digest()
        token = f"ctk{self.key_version}_" + base64.b32encode(mac).decode().rstrip("=").lower()[:24]
        self._reverse[token] = customer_id
        return token

    def detokenize(self, token: str, *, purpose: str, actor: str) -> str:
        """Reverse lookup. Restricted: every call is audited with who asked and why."""
        cid = self._reverse.get(token)
        if self.audit is not None:
            self.audit.append("DETOKENIZE", actor=actor, customer_token=token,
                              details={"purpose": purpose, "found": cid is not None})
        if cid is None:
            raise KeyError("unknown token")
        return cid
