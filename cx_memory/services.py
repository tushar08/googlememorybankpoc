"""Builds the session + memory services for the selected backend."""
from __future__ import annotations

from .config import Settings, settings as default_settings


def build_services(cfg: Settings = default_settings):
    if cfg.use_vertex:
        from google.adk.memory import VertexAiMemoryBankService
        from google.adk.sessions import VertexAiSessionService

        if not (cfg.project_id and cfg.agent_engine_id):
            raise RuntimeError("BACKEND=vertex needs GOOGLE_CLOUD_PROJECT and AGENT_ENGINE_ID (run scripts/setup_memory_bank.py)")
        session_service = VertexAiSessionService(
            project=cfg.project_id, location=cfg.memory_location, agent_engine_id=cfg.agent_engine_id
        )
        memory_service = VertexAiMemoryBankService(
            project=cfg.project_id, location=cfg.memory_location, agent_engine_id=cfg.agent_engine_id
        )
        return session_service, memory_service

    from google.adk.sessions import InMemorySessionService

    from .local_memory import LocalMemoryService

    return InMemorySessionService(), LocalMemoryService()
