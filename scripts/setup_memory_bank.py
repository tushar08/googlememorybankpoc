"""Phase 1: create the Agent Platform (Agent Engine) instance that hosts Sessions + Memory Bank
with the 8 topics (4 managed, 4 custom), a few-shot extraction example and lifecycle TTLs.

One instance per partition in production (consumer / business, per region). The PoC uses one instance
and separates partitions by app_name in the memory scope.

Usage:
    python scripts/setup_memory_bank.py            # create
    python scripts/setup_memory_bank.py --show     # print config of AGENT_ENGINE_ID
Prints AGENT_ENGINE_ID to put in .env.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vertexai  # noqa: E402

from cx_memory.config import settings  # noqa: E402
from cx_memory.memory_topics import generate_examples_config, memory_topics_config  # noqa: E402


DAY_S = 86400


def memory_bank_config() -> dict:
    cfg: dict = {
        "customization_configs": [{
            "memory_topics": memory_topics_config(),
            "generate_memories_examples": generate_examples_config(),
        }],
        # Engine-wide backstop TTLs. Explicit facts written by the gateway carry their own per-topic
        # `ttl` (see cx_memory/lifecycle.py); these cover memories Memory Bank generates by itself.
        "ttl_config": {
            "granular_ttl_config": {
                "create_ttl": f"{365 * DAY_S}s",
                "generate_created_ttl": f"{90 * DAY_S}s",
                "generate_updated_ttl": f"{90 * DAY_S}s",
            },
            "memory_revision_default_ttl": f"{365 * DAY_S}s",
        },
    }
    p, loc = settings.project_id, settings.memory_location
    if settings.memory_generation_model:
        cfg["generation_config"] = {"model": f"projects/{p}/locations/{loc}/publishers/google/models/{settings.memory_generation_model}"}
    if settings.embedding_model:
        cfg["similarity_search_config"] = {"embedding_model": f"projects/{p}/locations/{loc}/publishers/google/models/{settings.embedding_model}"}
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print the config without calling GCP")
    args = ap.parse_args()

    if args.dry_run:
        print(json.dumps(memory_bank_config(), indent=2))
        return
    if not settings.project_id:
        sys.exit("Set GOOGLE_CLOUD_PROJECT in .env")

    client = vertexai.Client(project=settings.project_id, location=settings.memory_location)

    if args.show:
        name = f"projects/{settings.project_id}/locations/{settings.memory_location}/reasoningEngines/{settings.agent_engine_id}"
        engine = client.agent_engines.get(name=name)
        print(engine.api_resource)
        return

    engine = client.agent_engines.create(config={
        "display_name": "cx-unified-memory-poc",
        "description": "Cross-channel customer memory PoC (synthetic data only)",
        "context_spec": {"memory_bank_config": memory_bank_config()},
    })
    name = engine.api_resource.name
    print(f"Created: {name}")
    print(f"\nAdd to .env:\nAGENT_ENGINE_ID={name.split('/')[-1]}")


if __name__ == "__main__":
    main()
