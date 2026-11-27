"""Centralized context store for channel switching (digital assistant -> voice / live chat).

When the digital assistant escalates, it writes the latest context (auth state, intent, reason code,
what was already done, referenced transactions) and gets back a short-lived handoff token. The token
travels with the escalation (click-to-dial attribute, chat transfer), and the agent engagement platform
redeems it to:
  1. identify and authenticate the customer: a risk-based check decides whether digital authentication
     carries over to the new channel or a step-up is needed (channels have different risk levels);
  2. route by reason code to a skilled queue (proficiency routing);
  3. show the specialist why the customer is contacting, what was done, suggested actions and content.
After the contact, the platform updates the context with the fulfilment status, and the digital assistant
shows it when the customer comes back.

Tokens are single-use for authentication (replay is refused), expire after 15 minutes, and are bound to
one customer. The store holds the customer token, never the customer ID.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from . import clock
from .governance import Assurance, redact

HANDOFF_TTL_S = 15 * 60
INTENT_RISK = {"FEE_WAIVER": 10, "FEE_INQUIRY": 5, "MAKE_PAYMENT": 15, "PRODUCT_INTEREST": 5,
               "UNRECOGNIZED_CHARGE": 20, "CONTACT_CHANGE": 60}
CHANNEL_RISK = {"live_chat": 0, "voice": 15, "callback": 10}
AUTO_AUTH_MAX = 45
STEP_UP_MAX = 70


@dataclass
class Handoff:
    token: str
    customer_token: str
    customer_id: str                     # held by the gateway-side store only; never returned to callers
    source_channel: str
    target_channel: str
    auth: dict
    intent: str
    reason_code: str
    summary: str
    context: dict
    created_at: float
    redeemed_at: float | None = None
    redeemed_by: str | None = None
    fulfilment: dict | None = None
    fulfilment_acknowledged: bool = False
    history: list[dict] = field(default_factory=list)

    def view(self) -> dict:
        return {"handoff_token": self.token, "customer_token": self.customer_token, "source_channel": self.source_channel,
                "target_channel": self.target_channel, "auth": self.auth, "intent": self.intent,
                "reason_code": self.reason_code, "summary": self.summary, "context": self.context,
                "created_at": clock.iso(self.created_at), "redeemed_at": clock.iso(self.redeemed_at),
                "fulfilment": self.fulfilment, "history": self.history}


class ContextStore:
    def __init__(self, audit=None) -> None:
        self.handoffs: dict[str, Handoff] = {}
        self.audit = audit

    def create(self, *, customer_token: str, customer_id: str, source_channel: str, target_channel: str,
               assurance: Assurance, intent: str, reason_code: str, summary: str, context: dict) -> Handoff:
        h = Handoff(token="hct_" + secrets.token_urlsafe(18), customer_token=customer_token, customer_id=customer_id,
                    source_channel=source_channel, target_channel=target_channel,
                    auth={"assurance": assurance.name, "method": "authenticated_digital_session",
                          "authenticated_at": clock.iso(clock.now())},
                    intent=intent, reason_code=reason_code, summary=redact(summary)[0], context=context,
                    created_at=clock.now())
        h.history.append({"at": clock.iso(clock.now()), "event": "CONTEXT_STORED", "by": source_channel})
        self.handoffs[h.token] = h
        if self.audit:
            self.audit.append("HANDOFF_CREATED", actor=f"{source_channel}-assistant", customer_token=customer_token,
                              details={"reason_code": reason_code, "target": target_channel})
        return h

    def assess(self, h: Handoff, *, channel: str, identifier_matches: bool) -> dict:
        """Risk-based assessment when the customer moves channel. Returns the decision and its factors."""
        age = clock.now() - h.created_at
        factors = {"channel": CHANNEL_RISK.get(channel, 25), "intent": INTENT_RISK.get(h.reason_code, 20)}
        if not identifier_matches:
            factors["identifier_not_on_file"] = 35
        if age > HANDOFF_TTL_S * 2 / 3:
            factors["token_age"] = 15
        score = sum(factors.values())
        if score <= AUTO_AUTH_MAX:
            decision, assurance = "auto_authenticated", Assurance.HIGH
        elif score <= STEP_UP_MAX:
            decision, assurance = "step_up_required", Assurance.MEDIUM
        else:
            decision, assurance = "denied_route_to_fraud", Assurance.NONE
        return {"decision": decision, "assurance": assurance, "risk_score": score, "factors": factors,
                "token_age_s": int(age)}

    def redeem(self, token: str, *, channel: str, identifier_matches: bool, by: str) -> tuple[Handoff | None, dict]:
        h = self.handoffs.get(token)
        if h is None:
            return None, {"decision": "invalid_token"}
        if clock.now() - h.created_at > HANDOFF_TTL_S:
            return None, {"decision": "token_expired"}
        if h.redeemed_at is not None:
            if self.audit:
                self.audit.append("HANDOFF_REPLAY_REFUSED", actor=by, customer_token=h.customer_token, details={"channel": channel})
            return None, {"decision": "token_already_used"}
        risk = self.assess(h, channel=channel, identifier_matches=identifier_matches)
        h.redeemed_at, h.redeemed_by = clock.now(), by
        h.history.append({"at": clock.iso(clock.now()), "event": "CONTEXT_RETRIEVED", "by": by, "decision": risk["decision"]})
        if self.audit:
            self.audit.append("HANDOFF_REDEEMED", actor=by, customer_token=h.customer_token,
                              details={"channel": channel, "decision": risk["decision"], "risk_score": risk["risk_score"]})
        return h, risk

    def update_fulfilment(self, token: str, fulfilment: dict, by: str) -> Handoff | None:
        h = self.handoffs.get(token)
        if h is None:
            return None
        h.fulfilment = {**fulfilment, "updated_at": clock.iso(clock.now()), "by": by}
        h.fulfilment_acknowledged = False
        h.history.append({"at": clock.iso(clock.now()), "event": "FULFILMENT_UPDATED", "by": by, "status": fulfilment.get("status")})
        return h

    def pending_updates(self, customer_token: str) -> list[Handoff]:
        """Fulfilment updates the customer hasn't seen yet (shown in the next digital welcome)."""
        return [h for h in self.handoffs.values()
                if h.customer_token == customer_token and h.fulfilment and not h.fulfilment_acknowledged]

    def latest(self, customer_token: str) -> Handoff | None:
        rows = [h for h in self.handoffs.values() if h.customer_token == customer_token]
        return max(rows, key=lambda h: h.created_at) if rows else None
