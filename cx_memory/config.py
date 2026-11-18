"""Central configuration. Every value can be overridden by environment variable (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

try:  # optional: load .env when python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # --- GCP ---------------------------------------------------------------
    project_id: str = field(default_factory=lambda: _env("GOOGLE_CLOUD_PROJECT"))
    # Model calls go to the *global* endpoint (GOOGLE_CLOUD_LOCATION=global is read by google-genai).
    model_location: str = field(default_factory=lambda: _env("GOOGLE_CLOUD_LOCATION", "global"))
    # Memory Bank + Sessions live on a regional Agent Engine instance.
    memory_location: str = field(default_factory=lambda: _env("MEMORY_LOCATION", "us-central1"))
    agent_engine_id: str = field(default_factory=lambda: _env("AGENT_ENGINE_ID"))

    # --- Model -------------------------------------------------------------
    # Confirm the exact model ID with `python scripts/verify_env.py` before the demo.
    model: str = field(default_factory=lambda: _env("MODEL", "gemini-3.8-flash"))
    memory_generation_model: str = field(default_factory=lambda: _env("MEMORY_GEN_MODEL", ""))
    embedding_model: str = field(default_factory=lambda: _env("EMBEDDING_MODEL", ""))

    # --- Unified memory scope (one bank, all channels) ----------------------
    app_name: str = field(default_factory=lambda: _env("APP_NAME", "cx_unified"))

    # --- Runtime mode ------------------------------------------------------
    # "vertex" = Agent Platform Sessions + Memory Bank; "local" = in-process (offline tests/dev)
    backend: str = field(default_factory=lambda: _env("BACKEND", "local"))

    # How many recent cross-channel turns to keep in the real-time tier
    live_context_turns: int = field(default_factory=lambda: int(_env("LIVE_CONTEXT_TURNS", "6")))

    @property
    def use_vertex(self) -> bool:
        return self.backend.lower() == "vertex"


settings = Settings()
