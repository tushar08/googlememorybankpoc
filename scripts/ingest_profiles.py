"""Phase 3: load synthetic profiles from the 5 source systems into Memory Bank (tier 2).

Memories are keyed by customer token (not customer ID) and written into the customer's partition
(consumer / business). The CRM name line is not copied into memory; names come from the live profile.

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
from cx_memory.profiles import all_customer_ids  # noqa: E402


def reset_customer(gw, customer_id: str) -> int:
    """Delete existing memories for this customer's tokenized scope (Vertex only)."""
    import vertexai

    part, token = gw.scope(customer_id)
    client = vertexai.Client(project=settings.project_id, location=settings.memory_location)
    engine = f"projects/{settings.project_id}/locations/{settings.memory_location}/reasoningEngines/{settings.agent_engine_id}"
    n = 0
    for m in client.agent_engines.memories.retrieve(name=engine, scope={"app_name": part.app_name, "user_id": token}):
        client.agent_engines.memories.delete(name=m.memory.name)
        n += 1
    return n


async def ingest(customer_ids: list[str], reset: bool) -> None:
    from cx_memory.gateway import OmnichannelGateway

    gw = OmnichannelGateway()
    for cid in customer_ids:
        part, token = gw.scope(cid)
        if reset and settings.use_vertex:
            print(f"{cid} -> {token}: deleted {reset_customer(gw, cid)} existing memories")
        # Through the governed layer: redaction, per-topic TTL and ledger ids apply to seeded facts too.
        n = await gw.seed_customer(cid)
        print(f"{cid} -> {part.app_name}/{token}: wrote {n} facts")
        res = await gw.memory_service.search_memory(app_name=part.app_name, user_id=token, query="card dispute travel preferences")
        print(f"{cid}: retrieval check -> {len(res.memories)} memories returned")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--customer", action="append")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    print(f"backend={settings.backend} partitions={settings.app_name}_consumer, {settings.app_name}_business")
    asyncio.run(ingest(a.customer or all_customer_ids(), a.reset))


if __name__ == "__main__":
    main()
