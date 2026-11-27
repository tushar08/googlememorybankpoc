"""OmnichannelGateway: identity -> assurance -> token + partition -> session -> agent -> guard -> events.

Per turn:
  1. resolve the channel identifier; unknown or shared identifiers get a step-up reply (no LLM, no memory)
  2. tokenize the customer ID and pick the partition (consumer / business) from the SOR segment
  3. create or reuse the session (scoped to the token), refresh the SOR profile and the live context
  4. run the channel agent with a TurnContext carrying assurance, consent and a trace
  5. apply the channel style guard, meter cost, publish a taxonomy-coded interaction event

Deterministic routing: the inbound channel alone selects the agent (no LLM router), so the path is
predictable, cheap and auditable.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from google.adk.runners import Runner
from google.genai import types

from . import clock
from .agents import build_all_agents
from .channels import Channel, enforce_voice, validate
from .config import Settings, settings as default_settings
from .cost import est_tokens
from .context_store import ContextStore
from .crm import CrmConnector
from .digital import DigitalAssistant
from .events import TOPIC_INTERACTIONS, TOPIC_SOR_CASE, EventBus, classify
from .governance import Assurance, AuditLog
from .governed_memory import GovernedMemoryService, TurnContext, reset_turn, set_turn
from .identity import IdentityResolver, NORMALIZERS, Resolution, StepUpService
from .live_context import LiveContextStore
from .local_memory import fact_entry
from .profiles import all_customer_ids, apply_sor_update, load_profile, owner_of_ref, profile_summary, seed_facts
from .quotas import Partition, PartitionRouter
from .servicing import ServicingSystems
from .services import build_services
from .tokenization import TokenVault

log = logging.getLogger(__name__)

STEP_UP_TEXT = {
    ("unknown", Channel.VOICE): "I don't recognise the number you're calling from. To keep your account safe, please key in "
                                "your account number and I'll text a passcode to the phone we have on file.",
    ("ambiguous", Channel.VOICE): "This number is shared by more than one customer. Please key in your account number so I "
                                  "know who I'm speaking with, and I'll text a passcode to your phone on file.",
    ("unknown", Channel.EMAIL): "Dear customer,\n\nWe could not match this email address to an account, so we cannot discuss "
                                "account details by email. Please sign in to online banking and use secure messages, or call us."
                                "\n\nCustomer Care",
    ("unknown", Channel.WEB_CHAT): "I couldn't match that account to your sign-in. Please sign in again to continue.",
}


@dataclass
class GatewayReply:
    customer_id: str | None
    channel: str
    session_id: str | None
    agent: str
    text: str
    style_violations: list[str] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    assurance: str = "NONE"
    step_up: dict | None = None
    trace: dict = field(default_factory=dict)


@dataclass
class SessionInfo:
    customer_id: str
    token: str
    partition: Partition
    channel: Channel
    identifier_key: str
    assurance: Assurance


class OmnichannelGateway:
    def __init__(self, cfg: Settings = default_settings, model=None, session_service=None, memory_service=None):
        self.cfg = cfg
        if session_service is None or memory_service is None:
            session_service, memory_service = build_services(cfg)
        self.session_service = session_service
        self.audit = AuditLog()
        self.bus = EventBus()
        self.router = PartitionRouter(cfg.app_name)
        self.vault = TokenVault(audit=self.audit)
        self.memory_service = GovernedMemoryService(memory_service, router=self.router, audit=self.audit, bus=self.bus)
        self.identity = IdentityResolver.from_source_systems()
        self.step_up = StepUpService(self.identity, audit=self.audit)
        self.live = LiveContextStore(cfg.live_context_turns)
        self.agents = build_all_agents(model)
        # One Runner per (partition, channel). Within a partition all channels share one memory scope.
        self.runners = {
            (p.app_name, ch): Runner(agent=agent, app_name=p.app_name,
                                     session_service=self.session_service, memory_service=self.memory_service)
            for p in self.router.partitions.values() for ch, agent in self.agents.items()
        }
        self.sessions: dict[str, SessionInfo] = {}
        self.servicing = ServicingSystems()
        self.context = ContextStore(audit=self.audit)
        self.crm = CrmConnector(self)
        self.digital = DigitalAssistant(self)
        self.bus.subscribe(TOPIC_SOR_CASE, self._on_sor_event)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def route(channel: Channel | str) -> Channel:
        return Channel(channel)  # raises ValueError for unknown channels

    def scope(self, customer_id: str) -> tuple[Partition, str]:
        part = self.router.for_segment(load_profile(customer_id)["segment"])
        return part, self.vault.tokenize(customer_id, part.name)

    @property
    def app_name(self) -> str:  # kept for scripts that predate partitions
        return self.router.partitions["consumer"].app_name

    async def seed_customer(self, customer_id: str) -> int:
        part, token = self.scope(customer_id)
        facts = seed_facts(load_profile(customer_id))
        for f, topic in facts:
            await self.memory_service.add_memory(app_name=part.app_name, user_id=token, memories=[fact_entry(f, topic)],
                                                 custom_metadata={"topic": topic, "source_kind": "sor_seed"})
        return len(facts)

    async def seed_all(self) -> None:
        for cid in all_customer_ids():
            await self.seed_customer(cid)

    async def reseed_vertex(self) -> None:
        """Delete each synthetic customer's memories in Memory Bank, then seed them again."""
        for cid in all_customer_ids():
            part, token = self.scope(cid)
            purged = await self.memory_service.inner.purge_scope(part.app_name, token)
            n = await self.seed_customer(cid)
            print(f"reseed {cid}: deleted {purged}, wrote {n}", flush=True)

    async def _create_session(self, info: SessionInfo):
        profile = load_profile(info.customer_id)
        s = await self.session_service.create_session(
            app_name=info.partition.app_name,
            user_id=info.token,
            state={
                "customer_token": info.token,
                "first_name": profile["first_name"],
                "channel": info.channel.value,
                "assurance": info.assurance.name,
                "customer_profile": profile_summary(profile),
                "live_context": self.live.render(info.token),
            },
        )
        self.sessions[s.id] = info
        return s

    def _step_up_reply(self, ch: Channel, res: Resolution) -> GatewayReply:
        text = STEP_UP_TEXT.get((res.status, ch)) or STEP_UP_TEXT[("unknown", ch)]
        self.audit.append("IDENTITY_UNRESOLVED", actor="gateway", details={"channel": ch.value, **res.view()})
        return GatewayReply(None, ch.value, None, "identity_guard", text, assurance=res.assurance.name,
                            step_up={"required": True, "reason": res.status, "next": "POST /identity/step-up/start"},
                            trace={"identity": res.view(), "memories_used": [], "writes": [], "extraction": [], "degraded": []})

    # ------------------------------------------------------------------ main entry
    async def handle(self, channel: Channel | str, identifier: str, text: str, session_id: str | None = None) -> GatewayReply:
        ch = self.route(channel)
        key = NORMALIZERS[ch](identifier)
        info = self.sessions.get(session_id) if session_id else None
        if info and (info.channel is not ch or info.identifier_key != key):
            info, session_id = None, None  # a session id is only valid for the identifier that opened it
        session = None
        if info:
            res = Resolution("resolved", info.customer_id, info.assurance, "existing_session", key[-4:], 1)
            session = await self.session_service.get_session(app_name=info.partition.app_name, user_id=info.token,
                                                             session_id=session_id)
        else:
            res = self.identity.resolve_contact(ch, identifier)
            if res.status != "resolved":
                return self._step_up_reply(ch, res)
        if session is None:
            part, token = self.scope(res.customer_id)
            info = SessionInfo(res.customer_id, token, part, ch, key, res.assurance)
            session = await self._create_session(info)

        part, token = info.partition, info.token
        profile = load_profile(info.customer_id)
        ctx = TurnContext(customer_token=token, app_name=part.app_name, partition=part.name, channel=ch.value,
                          assurance=info.assurance, actor=f"{ch.value}_agent", session_id=session.id,
                          consent=token not in self.memory_service.consent_withdrawn,
                          memory=self.memory_service, live=self.live)
        before = dict(self.memory_service.meter.by_partition[part.name])
        reply, tool_calls, model_calls, usage_in, usage_out = "", [], 0, 0, 0
        handle = set_turn(ctx)
        try:
            async for event in self.runners[(part.app_name, ch)].run_async(
                user_id=token,
                session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text=text)]),
                # SOR is read live every turn; other channels may have spoken since the last turn
                state_delta={"live_context": self.live.render(token), "channel": ch.value,
                             "customer_profile": profile_summary(profile)},
            ):
                if event.author != "user" and event.content:
                    model_calls += 1
                    um = getattr(event, "usage_metadata", None)
                    usage_in += (getattr(um, "prompt_token_count", 0) or 0) if um else 0
                    usage_out += (getattr(um, "candidates_token_count", 0) or 0) if um else 0
                for fc in event.get_function_calls() or []:
                    tool_calls.append(fc.name)
                if event.is_final_response() and event.content and event.content.parts:
                    reply = "".join(p.text or "" for p in event.content.parts).strip()
        finally:
            reset_turn(handle)

        violations = validate(ch, reply, profile["first_name"])
        if ch is Channel.VOICE and violations:
            log.info("voice guard applied: %s", violations)
            reply = enforce_voice(reply)

        # cost: real token counts when the model reports them, otherwise an estimate
        if not usage_in:
            mem_text = " ".join(m["text"] for m in ctx.trace["memories_used"])
            usage_in = model_calls * (600 + est_tokens(profile_summary(profile) + self.live.render(token) + text + mem_text))
            usage_out = est_tokens(reply) + 20 * max(0, model_calls - 1)
        meter = self.memory_service.meter
        meter.add(part.name, turns=1, model_calls=model_calls, model_input_tokens=usage_in, model_output_tokens=usage_out)
        after = meter.by_partition[part.name]
        delta = {k: after[k] - before.get(k, 0) for k in after if after[k] != before.get(k, 0)}

        taxonomy = classify(text)
        evt = await self.bus.publish(TOPIC_INTERACTIONS, token, {
            "event_id": str(uuid.uuid4()), "event_type": "CustomerInteraction", "schema_version": "1.0",
            "taxonomy_code": taxonomy, "channel": ch.value, "customer_token": token, "partition": part.name,
            "assurance": info.assurance.name, "session_id": session.id, "occurred_at": clock.iso(clock.now()),
            "memory_refs_read": [m["memory_id"] for m in ctx.trace["memories_used"]],
            "memory_refs_written": [w.get("memory_id") for w in ctx.trace["writes"] if w.get("memory_id")],
            "source": "cx-memory-gateway",  # no raw text on the bus: refs + taxonomy only
        })
        trace = {
            "identity": res.view(), "customer_token": token, "partition": part.name, "app_name": part.app_name,
            "taxonomy_code": taxonomy, "event_offset": f"{evt['topic']}[{evt['partition']}]@{evt['offset']}",
            "cost_delta": {**delta, "est_usd": meter.estimate({**{f: 0 for f in meter.FIELDS}, **delta})["total_usd"]},
            **ctx.trace,
        }
        return GatewayReply(info.customer_id, ch.value, session.id, self.agents[ch].name, reply, violations, tool_calls,
                            info.assurance.name, None, trace)

    # ------------------------------------------------------------------ step-up
    def step_up_start(self, channel: Channel | str, identifier: str, account_number: str) -> dict:
        return self.step_up.start(self.route(channel), identifier, account_number)

    async def step_up_verify(self, challenge_id: str, code: str) -> dict:
        out, ch = self.step_up.verify(challenge_id, code)
        if ch is None:
            return out
        part, token = self.scope(ch.customer_id)
        info = SessionInfo(ch.customer_id, token, part, ch.channel, NORMALIZERS[ch.channel](ch.identifier), Assurance.HIGH)
        s = await self._create_session(info)
        note = ("caller verified by one-time passcode on a NEW number ending "
                f"{info.identifier_key[-4:]} (now linked)" if out["new_identifier_linked"]
                else "caller verified by one-time passcode on a shared line")
        await self.live.note(token, ch.channel.value, note)
        return {**out, "session_id": s.id, "customer_id": ch.customer_id}

    async def end_session(self, session_id: str) -> dict:
        info = self.sessions.get(session_id)
        if not info:
            return {"status": "unknown session"}
        msg = await self.memory_service.flush_session(info.partition.app_name, info.token, session_id)
        return {"status": "ended", "extraction": msg}

    # ------------------------------------------------------------------ system-of-record events
    async def publish_sor_event(self, system: str, ref_id: str, status: str, detail: str | None = None) -> dict:
        cid = owner_of_ref(ref_id)
        if cid is None:
            return {"status": "unknown reference"}
        _, token = self.scope(cid)
        rec = await self.bus.publish(TOPIC_SOR_CASE, token, {"system": system, "ref_id": ref_id, "status": status,
                                                             "detail": detail, "customer_token": token})
        return {"status": "published", "offset": rec["offset"], "partition": rec["partition"]}

    async def _on_sor_event(self, rec: dict) -> None:
        """Consumer: the SOR is the source of truth; supersede the memory that now disagrees with it."""
        p = rec["payload"]
        cid = apply_sor_update(p["ref_id"], p["status"], p.get("detail"))
        if cid is None:
            return
        part, token = self.scope(cid)
        kind = "Card dispute" if p["ref_id"].startswith("DSP") else "Servicing case"
        text = f"{kind} {p['ref_id']} status changed to {p['status']}" + (f": {p['detail']}" if p.get("detail") else "") \
            + f" (as of {clock.utcnow():%Y-%m-%d}). [source: {p['system']} event]"
        await self.memory_service.write_fact(app_name=part.app_name, user_id=token, text=text, topic="open_service_issues",
                                             source_kind="sor_event")
        await self.live.note(token, "sor", f"{p['ref_id']} is now {p['status']}")
