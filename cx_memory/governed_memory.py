"""GovernedMemoryService: one choke point in front of Memory Bank.

It implements ADK's BaseMemoryService, so agents, PreloadMemoryTool and callbacks use it unchanged.
Every read and write passes through, in order:

  read : partition read quota -> circuit breaker -> fault/timeout -> inner search
         -> lifecycle filter (superseded / expired / erased) -> audit -> trace (memories used)
  write: consent -> assurance -> redaction / special-category -> per-customer daily quota
         -> ledger (TTL, supersede) -> partition write quota -> inner write | outbox -> audit
  extract: extraction policy (per_turn / salient / batched) -> redact events -> extract quota
         -> inner extraction | outbox -> cost meter

Per-turn context (who, which channel, which assurance level) travels in a contextvar set by the gateway.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from google.adk.memory.base_memory_service import BaseMemoryService, SearchMemoryResponse
from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types

from . import clock
from .cost import EXTRACTION_OVERHEAD_TOKENS, CostMeter, ExtractionPolicy, est_tokens
from .events import TOPIC_MEMORY, EventBus
from .governance import Assurance, AuditLog, Classification, evaluate_write, redact
from .lifecycle import POLICIES, DAY, MemoryLedger, MemoryRecord, expiry_for, subject_key
from .local_memory import LocalMemoryService
from .quotas import PartitionRouter
from .resilience import CircuitBreaker, FaultInjector, MemoryUnavailable, Outbox, OutboxItem, idempotency_key

log = logging.getLogger(__name__)


@dataclass
class TurnContext:
    customer_token: str
    app_name: str
    partition: str
    channel: str
    assurance: Assurance
    actor: str
    session_id: str | None = None
    consent: bool = True
    trace: dict = field(default_factory=lambda: {"memories_used": [], "writes": [], "extraction": [], "degraded": []})
    memory: "GovernedMemoryService | None" = None
    live: object = None


_CURRENT: contextvars.ContextVar[TurnContext | None] = contextvars.ContextVar("cx_turn", default=None)


def current_turn() -> TurnContext | None:
    return _CURRENT.get()


def set_turn(ctx: TurnContext | None):
    return _CURRENT.set(ctx)


def reset_turn(token) -> None:
    _CURRENT.reset(token)


def _entry_text(e: MemoryEntry) -> str:
    return " ".join(p.text or "" for p in (e.content.parts or [])) if e.content else ""


class GovernedMemoryService(BaseMemoryService):
    def __init__(self, inner: BaseMemoryService, *, router: PartitionRouter, audit: AuditLog, bus: EventBus,
                 ledger: MemoryLedger | None = None, meter: CostMeter | None = None,
                 policy: ExtractionPolicy | None = None, read_timeout_s: float = 0.8) -> None:
        self.inner = inner
        self.router = router
        self.audit = audit
        self.bus = bus
        self.ledger = ledger or MemoryLedger()
        self.meter = meter or CostMeter()
        self.policy = policy or ExtractionPolicy()
        self.faults = FaultInjector()
        self.breaker = CircuitBreaker()
        self.outbox = Outbox()
        self.read_timeout_s = read_timeout_s
        self.consent_withdrawn: set[str] = set()
        self.is_local = isinstance(inner, LocalMemoryService)

    # ------------------------------------------------------------------ helpers
    def _ctx(self, app_name: str, user_id: str) -> TurnContext:
        ctx = current_turn()
        if ctx and ctx.customer_token == user_id:
            return ctx
        part = self.router.by_app(app_name)
        return TurnContext(user_id, app_name, part.name, "system", Assurance.SYSTEM, actor="system",
                           consent=user_id not in self.consent_withdrawn)

    async def _guarded(self, coro_factory, *, timeout: float | None = None):
        """Run one Memory Bank call behind the breaker, fault injector and timeout."""
        if not self.breaker.allow():
            raise MemoryUnavailable("circuit open: Memory Bank calls suspended")
        async def call():
            await self.faults.before_call()  # inside the timeout: a slow bank counts against the budget
            return await coro_factory()
        try:
            result = await (asyncio.wait_for(call(), timeout) if timeout else call())
        except (asyncio.TimeoutError, MemoryUnavailable, Exception) as exc:
            self.breaker.failure()
            if isinstance(exc, asyncio.TimeoutError):
                raise MemoryUnavailable(f"Memory Bank read exceeded {int((timeout or 0) * 1000)} ms budget") from None
            raise MemoryUnavailable(str(exc)) from None
        self.breaker.success()
        return result

    def _inner_meta(self, rec: MemoryRecord) -> dict:
        if self.is_local:
            return {"topic": rec.topic, "memory_id": rec.memory_id}
        meta: dict = {"memory_id": rec.memory_id}
        if rec.expires_at:
            meta["ttl"] = f"{max(60, int(rec.expires_at - clock.now()))}s"  # native Memory Bank expiry
        return meta

    async def _delete_inner(self, app_name: str, token: str, memory_id: str, reason: str) -> None:
        async def run():
            await self._guarded(lambda: self.inner.delete_memory(app_name=app_name, user_id=token, memory_id=memory_id))
        try:
            await run()
        except Exception as exc:
            self.outbox.enqueue(OutboxItem(idempotency_key("delete", memory_id), "delete", f"{reason}: delete {memory_id}",
                                           run, last_error=str(exc), customer_token=token))

    # ------------------------------------------------------------------ read
    async def search_memory(self, *, app_name: str, user_id: str, query: str) -> SearchMemoryResponse:
        ctx = self._ctx(app_name, user_id)
        part = self.router.by_app(app_name)
        if not part.buckets["read"].try_take():
            self.meter.add(part.name, throttled=1, degraded_reads=1)
            ctx.trace["degraded"].append("memory read throttled by partition quota: served from live context + profile")
            return SearchMemoryResponse(memories=[])
        try:
            res = await self._guarded(lambda: self.inner.search_memory(app_name=app_name, user_id=user_id, query=query),
                                      timeout=self.read_timeout_s)
        except MemoryUnavailable as exc:
            self.meter.add(part.name, degraded_reads=1)
            ctx.trace["degraded"].append(f"memory read failed ({exc}): served from live context + profile")
            self.audit.append("READ_DEGRADED", actor=ctx.actor, customer_token=user_id, details={"error": str(exc)})
            return SearchMemoryResponse(memories=[])
        self.meter.add(part.name, retrieve_calls=1)

        kept, dropped, returned = [], [], []
        for m in res.memories:
            rec = self.ledger.lookup(user_id, getattr(m, "id", None) or (m.custom_metadata or {}).get("memory_id"), _entry_text(m))
            mid = rec.memory_id if rec else (getattr(m, "id", None) or "generated")
            if not self.ledger.usable(rec):
                dropped.append(mid)
                continue
            if rec and rec.classification == Classification.RESTRICTED.name and ctx.assurance < Assurance.MEDIUM:
                dropped.append(mid)
                continue
            kept.append(m)
            returned.append(mid)
            used = {"memory_id": mid, "text": _entry_text(m)[:200]}
            if rec:
                used.update(topic=rec.topic, source=f"{rec.source_kind}/{rec.source_channel}", version=rec.version,
                            age_days=round((clock.now() - rec.created_at) / DAY, 1),
                            expires=clock.iso(rec.expires_at) if rec.expires_at else "never")
            if all(u["memory_id"] != mid for u in ctx.trace["memories_used"]):
                ctx.trace["memories_used"].append(used)
        self.audit.append("READ", actor=ctx.actor, customer_token=user_id,
                          details={"channel": ctx.channel, "assurance": ctx.assurance.name, "query": query[:80],
                                   "returned": returned,
                                   "filtered_out": dropped})
        return SearchMemoryResponse(memories=kept)

    # ------------------------------------------------------------------ explicit write
    async def write_fact(self, *, app_name: str, user_id: str, text: str, topic: str, source_kind: str = "agent_tool",
                         ctx: TurnContext | None = None) -> dict:
        ctx = ctx or self._ctx(app_name, user_id)
        part = self.router.by_app(app_name)
        pol = POLICIES.get(topic)
        if pol is None:
            return {"status": "rejected", "reason": f"unknown topic '{topic}'", "allowed": list(POLICIES)}
        consent = ctx.consent and user_id not in self.consent_withdrawn
        decision = evaluate_write(text, topic, ctx.assurance, consent, pol.min_write_assurance)
        if not decision.allowed:
            self.meter.add(part.name, writes_blocked=1)
            self.audit.append("WRITE_BLOCKED", actor=ctx.actor, customer_token=user_id,
                              details={"topic": topic, "reasons": decision.reasons, "channel": ctx.channel})
            out = {"status": "rejected", "reason": "; ".join(decision.reasons), "needs_step_up": decision.needs_step_up}
            ctx.trace["writes"].append({**out, "topic": topic})
            return out
        if ctx.assurance is not Assurance.SYSTEM and not self.router.take_daily_write(part, user_id):
            self.meter.add(part.name, writes_blocked=1)
            out = {"status": "rejected", "reason": f"daily write quota for this customer reached ({part.daily_writes_per_customer}/day)"}
            self.audit.append("WRITE_BLOCKED", actor=ctx.actor, customer_token=user_id, details={"topic": topic, **out})
            ctx.trace["writes"].append({**out, "topic": topic})
            return out

        now = clock.now()
        rec = MemoryRecord(
            memory_id=self.ledger.new_id(), customer_token=user_id, app_name=app_name, topic=topic,
            text=decision.text, source_kind=source_kind, source_channel=ctx.channel,
            classification=decision.classification.name, created_at=now,
            expires_at=expiry_for(topic, decision.text, now), subject_key=subject_key(topic, decision.text),
            redacted=decision.redacted)
        superseded = self.ledger.add(rec)
        entry = MemoryEntry(id=rec.memory_id, author=source_kind,
                            content=types.Content(role="user", parts=[types.Part(text=rec.text)]),
                            timestamp=clock.iso(now), custom_metadata={"topic": topic, "memory_id": rec.memory_id})

        status = "saved"

        async def run_write():
            await self._guarded(lambda: self.inner.add_memory(app_name=app_name, user_id=user_id, memories=[entry],
                                                              custom_metadata=self._inner_meta(rec)))
        if not part.buckets["write"].try_take():
            status = "queued"
            self.meter.add(part.name, throttled=1, outboxed_writes=1)
            self.outbox.enqueue(OutboxItem(idempotency_key(user_id, rec.text), "write", f"throttled write {rec.memory_id}",
                                           run_write, last_error="partition write quota", customer_token=user_id))
        else:
            try:
                await run_write()
            except MemoryUnavailable as exc:
                status = "queued"
                self.meter.add(part.name, outboxed_writes=1)
                ctx.trace["degraded"].append(f"memory write deferred to outbox ({exc})")
                self.outbox.enqueue(OutboxItem(idempotency_key(user_id, rec.text), "write", f"write {rec.memory_id}",
                                               run_write, last_error=str(exc), customer_token=user_id))
        for old in superseded:
            await self._delete_inner(app_name, user_id, old.memory_id, "superseded")
            await self.bus.publish(TOPIC_MEMORY, user_id, {"type": "MEMORY_SUPERSEDED", "memory_id": old.memory_id,
                                                           "by": rec.memory_id, "topic": topic})
        await self._enforce_cap(part, app_name, user_id)

        self.meter.add(part.name, explicit_writes=1)
        self.audit.append("WRITE", actor=ctx.actor, customer_token=user_id,
                          details={"memory_id": rec.memory_id, "topic": topic, "source": source_kind, "channel": ctx.channel,
                                   "classification": rec.classification, "redacted": decision.redacted, "status": status,
                                   "supersedes": rec.supersedes, "expires_at": clock.iso(rec.expires_at)})
        await self.bus.publish(TOPIC_MEMORY, user_id, {"type": "MEMORY_WRITTEN", "memory_id": rec.memory_id, "topic": topic,
                                                       "source": source_kind, "status": status})
        out = {"status": status, "topic": topic, "fact": rec.text, "memory_id": rec.memory_id, "version": rec.version,
               "supersedes": rec.supersedes, "expires_at": clock.iso(rec.expires_at) or "never", "redacted": decision.redacted}
        ctx.trace["writes"].append(out)
        return out

    async def _enforce_cap(self, part, app_name: str, token: str) -> None:
        over = len(self.ledger.active(token)) - part.max_memories_per_customer
        for rec in self.ledger.eviction_candidates(token)[:max(0, over)]:
            self.ledger.mark(rec, "EVICTED")
            await self._delete_inner(app_name, token, rec.memory_id, "evicted (per-customer cap)")
            self.audit.append("EVICT", actor="system", customer_token=token, details={"memory_id": rec.memory_id, "topic": rec.topic})

    # ------------------------------------------------------------------ ADK write surface
    async def add_memory(self, *, app_name: str, user_id: str, memories: Sequence[MemoryEntry],
                         custom_metadata: Mapping[str, object] | None = None) -> None:
        meta = dict(custom_metadata or {})
        for m in memories:
            topic = meta.get("topic") or (m.custom_metadata or {}).get("topic") or "key_conversation_details"
            await self.write_fact(app_name=app_name, user_id=user_id, text=_entry_text(m), topic=str(topic),
                                  source_kind=str(meta.get("source_kind") or m.author or "agent_tool"))

    async def add_events_to_memory(self, *, app_name: str, user_id: str, events, session_id: str | None = None,
                                   custom_metadata: Mapping[str, object] | None = None) -> None:
        ctx = self._ctx(app_name, user_id)
        part = self.router.by_app(app_name)
        user_text = " ".join(_entry_text(e) for e in events if getattr(e, "author", "") == "user" and e.content)
        if not ctx.consent or user_id in self.consent_withdrawn:
            ctx.trace["extraction"].append("skip: memory consent withdrawn")
            return
        batch, decision = self.policy.offer((app_name, user_id, session_id), user_text, list(events))
        ctx.trace["extraction"].append(decision)
        if batch is None:
            self.meter.add(part.name, extraction_skipped=1)
            return
        await self._extract(app_name, user_id, batch, ctx, part, session_id)

    async def flush_session(self, app_name: str, user_id: str, session_id: str) -> str:
        batch = self.policy.flush((app_name, user_id, session_id))
        if not batch:
            return "nothing buffered"
        ctx = self._ctx(app_name, user_id)
        await self._extract(app_name, user_id, batch, ctx, self.router.by_app(app_name), session_id)
        return f"extracted {len(batch)} buffered events"

    async def _extract(self, app_name, user_id, events, ctx: TurnContext, part, session_id) -> None:
        # Redact before anything leaves the gateway: Memory Bank never sees a full number.
        clean_events = []
        for ev in events:
            if ev.content and ev.content.parts:
                parts = [types.Part(text=redact(p.text)[0]) if p.text else p for p in ev.content.parts]
                ev = ev.model_copy(update={"content": types.Content(role=ev.content.role, parts=parts)})
            clean_events.append(ev)
        tokens = sum(est_tokens(_entry_text(e)) for e in clean_events if e.content)

        async def run():
            if self.is_local:
                # Offline stand-in for Memory Bank extraction: keep salient customer statements as facts.
                # If the agent already saved an explicit fact this turn, Memory Bank would consolidate the
                # extraction into it, so the stand-in skips the duplicate.
                if any(w.get("status") in ("saved", "queued") for w in ctx.trace["writes"]):
                    return
                for ev in clean_events:
                    if getattr(ev, "author", "") == "user" and ev.content:
                        txt = _entry_text(ev).strip()
                        # Memory Bank extracts facts, not questions; mimic that crudely offline.
                        if txt and not txt.rstrip().endswith("?"):
                            await self.write_fact(app_name=app_name, user_id=user_id, text=f"Customer said: {txt}",
                                                  topic="key_conversation_details", source_kind="extraction", ctx=ctx)
            else:
                await self._guarded(lambda: self.inner.add_events_to_memory(app_name=app_name, user_id=user_id,
                                                                           events=clean_events, session_id=session_id))
        if not part.buckets["extract"].try_take():
            self.meter.add(part.name, throttled=1)
            self.outbox.enqueue(OutboxItem(idempotency_key("extract", user_id, str(session_id), str(time.monotonic())),
                                           "extract", f"throttled extraction of {len(clean_events)} events", run,
                                           last_error="partition extract quota", customer_token=user_id))
            ctx.trace["extraction"].append("extract quota exhausted: queued in outbox")
            return
        try:
            if self.is_local:
                await self.faults.before_call()
            await run()
        except Exception as exc:
            self.outbox.enqueue(OutboxItem(idempotency_key("extract", user_id, str(session_id), str(time.monotonic())),
                                           "extract", f"extraction of {len(clean_events)} events", run,
                                           last_error=str(exc), customer_token=user_id))
            ctx.trace["degraded"].append(f"extraction deferred to outbox ({exc})")
            return
        self.meter.add(part.name, extraction_calls=1, extraction_input_tokens=tokens + EXTRACTION_OVERHEAD_TOKENS)
        self.audit.append("EXTRACT", actor=ctx.actor, customer_token=user_id,
                          details={"events": len(clean_events), "est_input_tokens": tokens + EXTRACTION_OVERHEAD_TOKENS, "channel": ctx.channel})

    async def add_session_to_memory(self, session) -> None:
        await self.add_events_to_memory(app_name=session.app_name, user_id=session.user_id, events=session.events,
                                        session_id=session.id)

    async def delete_memory(self, *, app_name: str, user_id: str, memory_id: str | None = None) -> None:
        rec = self.ledger.get(memory_id)
        if rec:
            self.ledger.mark(rec, "ERASED")
        await self._delete_inner(app_name, user_id, memory_id or "", "delete")

    # ------------------------------------------------------------------ lifecycle operations
    async def sweep_expired(self) -> list[dict]:
        out = []
        for rec in self.ledger.due_for_expiry():
            self.ledger.mark(rec, "EXPIRED")
            await self._delete_inner(rec.app_name, rec.customer_token, rec.memory_id, "expired")
            self.audit.append("EXPIRE", actor="lifecycle-sweeper", customer_token=rec.customer_token,
                              details={"memory_id": rec.memory_id, "topic": rec.topic})
            await self.bus.publish(TOPIC_MEMORY, rec.customer_token, {"type": "MEMORY_EXPIRED", "memory_id": rec.memory_id,
                                                                      "topic": rec.topic})
            out.append({"memory_id": rec.memory_id, "topic": rec.topic, "text": rec.text})
        return out

    async def erase_customer(self, app_name: str, token: str, *, actor: str, reason: str) -> dict:
        if token in self.ledger.legal_holds:
            self.audit.append("ERASE_REFUSED", actor=actor, customer_token=token, details={"reason": "legal hold active"})
            return {"status": "refused", "reason": "legal hold is active: erasure deferred until the hold is released"}
        n = 0
        for rec in self.ledger.all_for(token):
            if rec.status in ("ACTIVE", "SUPERSEDED"):
                self.ledger.mark(rec, "ERASED")
                n += 1
        if self.is_local:
            self.inner.purge(app_name, token)
        elif hasattr(self.inner, "purge_scope"):
            try:  # also removes memories Memory Bank generated itself (not in the ledger)
                await self._guarded(lambda: self.inner.purge_scope(app_name, token))
            except Exception as exc:
                async def retry():
                    await self._guarded(lambda: self.inner.purge_scope(app_name, token))
                self.outbox.enqueue(OutboxItem(idempotency_key("purge", token), "purge", "erasure: purge scope",
                                               retry, last_error=str(exc), customer_token=token))
        self.audit.append("ERASE", actor=actor, customer_token=token, details={"reason": reason, "records": n})
        await self.bus.publish(TOPIC_MEMORY, token, {"type": "CUSTOMER_ERASED", "records": n, "reason": reason})
        return {"status": "erased", "records": n}

    def set_consent(self, token: str, granted: bool, actor: str) -> None:
        (self.consent_withdrawn.discard if granted else self.consent_withdrawn.add)(token)
        self.audit.append("CONSENT_" + ("GRANTED" if granted else "WITHDRAWN"), actor=actor, customer_token=token)

    def set_legal_hold(self, token: str, on: bool, actor: str) -> None:
        (self.ledger.legal_holds.add if on else self.ledger.legal_holds.discard)(token)
        self.audit.append("LEGAL_HOLD_" + ("ON" if on else "OFF"), actor=actor, customer_token=token)

    async def drain_outbox(self, force: bool = False) -> dict:
        return await self.outbox.drain(force=force)

    def status(self) -> dict:
        return {"breaker": self.breaker.view(), "faults": self.faults.view(), "outbox": self.outbox.view(),
                "extraction_policy": {"mode": self.policy.mode, "batch_turns": self.policy.batch_turns,
                                      "buffered_events": self.policy.pending()},
                "read_timeout_ms": int(self.read_timeout_s * 1000), "partitions": self.router.view(),
                "meter": self.meter.snapshot(), "audit": self.audit.verify()}
