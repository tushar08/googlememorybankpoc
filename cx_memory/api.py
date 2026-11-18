"""FastAPI channel endpoints in front of the OmnichannelGateway.

Run:  uvicorn cx_memory.api:app --reload --port 8080
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .channels import Channel
from .gateway import OmnichannelGateway
from .identity import IdentityNotResolved

app = FastAPI(title="Cross-channel customer memory PoC", version="0.1.0")
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


@app.on_event("startup")
async def _seed_local_memory():
    """BACKEND=local starts empty, so seed it from the source systems (Vertex is seeded by ingest_profiles.py)."""
    g = gateway()
    if not g.cfg.use_vertex:
        from .local_memory import fact_entry
        from .profiles import all_customer_ids, load_profile, profile_facts

        for cid in all_customer_ids():
            await g.memory_service.add_memory(app_name=g.app_name, user_id=cid,
                                              memories=[fact_entry(f) for f in profile_facts(load_profile(cid))])


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


@app.get("/customers/{customer_id}/memories")
async def memories(customer_id: str, q: str = "customer"):
    g = gateway()
    res = await g.memory_service.search_memory(app_name=g.app_name, user_id=customer_id, query=q)
    return {"customer_id": customer_id, "memories": [" ".join(p.text or "" for p in m.content.parts) for m in res.memories]}


@app.get("/", include_in_schema=False)
async def demo_page():
    """Side-by-side demo console: three channels + live Memory Bank search."""
    return FileResponse(Path(__file__).parent / "static" / "demo.html")


@app.get("/healthz")
async def healthz():
    g = gateway()
    return {"ok": True, "backend": g.cfg.backend, "model": g.cfg.model, "app_name": g.app_name}
