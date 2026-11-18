"""OmnichannelGateway: identity -> session -> deterministic routing -> agent -> channel guard.

Deterministic routing: the inbound channel alone selects the agent (no LLM router), so the
path is predictable, cheap and auditable. Model and memory endpoints come from config.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from google.adk.runners import Runner
from google.genai import types

from . import callbacks
from .agents import build_all_agents
from .channels import Channel, enforce_voice, validate
from .config import Settings, settings as default_settings
from .identity import IdentityResolver
from .live_context import LiveContextStore
from .profiles import load_profile, profile_summary
from .services import build_services

log = logging.getLogger(__name__)


@dataclass
class GatewayReply:
    customer_id: str
    channel: str
    session_id: str
    agent: str
    text: str
    style_violations: list[str] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)


class OmnichannelGateway:
    def __init__(self, cfg: Settings = default_settings, model=None, session_service=None, memory_service=None):
        self.cfg = cfg
        self.app_name = cfg.app_name
        if session_service is None or memory_service is None:
            session_service, memory_service = build_services(cfg)
        self.session_service = session_service
        self.memory_service = memory_service
        self.identity = IdentityResolver.from_source_systems()
        self.live = LiveContextStore(cfg.live_context_turns)
        callbacks.LIVE_CONTEXT = self.live
        self.agents = build_all_agents(model)
        # One Runner per channel; all share app_name + services => one unified memory scope.
        self.runners = {
            ch: Runner(agent=agent, app_name=self.app_name,
                       session_service=self.session_service, memory_service=self.memory_service)
            for ch, agent in self.agents.items()
        }

    @staticmethod
    def route(channel: Channel | str) -> Channel:
        return Channel(channel)  # raises ValueError for unknown channels

    async def _ensure_session(self, customer_id: str, channel: Channel, session_id: str | None):
        if session_id:
            s = await self.session_service.get_session(app_name=self.app_name, user_id=customer_id, session_id=session_id)
            if s:
                return s
        profile = load_profile(customer_id)
        return await self.session_service.create_session(
            app_name=self.app_name,
            user_id=customer_id,
            state={
                "customer_id": customer_id,
                "first_name": profile["first_name"],
                "channel": channel.value,
                "customer_profile": profile_summary(profile),
                "live_context": self.live.render(customer_id),
            },
        )

    async def handle(self, channel: Channel | str, identifier: str, text: str, session_id: str | None = None) -> GatewayReply:
        ch = self.route(channel)
        customer_id = self.identity.resolve(ch, identifier)
        session = await self._ensure_session(customer_id, ch, session_id)
        runner = self.runners[ch]

        reply, tool_calls = "", []
        async for event in runner.run_async(
            user_id=customer_id,
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=text)]),
            # refresh the real-time tier on every turn (other channels may have spoken since)
            state_delta={"live_context": self.live.render(customer_id), "channel": ch.value},
        ):
            for fc in event.get_function_calls() or []:
                tool_calls.append(fc.name)
            if event.is_final_response() and event.content and event.content.parts:
                reply = "".join(p.text or "" for p in event.content.parts).strip()

        first_name = session.state.get("first_name")
        violations = validate(ch, reply, first_name)
        if ch is Channel.VOICE and violations:
            log.info("voice guard applied: %s", violations)
            reply = enforce_voice(reply)
        return GatewayReply(customer_id, ch.value, session.id, self.agents[ch].name, reply, violations, tool_calls)
