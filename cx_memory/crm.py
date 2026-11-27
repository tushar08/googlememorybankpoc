"""Contact center + CRM integration.

How memory reaches the HUMAN agent, not just the bots:

  1. Contact arrives (contact-center flow, e.g. Amazon Connect: ContactId, CustomerEndpoint, Attributes).
     The flow calls POST /connect/contact (a Lambda in a Connect flow would do the same call).
  2. The connector resolves identity, reads governed memory, and returns
       * contact attributes for the flow: token, assurance, queue, priority. Small, no PII;
       * a CRM screen pop for the agent desktop: open issues from the SOR, memory highlights with
         provenance, recent activity on other channels, and next-best actions.
  3. After-contact work: POST /connect/contact/{id}/wrap-up writes the CRM activity (disposition,
     taxonomy code, redacted notes), turns the agent's notes into a governed memory, and publishes the
     interaction event. CRM stays the system of record for the case; memory keeps the context.
  4. CRM or SOR case updates come back as events (POST /sor/events) and supersede stale memories.
"""
from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING

from . import clock
from .channels import Channel
from .events import TOPIC_CRM, TOPIC_INTERACTIONS, classify
from .governance import Assurance, redact
from .identity import normalize_phone
from .governed_memory import TurnContext, reset_turn, set_turn
from .profiles import load_profile

if TYPE_CHECKING:
    from .gateway import OmnichannelGateway

CONNECT_CHANNEL = {"VOICE": Channel.VOICE, "CHAT": Channel.WEB_CHAT, "EMAIL": Channel.EMAIL, "TASK": Channel.EMAIL}


def route_queue(profile: dict, intent: str, accessibility: bool) -> str:
    if accessibility:
        return "Accessibility-Trained-Agents"
    has_dispute = any(c.get("open_dispute") and "resolved" not in c["open_dispute"]["status"].lower() for c in profile["cards"])
    if has_dispute and re.search(r"dispute|charge", intent or "", re.IGNORECASE):
        return "Card-Disputes"
    if profile["segment"] == "Private Client":
        return "Private-Client-Desk"
    if profile["segment"] == "Small Business":
        return "Business-Banking"
    return "General-Servicing"


def next_best_actions(profile: dict, memories: list[dict], identity_notes: list[str]) -> list[str]:
    nba = []
    for c in profile["cards"]:
        d = c.get("open_dispute")
        if d:
            nba.append(f"Give the status of dispute {d['id']} ({d['status']}); the customer has already explained it")
        if c.get("replacement"):
            nba.append(f"Replacement card: {c['replacement']}")
    for k in profile["cases"]:
        if k["status"].lower() not in ("closed", "resolved"):
            nba.append(f"Open case {k['case_id']} ({k['topic']}): {k['status']}")
    texts = " ".join(m["text"] for m in memories)
    if trip := re.search(r"\b(?:to|in)\s+(Lisbon|[A-Z][a-z]{3,})\b.*?(\w+ \d{1,2}(?:, 20\d\d)?|20\d\d-\d\d-\d\d)", texts):
        nba.append(f"Upcoming travel to {trip.group(1)} ({trip.group(2)}): confirm the travel notice is active")
    pref = profile["preferences"]
    if pref.get("accessibility"):
        nba.append(f"Accessibility: {pref['accessibility']}")
    if pref.get("contact_window"):
        nba.append(f"Respect the contact window for call-backs: {pref['contact_window']}")
    if any("NEW DEVICE" in n for n in identity_notes):
        nba.append("Recently linked number: no contact-detail changes or payments without step-up")
    return nba


def suggested_actions(gw, customer_id: str, h) -> list[str]:
    """Predicted suggested actions for the specialist, from the reason code and the SOR."""
    sv, out = gw.servicing, []
    if h.reason_code == "FEE_WAIVER" and (fee := h.context.get("fee")):
        e = sv.waiver_eligibility(customer_id, fee["txn_id"], "specialist")
        out.append(f"Approve a one-time courtesy waiver of the ${fee['amount_usd']:.2f} {fee['description'].lower()} "
                   f"({fee['txn_id']}, {fee['date']}): " + ("eligible, no waiver in the last 12 months" if e["eligible"] else e["reason"]))
        out.append("Confirm the next payment date so the late fee doesn't repeat; offer AutoPay")
    elif h.reason_code == "UNRECOGNIZED_CHARGE":
        for tid in h.context.get("candidate_charges") or []:
            t = sv.txn(tid)
            if t and "QXT*" in t["description"]:
                out.append(f"Check descriptor first: {t['description']} ${t['amount_usd']:.2f} is a digital-services aggregator (KB-2211)")
        out.append("If still unrecognized: lock the card, open a dispute, order a replacement")
    elif h.reason_code == "PRODUCT_INTEREST" and (offer := h.context.get("offer")):
        out.append(f"Customer is {offer['eligibility']} for the {offer['offer']} ({offer['offer_id']}): walk through fees and benefits")
    for d in h.context.get("done_in_digital") or []:
        out.append(f"Already done in the app, don't repeat: {d}")
    return out


