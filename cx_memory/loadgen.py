"""Memory-layer load generator: throughput, latency, quotas and extraction cost under a burst.

It drives the GovernedMemoryService directly (one similarity search + one turn sync per turn), with
injected Memory Bank latency. Leaving the LLM out isolates the memory layer, which is where the quota and
extraction-cost questions are. Each run uses a fresh service and a fresh quota budget, so it never touches
demo customers.
"""
from __future__ import annotations

import asyncio
import random
import statistics
import time
import uuid

from google.adk.events import Event
from google.genai import types

from .cost import CostMeter, ExtractionPolicy
from .events import EventBus
from .governance import Assurance, AuditLog
from .governed_memory import GovernedMemoryService, TurnContext, reset_turn, set_turn
from .local_memory import LocalMemoryService
from .quotas import PartitionRouter

UTTERANCES = [
    ("hi there", False), ("thanks, that's all", False), ("ok great", False), ("can you hold on a second", False),
    ("I'm flying to Madrid on March 3", True), ("what's happening with my dispute on the hotel charge", True),
    ("please only call me after 6pm", True), ("my card was declined at the supermarket", True),
    ("I lost my card yesterday", True), ("how long will the refund take", True),
]


async def run_burst(*, contacts: int = 200, concurrency: int = 50, turns_per_contact: int = 3,
                    mb_latency_ms: int = 80, mode: str = "salient", app_base: str = "cx_unified", seed: int = 7) -> dict:
    rng = random.Random(seed)
    router = PartitionRouter(app_base)
    part = router.partitions["consumer"]
    svc = GovernedMemoryService(LocalMemoryService(), router=router, audit=AuditLog(), bus=EventBus(),
                                meter=CostMeter(), policy=ExtractionPolicy(mode=mode, batch_turns=6), read_timeout_s=2.0)
    svc.faults.memory_bank, svc.faults.slow_ms = ("slow", mb_latency_ms) if mb_latency_ms else ("ok", 0)
    latencies: list[float] = []
    sem = asyncio.Semaphore(concurrency)

    async def contact(i: int) -> None:
        token = f"loadtest_{i:05d}"
        session_id = str(uuid.uuid4())
        async with sem:
            for t in range(turns_per_contact):
                text, _ = rng.choice(UTTERANCES)
                ctx = TurnContext(customer_token=token, app_name=part.app_name, partition=part.name, channel="voice",
                                  assurance=Assurance.MEDIUM, actor="loadgen", session_id=session_id, memory=svc)
                h = set_turn(ctx)
                t0 = time.perf_counter()
                try:
                    await svc.search_memory(app_name=part.app_name, user_id=token, query=text)
                    inv = f"inv-{i}-{t}"
                    events = [Event(author="user", invocation_id=inv, content=types.Content(role="user", parts=[types.Part(text=text)])),
                              Event(author="voice_agent", invocation_id=inv,
                                    content=types.Content(role="model", parts=[types.Part(text="Noted, thank you.")]))]
                    await svc.add_events_to_memory(app_name=part.app_name, user_id=token, events=events, session_id=session_id)
                finally:
                    reset_turn(h)
                latencies.append((time.perf_counter() - t0) * 1000)
            await svc.flush_session(part.app_name, token, session_id)

    started = time.perf_counter()
    await asyncio.gather(*(contact(i) for i in range(contacts)))
    elapsed = time.perf_counter() - started
    row = svc.meter.by_partition[part.name]
    row["turns"] = contacts * turns_per_contact
    lat = sorted(latencies)
    return {
        "inputs": {"contacts": contacts, "concurrency": concurrency, "turns_per_contact": turns_per_contact,
                   "mb_latency_ms": mb_latency_ms, "extraction_mode": mode},
        "throughput": {"elapsed_s": round(elapsed, 2), "turns_per_s": round(row["turns"] / elapsed, 1),
                       "p50_ms": round(statistics.median(lat), 1), "p95_ms": round(lat[int(len(lat) * 0.95) - 1], 1),
                       "max_ms": round(lat[-1], 1)},
        "memory_ops": {k: row[k] for k in ("retrieve_calls", "extraction_calls", "extraction_skipped", "throttled",
                                           "degraded_reads", "explicit_writes", "outboxed_writes")},
        "quotas_after": part.view()["quotas"],
        "outbox": {"pending": len(svc.outbox.items), "dead_lettered": len(svc.outbox.dlq)},
        "cost": svc.meter.estimate(row),
    }
