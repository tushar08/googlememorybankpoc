"""Phase 3: load synthetic profiles from the 5 source systems into Memory Bank (tier 2).

Tier 1 (session state) is seeded automatically by the gateway when each session is created,
from the same merged profile, so both tiers carry identical source data.

Usage:
    python scripts/ingest_profiles.py                 # all customers
    python scripts/ingest_profiles.py --customer CUST-1001 --reset
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cx_memory.config import settings  # noqa: E402
from cx_memory.local_memory import fact_entry  # noqa: E402
from cx_memory.profiles import all_customer_ids, load_profile, profile_facts  # noqa: E402
from cx_memory.services import build_services  # noqa: E402


def reset_customer(customer_id: str) -> int:
    """Delete existing memories for this customer's scope (Vertex only)."""
    import vertexai

    client = vertexai.Client(project=settings.project_id, location=settings.memory_location)
    engine = f"projects/{settings.project_id}/locations/{settings.memory_location}/reasoningEngines/{settings.agent_engine_id}"
    n = 0
    for m in client.agent_engines.memories.retrieve(name=engine, scope={"app_name": settings.app_name, "user_id": customer_id}):
        client.agent_engines.memories.delete(name=m.memory.name)
        n += 1
    return n


async def ingest(customer_ids: list[str], reset: bool) -> None:
    _, memory = build_services()
    for cid in customer_ids:
        if reset and settings.use_vertex:
            print(f"{cid}: deleted {reset_customer(cid)} existing memories")
        facts = profile_facts(load_profile(cid))
        # Direct creation (no consolidation): source-system facts are authoritative and already atomic.
        await memory.add_memory(app_name=settings.app_name, user_id=cid, memories=[fact_entry(f) for f in facts])
        print(f"{cid}: wrote {len(facts)} facts")
        res = await memory.search_memory(app_name=settings.app_name, user_id=cid, query="card dispute travel preferences")
        print(f"{cid}: retrieval check -> {len(res.memories)} memories returned")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--customer", action="append")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    print(f"backend={settings.backend} app_name={settings.app_name}")
    asyncio.run(ingest(a.customer or all_customer_ids(), a.reset))


if __name__ == "__main__":
    main()