def agent_greeting(first_name: str, h) -> str:
    if h.reason_code == "FEE_WAIVER" and (fee := h.context.get("fee")):
        return f"Hi {first_name}, I can help with waiving the ${fee['amount_usd']:.2f} {fee['description'].lower()} from {fee['date']}."
    if h.reason_code == "UNRECOGNIZED_CHARGE":
        return f"Hi {first_name}, I can look at the unrecognized charges for you."
    if h.reason_code == "PRODUCT_INTEREST" and (offer := h.context.get("offer")):
        return f"Hi {first_name}, I understand you'd like to hear about the {offer['offer']}."
    return f"Hi {first_name}, I can see what you were doing in the app. How can I help?"


class CrmConnector:
    def __init__(self, gw: "OmnichannelGateway") -> None:
        self.gw = gw
        self.contacts: dict[str, dict] = {}
        self.activities: list[dict] = []

    async def inbound_contact(self, event: dict) -> dict:
        contact_id = event.get("ContactId") or str(uuid.uuid4())
        ch = CONNECT_CHANNEL.get(str(event.get("Channel", "VOICE")).upper(), Channel.VOICE)
        address = (event.get("CustomerEndpoint") or {}).get("Address", "")
        attrs = event.get("Attributes") or {}
        intent = attrs.get("intent", "")
        if attrs.get("handoff_token"):
            return await self._inbound_with_handoff(contact_id, ch, address, attrs["handoff_token"])
        res = self.gw.identity.resolve_contact(ch, address)
        if res.status != "resolved":
            self.gw.audit.append("CRM_CONTACT_UNVERIFIED", actor="contact-center", details={"contact_id": contact_id, **res.view()})
            return {"contact_id": contact_id,
                    "contact_attributes": {"verified": "false", "assurance": res.assurance.name,
                                           "route_queue": "Identity-Verification", "reason": res.status},
                    "screen_pop": None, "identity": res.view()}

        cid = res.customer_id
        part, token = self.gw.scope(cid)
        profile = load_profile(cid)
        ctx = TurnContext(customer_token=token, app_name=part.app_name, partition=part.name, channel=ch.value,
                          assurance=res.assurance, actor="crm-screen-pop", memory=self.gw.memory_service, live=self.gw.live)
        h = set_turn(ctx)
        try:
            await self.gw.memory_service.search_memory(app_name=part.app_name, user_id=token,
                                                       query=intent or "open dispute travel trip contact preference issue")
        finally:
            reset_turn(h)
        highlights = ctx.trace["memories_used"][:6]
        accessibility = bool(profile["preferences"].get("accessibility"))
        queue = route_queue(profile, intent, accessibility)
        open_issues = [{"ref": c["open_dispute"]["id"], "type": "Card dispute", "status": c["open_dispute"]["status"],
                        "detail": f"${c['open_dispute']['amount_usd']:.2f} at {c['open_dispute']['merchant']}"}
                       for c in profile["cards"] if c.get("open_dispute")]
        open_issues += [{"ref": k["case_id"], "type": k["topic"], "status": k["status"], "detail": k["detail"]}
                        for k in profile["cases"] if k["status"].lower() not in ("closed",)]
        screen_pop = {
            "crm_record_id": f"CRM-{cid}",   # CRM is a system of record: it may hold the real customer ID
            "customer": f"{profile['first_name']} {profile['last_name']}", "segment": profile["segment"],
            "customer_since": profile["customer_since"], "identity": res.view(),
            "open_issues": open_issues, "memory_highlights": highlights,
            "recent_activity_other_channels": self.gw.live.view(token)[-4:],
            "next_best_actions": next_best_actions(profile, highlights, res.notes),
            "degraded": ctx.trace["degraded"],
        }
        self.contacts[contact_id] = {"customer_id": cid, "token": token, "partition": part, "channel": ch,
                                     "assurance": res.assurance, "queue": queue, "started_at": clock.now()}
        self.gw.audit.append("CRM_SCREEN_POP", actor="contact-center", customer_token=token,
                             details={"contact_id": contact_id, "queue": queue,
                                      "memories_shown": [m["memory_id"] for m in highlights]})
        return {
            "contact_id": contact_id,
            "contact_attributes": {  # returned to the contact flow: keep it small and free of PII
                "verified": "true", "customer_token": token, "assurance": res.assurance.name, "route_queue": queue,
                "priority": "high" if profile["segment"] == "Private Client" or open_issues else "normal",
                "greeting_first_name": profile["first_name"],
            },
            "screen_pop": screen_pop,
        }

    async def _inbound_with_handoff(self, contact_id: str, ch: Channel, address: str, token: str) -> dict:
        """Channel switch from the digital assistant: redeem the context token, assess risk, route by reason code."""
        gw = self.gw
        peek = gw.context.handoffs.get(token)
        on_file = False
        if peek is not None:
            if ch is Channel.VOICE:
                on_file = normalize_phone(address) in gw.identity.contacts.get(peek.customer_id, {}).get("phones", []) or \
                    gw.identity.resolve_contact(ch, address).customer_id == peek.customer_id
            else:
                on_file = True  # live chat transfer inside the same authenticated app session
        h, risk = gw.context.redeem(token, channel="voice" if ch is Channel.VOICE else "live_chat",
                                    identifier_matches=on_file, by="agent-engagement-platform")
        if h is None or risk["decision"] != "auto_authenticated":
            gw.audit.append("CRM_HANDOFF_REJECTED", actor="contact-center", details={"contact_id": contact_id, **{k: v for k, v in (risk or {}).items() if k != "assurance"}})
            return {"contact_id": contact_id,
                    "contact_attributes": {"verified": "false", "route_queue": "Identity-Verification",
                                           "reason": risk["decision"] if risk else "invalid_token"},
                    "screen_pop": None, "risk": {k: (v.name if k == "assurance" else v) for k, v in (risk or {}).items()}}
        cid = h.customer_id
        part, ctoken = gw.scope(cid)
        profile = load_profile(cid)
        route = gw.servicing.route(h.reason_code, "voice" if ch is Channel.VOICE else "live_chat")
        ctx = TurnContext(customer_token=ctoken, app_name=part.app_name, partition=part.name, channel=ch.value,
                          assurance=Assurance.HIGH, actor="crm-screen-pop", memory=gw.memory_service, live=gw.live)
        hnd = set_turn(ctx)
        try:
            await gw.memory_service.search_memory(app_name=part.app_name, user_id=ctoken,
                                                  query=f"{h.reason_code.replace('_', ' ').lower()} {h.summary}")
        finally:
            reset_turn(hnd)
        suggested = suggested_actions(gw, cid, h)
        greeting = agent_greeting(profile["first_name"], h)
        screen_pop = {
            "crm_record_id": f"CRM-{cid}", "customer": f"{profile['first_name']} {profile['last_name']}",
            "segment": profile["segment"], "customer_since": profile["customer_since"],
            "identity": {"status": "resolved", "assurance": "HIGH", "method": "digital_handoff_token",
                         "notes": [f"authentication carried over from the app (risk score {risk['risk_score']}): do not re-verify"]},
            "why_calling": {"reason_code": h.reason_code, "summary": h.summary, "from": h.source_channel,
                            "done_in_digital": h.context.get("done_in_digital", []), "stored_at": clock.iso(h.created_at)},
            "suggested_actions": suggested, "knowledge": gw.servicing.knowledge(h.reason_code),
            "agent_greeting": greeting, "routing": route,
            "open_issues": [], "memory_highlights": ctx.trace["memories_used"][:5],
            "recent_activity_other_channels": gw.live.view(ctoken)[-4:],
            "next_best_actions": suggested, "degraded": ctx.trace["degraded"],
        }
        self.contacts[contact_id] = {"customer_id": cid, "token": ctoken, "partition": part, "channel": ch,
                                     "assurance": Assurance.HIGH, "queue": route["queue"], "started_at": clock.now(),
                                     "handoff_token": token, "reason_code": h.reason_code}
        await gw.live.note(ctoken, ch.value, f"customer moved from the app to {ch.value} ({h.reason_code}); context passed, no re-authentication")
        gw.audit.append("CRM_SCREEN_POP", actor="contact-center", customer_token=ctoken,
                        details={"contact_id": contact_id, "queue": route["queue"], "handoff": True, "reason_code": h.reason_code})
        return {
            "contact_id": contact_id,
            "contact_attributes": {"verified": "true", "auth": "carried_over", "customer_token": ctoken, "assurance": "HIGH",
                                   "route_queue": route["queue"], "skill": h.reason_code, "priority": "high",
                                   "greeting_first_name": profile["first_name"]},
            "risk": {**{k: v for k, v in risk.items() if k != "assurance"}, "assurance": risk["assurance"].name},
            "screen_pop": screen_pop,
        }

    async def wrap_up(self, contact_id: str, *, disposition: str, notes: str, agent_id: str,
                      fulfilment: dict | None = None) -> dict:
        c = self.contacts.get(contact_id)
        if c is None:
            return {"status": "unknown contact"}
        part, token = c["partition"], c["token"]
        # A human agent who handled the contact has verified the caller in the conversation.
        ctx = TurnContext(customer_token=token, app_name=part.app_name, partition=part.name, channel=c["channel"].value,
                          assurance=max(c["assurance"], Assurance.MEDIUM), actor=f"crm-agent:{agent_id}",
                          memory=self.gw.memory_service, live=self.gw.live)
        h = set_turn(ctx)
        try:
            mem = await self.gw.memory_service.write_fact(
                app_name=part.app_name, user_id=token, text=f"Agent note ({disposition}): {notes}",
                topic="key_conversation_details", source_kind="crm_note", ctx=ctx)
        finally:
            reset_turn(h)
        activity = {
            "activity_id": f"ACT-{uuid.uuid4().hex[:8].upper()}", "crm_record_id": f"CRM-{c['customer_id']}",
            "contact_id": contact_id, "channel": c["channel"].value, "queue": c["queue"], "agent_id": agent_id,
            "disposition": disposition, "taxonomy_code": classify(notes), "notes": redact(notes)[0],
            "memory_id": mem.get("memory_id"), "memory_status": mem["status"],
            "handle_time_s": int(clock.now() - c["started_at"]), "logged_at": clock.iso(clock.now()),
        }
        if fulfilment and c.get("handoff_token"):
            activity["fulfilment"] = await self._fulfil(c, fulfilment, agent_id)
        self.activities.append(activity)
        await self.gw.bus.publish(TOPIC_CRM, token, {k: v for k, v in activity.items() if k != "notes"})
        await self.gw.bus.publish(TOPIC_INTERACTIONS, token, {
            "event_type": "CustomerInteraction", "schema_version": "1.0", "taxonomy_code": activity["taxonomy_code"],
            "channel": c["channel"].value, "customer_token": token, "partition": part.name, "assurance": ctx.assurance.name,
            "occurred_at": activity["logged_at"], "source": "crm-after-contact-work",
            "memory_refs_written": [mem.get("memory_id")] if mem.get("memory_id") else []})
        await self.gw.live.note(token, c["channel"].value, f"human agent wrap-up: {disposition}")
        self.gw.audit.append("CRM_ACTIVITY_LOGGED", actor=f"crm-agent:{agent_id}", customer_token=token,
                             details={"activity_id": activity["activity_id"], "memory_id": mem.get("memory_id")})
        return {"crm_activity": activity, "memory": mem}

    async def _fulfil(self, c: dict, fulfilment: dict, agent_id: str) -> dict:
        """Specialist completes the known intent; the context store is updated for the customer's return to the app."""
        gw, cid = self.gw, c["customer_id"]
        h = gw.context.handoffs[c["handoff_token"]]
        result: dict = {"action": fulfilment.get("action")}
        if fulfilment.get("action") == "FEE_WAIVER" and (fee := h.context.get("fee")):
            w = gw.servicing.waive_fee(cid, fee["txn_id"], "specialist", approver=agent_id)
            ok = w["status"] == "waived"
            msg = (f"your ${fee['amount_usd']:.2f} {fee['description'].lower()} was waived by a specialist (ref {w['ref']}). "
                   f"It will show as a credit on your next statement." if ok else f"we couldn't waive the fee: {w.get('reason')}.")
            result.update(status="completed" if ok else "declined", ref=w.get("ref"), message=msg)
        else:
            result.update(status=fulfilment.get("status", "completed"), message=fulfilment.get("message", "your request was completed by a specialist."))
        gw.context.update_fulfilment(c["handoff_token"], result, by=f"crm-agent:{agent_id}")
        await gw.memory_service.write_fact(app_name=c["partition"].app_name, user_id=c["token"],
                                           text=f"Specialist outcome for {h.reason_code.replace('_', ' ').lower()}: {result['message']}",
                                           topic="open_service_issues", source_kind="crm_note")
        return result
