"""FastAPI endpoints in front of the OmnichannelGateway.

Run:  uvicorn cx_memory.api:app --reload --port 8080

  /channels/*          web chat, voice (STT transcript), email
  /identity/step-up/*  new phone / shared line: account number + one-time passcode
  /connect/*           contact-center flow: screen pop and after-contact wrap-up into CRM
  /sor/events          system-of-record updates (dispute resolved, ...)
  /customers/*         memory search, ledger, consent, legal hold, erasure, export (DSAR)
  /ops/*               cost, quotas, capacity plan, burst test, faults, outbox, lifecycle clock, audit, events
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import clock
from .channels import Channel
from .cost import ExtractionPolicy, capacity_plan
from .gateway import OmnichannelGateway
from .governance import Assurance
from .governed_memory import TurnContext, reset_turn, set_turn
from .identity import IdentityNotResolved
from .loadgen import run_burst

_gateway: OmnichannelGateway | None = None


def gateway() -> OmnichannelGateway:
    global _gateway
    if _gateway is None:
        model = None
        if os.environ.get("FAKE_MODEL"):  # offline UI rehearsal without GCP
            from tests.fake_llm import FakeLlm

            model = FakeLlm()
        _gateway = OmnichannelGateway(model=model)
    return _gateway


async def _drainer():
    while True:
        await asyncio.sleep(3)
        with contextlib.suppress(Exception):
            await gateway().memory_service.drain_outbox()


@contextlib.asynccontextmanager
async def lifespan(_app):
    # Local backend starts empty. Live (vertex) with SEED_ON_START=1: wipe and re-seed every synthetic customer
    # through this process, so the in-process lifecycle ledger knows each memory (supersede / expiry / erasure).
    await _seed(gateway())
    task = asyncio.create_task(_drainer())
    yield
    task.cancel()


app = FastAPI(title="Cross-channel customer memory PoC", version="0.2.0", lifespan=lifespan)


# ------------------------------------------------------------------ channels
class WebChatIn(BaseModel):
    account_id: str
    message: str
    session_id: str | None = None


class VoiceIn(BaseModel):
    caller_number: str
    transcript: str  # STT output from the telephony layer
    session_id: str | None = None


class EmailIn(BaseModel):
    sender: str
    subject: str = ""
    body: str
    session_id: str | None = None


async def _run(channel: Channel, identifier: str, text: str, session_id: str | None):
    try:
        r = await gateway().handle(channel, identifier, text, session_id)
    except IdentityNotResolved as e:
        raise HTTPException(status_code=404, detail=str(e))
    return r.__dict__


@app.post("/channels/web-chat")
async def web_chat(body: WebChatIn):
    return await _run(Channel.WEB_CHAT, body.account_id, body.message, body.session_id)


@app.post("/channels/voice")
async def voice(body: VoiceIn):
    return await _run(Channel.VOICE, body.caller_number, body.transcript, body.session_id)


@app.post("/channels/email")
async def email(body: EmailIn):
    text = f"Subject: {body.subject}\n\n{body.body}" if body.subject else body.body
    return await _run(Channel.EMAIL, body.sender, text, body.session_id)


class ConcurrentIn(BaseModel):
    web_chat: WebChatIn
    voice: VoiceIn
    email: EmailIn


@app.post("/demo/concurrent")
async def concurrent(body: ConcurrentIn):
    """Demo helper: serve all three channels at the same time from one memory."""
    results = await asyncio.gather(
        _run(Channel.WEB_CHAT, body.web_chat.account_id, body.web_chat.message, body.web_chat.session_id),
        _run(Channel.VOICE, body.voice.caller_number, body.voice.transcript, body.voice.session_id),
        _run(Channel.EMAIL, body.email.sender, body.email.body, body.email.session_id),
    )
    return {"web_chat": results[0], "voice": results[1], "email": results[2]}


@app.post("/sessions/{session_id}/end")
async def end_session(session_id: str):
    return await gateway().end_session(session_id)


# ------------------------------------------------------------------ identity step-up
class StepUpStartIn(BaseModel):
    channel: str = "voice"
    identifier: str
    account_number: str


class StepUpVerifyIn(BaseModel):
    challenge_id: str
    code: str


@app.post("/identity/step-up/start")
async def step_up_start(body: StepUpStartIn):
    return gateway().step_up_start(body.channel, body.identifier, body.account_number)


@app.post("/identity/step-up/verify")
async def step_up_verify(body: StepUpVerifyIn):
    return await gateway().step_up_verify(body.challenge_id, body.code)


# ------------------------------------------------------------------ contact center + CRM
@app.post("/connect/contact")
async def connect_contact(event: dict):
    """Contact-center flow event (Amazon Connect style): ContactId, Channel, CustomerEndpoint.Address, Attributes."""
    return await gateway().crm.inbound_contact(event)


class WrapUpIn(BaseModel):
    disposition: str
    notes: str
    agent_id: str = "agent-007"
    fulfilment: dict | None = None  # e.g. {"action": "FEE_WAIVER"} when the specialist completes the known intent


@app.post("/connect/contact/{contact_id}/wrap-up")
async def connect_wrap_up(contact_id: str, body: WrapUpIn):
    return await gateway().crm.wrap_up(contact_id, disposition=body.disposition, notes=body.notes, agent_id=body.agent_id,
                                      fulfilment=body.fulfilment)


@app.get("/crm/activities")
async def crm_activities():
    return {"activities": gateway().crm.activities[-20:]}


class SorEventIn(BaseModel):
    system: str = "Cards"
    ref_id: str
    status: str
    detail: str | None = None


@app.post("/sor/events")
async def sor_event(body: SorEventIn):
    return await gateway().publish_sor_event(body.system, body.ref_id, body.status, body.detail)


# ------------------------------------------------------------------ digital assistant + context handoff
class DigitalStartIn(BaseModel):
    account_id: str


class DigitalMsgIn(BaseModel):
    session_id: str
    text: str


class EscalateIn(BaseModel):
    session_id: str
    target: str = Field("voice", description="voice | live_chat | callback")


@app.post("/digital/session")
async def digital_start(body: DigitalStartIn):
    """Customer opens the in-app assistant (already authenticated by the app)."""
    return await gateway().digital.start(body.account_id)


@app.post("/digital/message")
async def digital_message(body: DigitalMsgIn):
    return await gateway().digital.message(body.session_id, body.text)


@app.post("/digital/escalate")
async def digital_escalate(body: EscalateIn):
    if body.target not in ("voice", "live_chat", "callback"):
        raise HTTPException(400, "target must be voice, live_chat or callback")
    return await gateway().digital.escalate(body.session_id, body.target)


@app.get("/context/{handoff_token}")
async def context_view(handoff_token: str):
    """Ops view of a stored context (the customer ID is never returned)."""
    h = gateway().context.handoffs.get(handoff_token)
    if h is None:
        raise HTTPException(404, "unknown handoff token")
    return h.view()


@app.post("/ops/servicing/reset")
async def servicing_reset():
    """Demo helper: restore the synthetic servicing data (fees, payments, queues) so journeys can be re-run."""
    from .servicing import ServicingSystems

    g = gateway()
    g.servicing = ServicingSystems()
    g.context.handoffs.clear()
    g.digital.sessions.clear()
    return {"status": "servicing data and context store restored"}


@app.get("/ops/digital-token/{session_id}")
async def digital_token(session_id: str):
    """Demo helper: the handoff token the app would pass along with click-to-dial or chat transfer."""
    s = gateway().digital.sessions.get(session_id)
    if s is None or not s.handoff_token:
        raise HTTPException(404, "no handoff for this session")
    return {"handoff_token": s.handoff_token}


@app.get("/customers/{customer_id}/insights")
async def insights(customer_id: str):
    sv = gateway().servicing
    return {"predicted_intent": sv.predicted_intent(customer_id), "next_best_action": sv.next_best_action(customer_id)}


# ------------------------------------------------------------------ customers: memory, lifecycle, privacy
def _scope(customer_id: str):
    try:
        return gateway().scope(customer_id)
    except KeyError:
        raise HTTPException(404, f"unknown customer {customer_id}")


@app.get("/customers/{customer_id}/memories")
async def memories(customer_id: str, q: str = "customer"):
    g = gateway()
    part, token = _scope(customer_id)
    ctx = TurnContext(customer_token=token, app_name=part.app_name, partition=part.name, channel="ops-console",
                      assurance=Assurance.SYSTEM, actor="ops-console", memory=g.memory_service)
    h = set_turn(ctx)
    try:
        res = await g.memory_service.search_memory(app_name=part.app_name, user_id=token, query=q)
    finally:
        reset_turn(h)
    return {"customer_id": customer_id, "customer_token": token, "partition": part.name,
            "memories": [" ".join(p.text or "" for p in m.content.parts) for m in res.memories],
            "used": ctx.trace["memories_used"], "degraded": ctx.trace["degraded"]}


@app.get("/customers/{customer_id}/ledger")
async def ledger(customer_id: str):
    g = gateway()
    part, token = _scope(customer_id)
    recs = [r.view() for r in g.memory_service.ledger.all_for(token)]
    return {"customer_id": customer_id, "customer_token": token, "partition": part.name, "records": recs,
            "legal_hold": token in g.memory_service.ledger.legal_holds,
            "consent": token not in g.memory_service.consent_withdrawn,
            "live_context": g.live.view(token)}


class ConsentIn(BaseModel):
    granted: bool


@app.post("/customers/{customer_id}/consent")
async def consent(customer_id: str, body: ConsentIn):
    g = gateway()
    part, token = _scope(customer_id)
    g.memory_service.set_consent(token, body.granted, actor="privacy-ops")
    out = {"consent": body.granted}
    if not body.granted:
        out["erasure"] = await g.memory_service.erase_customer(part.app_name, token, actor="privacy-ops",
                                                               reason="consent withdrawn")
    return out


class HoldIn(BaseModel):
    on: bool


@app.post("/customers/{customer_id}/legal-hold")
async def legal_hold(customer_id: str, body: HoldIn):
    g = gateway()
    _, token = _scope(customer_id)
    g.memory_service.set_legal_hold(token, body.on, actor="legal-ops")
    return {"legal_hold": body.on}


@app.post("/customers/{customer_id}/erase")
async def erase(customer_id: str):
    g = gateway()
    part, token = _scope(customer_id)
    return await g.memory_service.erase_customer(part.app_name, token, actor="privacy-ops", reason="right to erasure request")


@app.get("/customers/{customer_id}/export")
async def export(customer_id: str):
    """Data-subject access request: every memory record and every audit entry for this customer."""
    g = gateway()
    part, token = _scope(customer_id)
    g.vault.detokenize(token, purpose="DSAR export", actor="privacy-ops")
    return {"customer_id": customer_id, "customer_token": token, "partition": part.name,
            "memories": [r.view() for r in g.memory_service.ledger.all_for(token)],
            "audit": g.audit.for_customer(token)}


# ------------------------------------------------------------------ ops
@app.get("/ops/status")
async def ops_status():
    g = gateway()
    return {**g.memory_service.status(), "clock_offset_days": clock.offset_days(), "now": clock.iso(clock.now()),
            "backend": g.cfg.backend, "model": g.cfg.model}


class FaultIn(BaseModel):
    memory_bank: str = Field("ok", description="ok | slow | down | throttled")
    slow_ms: int = 1500


@app.post("/ops/faults")
async def faults(body: FaultIn):
    g = gateway()
    if body.memory_bank not in g.memory_service.faults.MODES:
        raise HTTPException(400, f"memory_bank must be one of {g.memory_service.faults.MODES}")
    g.memory_service.faults.memory_bank, g.memory_service.faults.slow_ms = body.memory_bank, body.slow_ms
    if body.memory_bank == "ok":
        g.memory_service.breaker.reset()
    return g.memory_service.faults.view()


@app.post("/ops/outbox/drain")
async def drain():
    return await gateway().memory_service.drain_outbox(force=True)


class PolicyIn(BaseModel):
    mode: str = "salient"
    batch_turns: int = 6


@app.post("/ops/extraction-policy")
async def extraction_policy(body: PolicyIn):
    if body.mode not in ExtractionPolicy.MODES:
        raise HTTPException(400, f"mode must be one of {ExtractionPolicy.MODES}")
    p = gateway().memory_service.policy
    p.mode, p.batch_turns = body.mode, body.batch_turns
    return {"mode": p.mode, "batch_turns": p.batch_turns}


class ClockIn(BaseModel):
    days: float = 0
    reset: bool = False


@app.post("/ops/clock/advance")
async def advance_clock(body: ClockIn):
    clock.reset() if body.reset else clock.advance(days=body.days)
    return {"offset_days": clock.offset_days(), "now": clock.iso(clock.now())}


@app.post("/ops/lifecycle/sweep")
async def sweep():
    return {"expired": await gateway().memory_service.sweep_expired()}


@app.get("/ops/capacity")
async def capacity(contacts_per_month: int = 2_000_000, turns_per_contact: float = 4, salient_share: float = 0.45,
                   mode: str = "salient", batch_turns: int = 6, avg_turn_tokens: int = 60, peak_factor: float = 3.0,
                   customers: int = 1_500_000, memories_per_customer: int = 25, partition: str = "consumer"):
    g = gateway()
    p = g.router.partitions.get(partition) or g.router.partitions["consumer"]
    return capacity_plan(contacts_per_month=contacts_per_month, turns_per_contact=turns_per_contact,
                         salient_share=salient_share, mode=mode, batch_turns=batch_turns, avg_turn_tokens=avg_turn_tokens,
                         peak_factor=peak_factor, extract_qpm_quota=p.extract_qpm, read_qpm_quota=p.read_qpm,
                         memories_per_customer=memories_per_customer, customers=customers)


class BurstIn(BaseModel):
    contacts: int = Field(200, le=2000)
    concurrency: int = Field(50, le=500)
    turns_per_contact: int = Field(3, le=10)
    mb_latency_ms: int = Field(80, le=2000)
    mode: str = "salient"


@app.post("/ops/burst")
async def burst(body: BurstIn):
    """Memory-layer burst test on a fresh, isolated in-process store (does not touch Memory Bank)."""
    return await run_burst(contacts=body.contacts, concurrency=body.concurrency, turns_per_contact=body.turns_per_contact,
                           mb_latency_ms=body.mb_latency_ms, mode=body.mode, app_base=gateway().cfg.app_name)


@app.get("/ops/client-use-cases")
async def client_use_cases():
    """The client journeys and their synthetic test data (eval/client_use_cases.json)."""
    from eval.run_client_use_cases import CASES

    return {"use_cases": CASES}


@app.post("/ops/client-use-cases/run")
async def run_client_use_cases():
    """Run every client journey on a fresh, isolated offline gateway and report each expected result."""
    from google.adk.sessions import InMemorySessionService

    from eval.run_client_use_cases import CASES, run_use_case
    from tests.fake_llm import FakeLlm

    from .config import Settings
    from .local_memory import LocalMemoryService
    from .profiles import _load

    offset = clock.offset_days()
    results = []
    for uc in CASES:
        clock.reset()
        _load.cache_clear()
        g = OmnichannelGateway(cfg=Settings(backend="local"), model=FakeLlm(),
                               session_service=InMemorySessionService(), memory_service=LocalMemoryService())
        await g.seed_all()
        results.append({"id": uc["id"], "title": uc["title"], "steps": await run_use_case(g, uc)})
    clock.reset()
    clock.advance(days=offset)
    _load.cache_clear()
    return {"results": results}


@app.get("/ops/audit")
async def audit(customer_id: str | None = None, n: int = 40):
    g = gateway()
    token = _scope(customer_id)[1] if customer_id else None
    return {"verify": g.audit.verify(), "entries": g.audit.tail(n, token)}


@app.get("/ops/events")
async def events(topic: str | None = None, n: int = 30):
    return {"events": gateway().bus.tail(topic, n)}


@app.get("/", include_in_schema=False)
async def business_view():
    """Business view: client journeys and trust stories, one step at a time, without technical detail."""
    return FileResponse(Path(__file__).parent / "static" / "present.html")


@app.get("/console", include_in_schema=False)
async def demo_page():
    """Technical console: three channels, CRM desktop, and governance / ops inspectors."""
    return FileResponse(Path(__file__).parent / "static" / "demo.html")


async def _seed(g: OmnichannelGateway) -> None:
    if not g.cfg.use_vertex:
        await g.seed_all()
    elif os.environ.get("SEED_ON_START") == "1":
        await g.reseed_vertex()


@app.post("/ops/demo/reset")
async def demo_reset():
    """Fresh demo state: new gateway (identity links, ledger, servicing, sessions), clock reset, customers re-seeded."""
    global _gateway
    _gateway = None
    clock.reset()
    g = gateway()
    await _seed(g)
    return {"status": "reset", "backend": g.cfg.backend}


@app.get("/healthz")
async def healthz():
    g = gateway()
    return {"ok": True, "backend": g.cfg.backend, "model": g.cfg.model, "partitions": [p.app_name for p in g.router.partitions.values()],
            "memory_breaker": g.memory_service.breaker.state}
