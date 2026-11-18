"""Phase 1 checklist item: verify GCP project, APIs, model on the global endpoint,
and Memory Bank + Sessions in us-central1. Prints PASS/FAIL per check.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cx_memory.config import settings  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


async def main() -> None:
    check("GOOGLE_CLOUD_PROJECT set", bool(settings.project_id), settings.project_id)
    check("AGENT_ENGINE_ID set", bool(settings.agent_engine_id), settings.agent_engine_id)
    check("GOOGLE_CLOUD_LOCATION is global", settings.model_location == "global", settings.model_location)

    # 1. Model on global endpoint
    try:
        from google import genai

        client = genai.Client(vertexai=True, project=settings.project_id, location=settings.model_location)
        r = client.models.generate_content(model=settings.model, contents="Reply with the single word: ready")
        check(f"Model {settings.model} @ {settings.model_location}", "ready" in (r.text or "").lower(), repr(r.text))
    except Exception as e:  # noqa: BLE001
        check(f"Model {settings.model} @ {settings.model_location}", False,
              f"{e}\n  -> confirm the exact model ID in Model Garden and set MODEL in .env")

    # 2. Agent Engine exists
    try:
        import vertexai

        vc = vertexai.Client(project=settings.project_id, location=settings.memory_location)
        name = f"projects/{settings.project_id}/locations/{settings.memory_location}/reasoningEngines/{settings.agent_engine_id}"
        vc.agent_engines.get(name=name)
        check("Agent Engine reachable", True, name)
    except Exception as e:  # noqa: BLE001
        check("Agent Engine reachable", False, str(e))

    # 3. Sessions round-trip
    try:
        from google.adk.sessions import VertexAiSessionService

        ss = VertexAiSessionService(project=settings.project_id, location=settings.memory_location,
                                    agent_engine_id=settings.agent_engine_id)
        s = await ss.create_session(app_name=settings.app_name, user_id="verify-user", state={"probe": 1})
        await ss.delete_session(app_name=settings.app_name, user_id="verify-user", session_id=s.id)
        check("Sessions create/delete", True, s.id)
    except Exception as e:  # noqa: BLE001
        check("Sessions create/delete", False, str(e))

    # 4. Memory Bank search round-trip
    try:
        from google.adk.memory import VertexAiMemoryBankService

        ms = VertexAiMemoryBankService(project=settings.project_id, location=settings.memory_location,
                                       agent_engine_id=settings.agent_engine_id)
        await ms.search_memory(app_name=settings.app_name, user_id="verify-user", query="probe")
        check("Memory Bank search", True)
    except Exception as e:  # noqa: BLE001
        check("Memory Bank search", False, str(e))

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
