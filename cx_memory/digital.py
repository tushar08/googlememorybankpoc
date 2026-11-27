"""In-app digital assistant: a dialog manager in front of the LLM channel agent.

Known servicing intents are handled deterministically (auditable, no hallucinated amounts):
  predicted intent (recommendation engine) -> confirm -> fulfil (payment, fee waiver)
  fee inquiry (transaction search) / unrecognized charge triage
  escalation to a specialist (voice click-to-dial, live chat, or call back) with a context handoff token
  next-best action offer after the need is met
Anything else falls through to the web chat LLM agent with governed memory.

Every turn is written to Tier 1 (live context), published as a taxonomy-coded interaction event,
and durable outcomes (payment made, fee waived, escalation reason) become governed memories.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import clock
from .channels import Channel
from .events import TOPIC_INTERACTIONS
from .governance import Assurance
from .identity import normalize_account
from .profiles import load_profile
from .servicing import due_in_days

if TYPE_CHECKING:
    from .gateway import OmnichannelGateway

CLICK_TO_DIAL = "+1 800 555 0100"

_YES = re.compile(r"^\s*(yes|yeah|yep|sure|ok(ay)?|please( do)?|go ahead|confirm)\b", re.IGNORECASE)
_NO = re.compile(r"^\s*(no|nope|nah|that'?s all|nothing)\b", re.IGNORECASE)
_AGENT = re.compile(r"\b(agent|specialist|human|representative|someone|person)\b", re.IGNORECASE)
_CALLBACK = re.compile(r"\b(call me back|callback|call back|later)\b", re.IGNORECASE)
_CALL = re.compile(r"\b(call|phone|dial|speak)\b", re.IGNORECASE)
_CHAT = re.compile(r"\b(chat|stay in the app|message)\b", re.IGNORECASE)
_WAIVE = re.compile(r"\b(waive\w*|waiver|reverse the fee|refund the fee|remove the fee)\b", re.IGNORECASE)
_FEE = re.compile(r"\bfees?\b|\bcharged\b", re.IGNORECASE)
_UNRECOGNIZED = re.compile(r"\b(don'?t recogni[sz]e|unrecogni[sz]ed|not mine|didn'?t make|unknown charge)\b", re.IGNORECASE)
_PAY = re.compile(r"\b(pay|payment)\b", re.IGNORECASE)
_MONTHS = {m: i + 1 for i, m in enumerate("january february march april may june july august september october november december".split())}
_SHORT = {k[:3]: v for k, v in _MONTHS.items()}

REASON_LABEL = {"FEE_WAIVER": "fee waiver", "UNRECOGNIZED_CHARGE": "unrecognized charges", "PRODUCT_INTEREST": "a card offer",
                "MAKE_PAYMENT": "a payment", "FEE_INQUIRY": "fees"}
TAXONOMY = {"FEE_WAIVER": "SERVICING.FEE.WAIVER_REQUEST", "FEE_INQUIRY": "SERVICING.FEE.INQUIRY",
            "MAKE_PAYMENT": "PAYMENTS.CARD_PAYMENT.MAKE", "UNRECOGNIZED_CHARGE": "SERVICING.DISPUTE.UNRECOGNIZED_CHARGE",
            "PRODUCT_INTEREST": "SALES.CARD_OFFER.INTEREST", "ESCALATION": "SERVICING.ESCALATION.SPECIALIST",
            "WELCOME": "DIGITAL.SESSION.START"}


@dataclass
class DigitalSession:
    session_id: str
    customer_id: str
    customer_token: str
    app_name: str
    account_id: str
    state: str = "idle"
    slots: dict = field(default_factory=dict)
    done: list[str] = field(default_factory=list)       # what was already fulfilled in this conversation
    transcript: list[tuple[str, str]] = field(default_factory=list)
    handoff_token: str | None = None
    llm_session_id: str | None = None


def _month_from(text: str) -> str | None:
    for word in re.findall(r"[a-z]+", text.lower()):
        m = _MONTHS.get(word) or (_SHORT.get(word) if len(word) == 3 else None)
        if m:
            return f"{clock.utcnow().year}-{m:02d}"
    return None


class DigitalAssistant:
    def __init__(self, gw: "OmnichannelGateway") -> None:
        self.gw = gw
        self.sessions: dict[str, DigitalSession] = {}

    # ------------------------------------------------------------------ helpers
    def _out(self, s: DigitalSession, text: str, *, intent: str, extra: dict | None = None) -> dict:
        s.transcript.append(("assistant", text))
        return {"session_id": s.session_id, "customer_id": s.customer_id, "agent": "digital_assistant",
                "assurance": "HIGH", "text": text, "state": s.state, "intent": intent, **(extra or {})}

    async def _log(self, s: DigitalSession, customer_text: str, reply: str, intent: str) -> None:
        await self.gw.live.append(s.customer_token, "web_chat", customer_text, reply)
        await self.gw.bus.publish(TOPIC_INTERACTIONS, s.customer_token, {
            "event_type": "CustomerInteraction", "schema_version": "1.0", "taxonomy_code": TAXONOMY.get(intent, "GENERAL.INQUIRY.OTHER"),
            "channel": "web_chat", "customer_token": s.customer_token, "assurance": "HIGH", "session_id": s.session_id,
            "occurred_at": clock.iso(clock.now()), "source": "digital-assistant-dialog-manager"})

    async def _remember(self, s: DigitalSession, text: str, topic: str, source_kind: str = "agent_tool") -> dict:
        return await self.gw.memory_service.write_fact(app_name=s.app_name, user_id=s.customer_token, text=text,
                                                       topic=topic, source_kind=source_kind)

    # ------------------------------------------------------------------ session start
    async def start(self, account_id: str) -> dict:
        res = self.gw.identity.resolve_contact(Channel.WEB_CHAT, account_id)
        if res.status != "resolved":
            return {"error": "not signed in", "identity": res.view()}
        cid = res.customer_id
        part, token = self.gw.scope(cid)
        s = DigitalSession(str(uuid.uuid4()), cid, token, part.app_name, normalize_account(account_id))
        self.sessions[s.session_id] = s
        first = load_profile(cid)["first_name"]
        lines, extra = [f"Hi {first}!"], {"insight": None, "updates": []}

        # Conversation resume: show fulfilment done by a specialist since the customer left.
        for h in self.gw.context.pending_updates(token):
            f = h.fulfilment
            lines.append(f"Update on your {REASON_LABEL.get(h.reason_code, 'request')}: {f['message']}")
            extra["updates"].append({"reason_code": h.reason_code, **f})
            h.fulfilment_acknowledged = True
        if extra["updates"]:
            s.state = "anything_else"
            lines.append("Is there anything else I can help you with?")
        else:
            ins = self.gw.servicing.predicted_intent(cid)
            extra["insight"] = ins
            if ins and ins["predicted_intent"] == "MAKE_PAYMENT":
                st = self.gw.servicing.statement(cid)
                days = due_in_days(st["due_date"])
                lines.append(f"Your credit card payment of ${st['minimum_due_usd']:.2f} (minimum) is due in {days} days, on "
                             f"{st['due_date']}. Would you like to take care of it now?")
                s.state, s.slots["intent"] = "confirm_intent", "MAKE_PAYMENT"
            elif ins and ins["predicted_intent"] == "FEE_WAIVER":
                lines.append("Are you contacting us about a fee waiver?")
                s.state, s.slots["intent"] = "confirm_intent", "FEE_WAIVER"
            else:
                lines.append("How can I help you today?")
        text = " ".join(lines)
        await self._log(s, "[opened chat]", text, "WELCOME")
        self.gw.audit.append("DIGITAL_SESSION_START", actor="digital-assistant", customer_token=token,
                             details={"predicted_intent": (extra["insight"] or {}).get("predicted_intent"), "updates": len(extra["updates"])})
        return self._out(s, text, intent="WELCOME", extra=extra)

    # ------------------------------------------------------------------ turns
    async def message(self, session_id: str, text: str) -> dict:
        s = self.sessions.get(session_id)
        if s is None:
            return {"error": "unknown session"}
        s.transcript.append(("customer", text))
        out = await self._dispatch(s, text)
        if out.get("agent") == "digital_assistant":
            await self._log(s, text, out["text"], out.get("intent", "GENERAL"))
        return out

    async def _dispatch(self, s: DigitalSession, text: str) -> dict:
        sv, cid = self.gw.servicing, s.customer_id

        # --- state-driven answers first
        if s.state == "confirm_intent":
            if _YES.search(text):
                return await self._begin(s, s.slots["intent"])
            if _NO.search(text):
                s.state = "idle"
                return self._out(s, "No problem. How can I help you today?", intent="GENERAL")
        if s.state == "payment_confirm":
            if _YES.search(text):
                pay = sv.make_payment(cid, s.slots["amount"], s.slots["from"])
                s.state = "idle"
                s.done.append(f"paid ${pay['amount_usd']:.2f} to the card (ref {pay['ref']})")
                await self._remember(s, f"Customer paid ${pay['amount_usd']:.2f} toward the card ending 4417 on {pay['posted']} "
                                        f"in the app (ref {pay['ref']}).", "key_conversation_details")
                return self._out(s, f"Done. Your payment of ${pay['amount_usd']:.2f} is submitted (confirmation {pay['ref']}) "
                                    f"and will post today. Anything else?", intent="MAKE_PAYMENT", extra={"payment": pay})
            if _NO.search(text):
                s.state = "idle"
                return self._out(s, "Okay, I haven't made the payment. Anything else?", intent="MAKE_PAYMENT")
        if s.state == "which_account":
            hint = "savings" if "saving" in text.lower() else "card" if re.search(r"card|credit", text, re.I) else \
                "checking" if "checking" in text.lower() else None
            if hint:
                fees = [t for t in sv.search(cid, kind="fee", account_hint=hint) if not t["waived"]]
                if not fees:
                    return self._out(s, f"I don't see any fees on your {hint} account.", intent="FEE_WAIVER")
                return await self._waive(s, fees[0])
        if s.state == "anything_else" and _NO.search(text):
            return await self._offer_nba(s)
        if s.state == "nba_offer":
            if _YES.search(text):
                return await self._escalate_offer(s, "PRODUCT_INTEREST", f"Customer is interested in the pre-qualified "
                                                  f"{s.slots['offer']['offer']} after their {', '.join(s.done) or 'servicing need'}.",
                                                  preferred="live_chat")
            if _NO.search(text):
                s.state = "idle"
                return self._out(s, "No problem. Have a great day!", intent="GENERAL")
        if s.state == "escalation_offer":
            target = "callback" if _CALLBACK.search(text) else "voice" if _CALL.search(text) else \
                "live_chat" if (_CHAT.search(text) or _YES.search(text)) else None
            if target:
                return await self.escalate(s.session_id, target)

        # --- free-text intents
        if _UNRECOGNIZED.search(text):
            month = _month_from(text)
            rows = sv.search(cid, kind="purchase", statement=month) if month else sv.search(cid, kind="purchase")
            s.slots["charges"], s.slots["reason_code"] = [r["txn_id"] for r in rows], "UNRECOGNIZED_CHARGE"
            s.state = "idle"
            lines = "; ".join(f"{r['date']} {r['description']} ${r['amount_usd']:.2f}" for r in rows[:5])
            return self._out(s, f"I can help you with the unrecognized charges. On that statement I see: {lines}. "
                                f"Which one don't you recognize?", intent="UNRECOGNIZED_CHARGE", extra={"transactions": rows})
        if _AGENT.search(text) or (s.slots.get("reason_code") and _CALL.search(text)):
            reason = s.slots.get("reason_code") or "GENERAL"
            summary = self._summary(s, reason)
            pref = "voice" if _CALL.search(text) else "live_chat"
            return await self._escalate_offer(s, reason, summary, preferred=pref, ask=False)
        if _WAIVE.search(text):
            fees = [t for t in sv.search(cid, kind="fee") if not t["waived"]]
            target = self._match_fee(fees, text) or (fees[0] if len(fees) == 1 else None)
            if target:
                return await self._waive(s, target)
            s.state = "which_account"
            return self._out(s, "Sure. Which account was the fee on?", intent="FEE_WAIVER")
        if _FEE.search(text) and not _PAY.search(text):
            fees = sv.search(cid, kind="fee", account_hint="card" if "card" in text.lower() else None)
            s.slots["reason_code"] = "FEE_INQUIRY"
            if not fees:
                return self._out(s, "I don't see any fees in the last 90 days.", intent="FEE_INQUIRY")
            lines = "; ".join(f"{t['date']} {t['description']} ${t['amount_usd']:.2f}" + (" (waived)" if t["waived"] else "") for t in fees)
            return self._out(s, f"Here are the fees on your card: {lines}.", intent="FEE_INQUIRY", extra={"transactions": fees})
        if _PAY.search(text):
            return await self._begin(s, "MAKE_PAYMENT")

        # --- everything else: the LLM channel agent with governed memory
        r = await self.gw.handle(Channel.WEB_CHAT, s.account_id, text, s.llm_session_id)
        s.llm_session_id = r.session_id
        s.transcript.append(("assistant", r.text))
        return {**r.__dict__, "session_id": s.session_id, "state": s.state, "intent": "LLM_FALLBACK"}

    # ------------------------------------------------------------------ intents
    async def _begin(self, s: DigitalSession, intent: str) -> dict:
        if intent == "MAKE_PAYMENT":
            st = self.gw.servicing.statement(s.customer_id)
            s.state, s.slots["amount"], s.slots["from"] = "payment_confirm", st["minimum_due_usd"], st["pay_from"]
            return self._out(s, f"I can help with that. Pay ${st['minimum_due_usd']:.2f} from checking ending {st['pay_from'][-4:]} "
                                f"to your card today? (Statement balance is ${st['statement_balance_usd']:.2f}.)", intent=intent)
        if intent == "FEE_WAIVER":
            s.state = "which_account"
            return self._out(s, "I can help you with that. Which account?", intent=intent)
        s.state = "idle"
        return self._out(s, "How can I help you today?", intent="GENERAL")

    def _match_fee(self, fees: list[dict], text: str) -> dict | None:
        t = text.lower()
        for f in fees:
            month = int(f["date"][5:7])
            day = int(f["date"][8:10])
            month_names = [m for m, i in _MONTHS.items() if i == month]
            named_date = any(m in t or m[:3] in t for m in month_names) and re.search(rf"\b{day}\b", t)
            if f["date"] in t or named_date or ("late" in t and f.get("fee_code") == "LATE_FEE"):
                return f
        return None

    async def _waive(self, s: DigitalSession, fee: dict) -> dict:
        sv = self.gw.servicing
        e = sv.waiver_eligibility(s.customer_id, fee["txn_id"], "digital")
        s.slots["fee"] = fee
        if e["eligible"]:
            w = sv.waive_fee(s.customer_id, fee["txn_id"], "digital", approver="digital-assistant")
            s.done.append(f"waived the ${w['amount_usd']:.2f} {fee['description'].lower()} (ref {w['ref']})")
            s.state = "anything_else"
            await self._remember(s, f"A one-time courtesy waiver of the ${w['amount_usd']:.2f} {fee['description'].lower()} from "
                                    f"{fee['date']} was applied in the app (ref {w['ref']}).", "key_conversation_details")
            return self._out(s, f"I can waive the ${w['amount_usd']:.2f} {fee['description'].lower()} as a one-time courtesy for you. "
                                f"Done: it's reversed (ref {w['ref']}). Is there anything else I can help you with?",
                             intent="FEE_WAIVER", extra={"waiver": w})
        if e.get("needs_specialist"):
            s.slots["reason_code"] = "FEE_WAIVER"
            summary = (f"Customer asks to waive the ${fee['amount_usd']:.2f} {fee['description'].lower()} from {fee['date']} "
                       f"(txn {fee['txn_id']}); digital is not allowed to waive card fees."
                       + (f" Already done in this chat: {'; '.join(s.done)}." if s.done else ""))
            return await self._escalate_offer(s, "FEE_WAIVER", summary, preferred="voice",
                                              lead=f"I'm not able to waive the {fee['description'].lower()} here, but a specialist can.")
        s.state = "idle"
        return self._out(s, f"I'm sorry, I can't waive that fee: {e['reason']}.", intent="FEE_WAIVER", extra={"eligibility": e})

    async def _offer_nba(self, s: DigitalSession) -> dict:
        nba = self.gw.servicing.next_best_action(s.customer_id)
        if nba and nba["channel"] == "specialist":
            s.state, s.slots["offer"] = "nba_offer", nba
            return self._out(s, f"Before you go: you're {nba['eligibility']} for the {nba['offer']}. "
                                f"Would you like to speak to a specialist about it?", intent="PRODUCT_INTEREST", extra={"next_best_action": nba})
        s.state = "idle"
        return self._out(s, "Thanks for chatting with us. Have a great day!", intent="GENERAL")

    def _summary(self, s: DigitalSession, reason: str) -> str:
        if reason == "UNRECOGNIZED_CHARGE":
            ids = s.slots.get("charges", [])
            return f"Customer doesn't recognize a charge on their statement; candidate transactions {', '.join(ids)}. Card not yet locked."
        return f"Customer asked for a specialist about {REASON_LABEL.get(reason, 'their request')}."

    async def _escalate_offer(self, s: DigitalSession, reason: str, summary: str, *, preferred: str, lead: str = "",
                              ask: bool = True) -> dict:
        """Check skilled-agent availability, store the context, get a handoff token, offer the options."""
        sv = self.gw.servicing
        route = sv.route(reason, preferred)
        h = self.gw.context.create(customer_token=s.customer_token, customer_id=s.customer_id, source_channel="digital_assistant",
                                   target_channel=preferred, assurance=Assurance.HIGH, intent=reason, reason_code=reason,
                                   summary=summary,
                                   context={"done_in_digital": list(s.done), "fee": s.slots.get("fee"),
                                            "candidate_charges": s.slots.get("charges"), "offer": s.slots.get("offer"),
                                            "transcript_tail": s.transcript[-6:]})
        s.handoff_token, s.slots["reason_code"] = h.token, reason
        await self._remember(s, f"Customer was escalated from the app to a specialist for {REASON_LABEL.get(reason, reason.lower())}: {summary}",
                             "open_service_issues" if reason in ("FEE_WAIVER", "UNRECOGNIZED_CHARGE") else "key_conversation_details")
        wait = f"about {max(1, route['est_wait_s'] // 60)} min" if route["available"] else "nobody free right now"
        if not ask:
            return await self.escalate(s.session_id, preferred)
        s.state = "escalation_offer"
        options = "Call now, chat with them here, or ask for a call back?" if preferred == "voice" else \
            "Shall I connect you in this chat? You can also call or ask for a call back."
        team = route["queue"].replace("-", " ")
        text = (f"{lead} " if lead else "") + (f"A specialist from our {team} team is available ({wait}). {options}"
                                               if route["available"] else f"Our {team} team is busy right now. {options}")
        return self._out(s, text.strip(), intent="ESCALATION",
                         extra={"handoff": {"handoff_token": h.token, "reason_code": reason, "routing": route}})

    async def escalate(self, session_id: str, target: str) -> dict:
        s = self.sessions[session_id]
        h = self.gw.context.handoffs[s.handoff_token]
        h.target_channel = target
        route = self.gw.servicing.route(h.reason_code, "voice" if target == "callback" else target)
        s.state = "escalated"
        if target == "voice":
            text = f"Calling a specialist now. You won't need to verify again or repeat yourself: they'll see why you're calling."
            extra = {"click_to_dial": {"number": CLICK_TO_DIAL, "contact_attributes": {"handoff_token": h.token}}}
        elif target == "live_chat":
            text = "Connecting you to a specialist in this chat. They'll pick up right where we left off."
            extra = {"chat_transfer": {"queue": route["queue"], "contact_attributes": {"handoff_token": h.token}}}
        else:
            window = load_profile(s.customer_id)["preferences"].get("contact_window", "")
            cb = self.gw.servicing.schedule_callback(s.customer_id, h.reason_code, window)
            text = f"Done: a specialist will call you back ({cb['window']}), ref {cb['ref']}."
            extra = {"callback": cb, "contact_attributes": {"handoff_token": h.token}}
        out = self._out(s, text, intent="ESCALATION", extra={"routing": route, "handoff_token": h.token, **extra})
        await self._log(s, f"[chose {target}]", text, "ESCALATION")
        return out
