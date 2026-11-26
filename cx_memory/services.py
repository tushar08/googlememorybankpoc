"""Builds the session + memory services for the selected backend.

The memory service returned here is the *inner* store. The gateway wraps it in GovernedMemoryService.
"""
from __future__ import annotations

import asyncio

from .config import Settings, settings as default_settings


def _vertex_memory_service(cfg: Settings):
    from google.adk.memory import VertexAiMemoryBankService

    class LifecycleVertexMemoryBankService(VertexAiMemoryBankService):
        """Adds what the governed layer needs for supersede / expiry / erasure: delete by id and purge by scope."""

        def __init__(self) -> None:
            super().__init__(project=cfg.project_id, location=cfg.memory_location, agent_engine_id=cfg.agent_engine_id)
            self._engine = (f"projects/{cfg.project_id}/locations/{cfg.memory_location}"
                            f"/reasoningEngines/{cfg.agent_engine_id}")

        def _client(self):
            import vertexai

            return vertexai.Client(project=cfg.project_id, location=cfg.memory_location)

        async def delete_memory(self, *, app_name: str, user_id: str, memory_id: str | None = None) -> None:
            if not memory_id:
                return
            client = self._client()
            await asyncio.to_thread(client.agent_engines.memories.delete, name=f"{self._engine}/memories/{memory_id}")

        async def purge_scope(self, app_name: str, user_id: str) -> int:
            """Erasure: delete every memory in the scope, including ones Memory Bank generated itself."""
            client = self._client()

            def run() -> int:
                n = 0
                for m in client.agent_engines.memories.retrieve(name=self._engine, scope={"app_name": app_name, "user_id": user_id}):
                    client.agent_engines.memories.delete(name=m.memory.name)
                    n += 1
                return n

            return await asyncio.to_thread(run)

    return LifecycleVertexMemoryBankService()


def build_services(cfg: Settings = default_settings):
    if cfg.use_vertex:
        from google.adk.sessions import VertexAiSessionService

        if not (cfg.project_id and cfg.agent_engine_id):
            raise RuntimeError("BACKEND=vertex needs GOOGLE_CLOUD_PROJECT and AGENT_ENGINE_ID (run scripts/setup_memory_bank.py)")
        session_service = VertexAiSessionService(
            project=cfg.project_id, location=cfg.memory_location, agent_engine_id=cfg.agent_engine_id
        )
        return session_service, _vertex_memory_service(cfg)

    from google.adk.sessions import InMemorySessionService

    from .local_memory import LocalMemoryService

    return InMemorySessionService(), LocalMemoryService()
