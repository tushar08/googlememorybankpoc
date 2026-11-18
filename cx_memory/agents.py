"""Three ADK LlmAgent channel agents sharing one memory scope (app_name=cx_unified, user_id=customer_id)."""
from __future__ import annotations

from google.adk.agents import LlmAgent

from .callbacks import sync_turn_to_memory
from .channels import STYLE_RULES, Channel
from .config import settings
from .tools import preload_memory, save_customer_fact

BASE_INSTRUCTION = """You are a customer-care assistant for a retail bank (synthetic proof-of-concept data only).
The customer has already been identified by the channel; do not ask them to repeat who they are.

GOAL: the customer must never have to repeat information they gave on another channel.
- Use the CUSTOMER PROFILE (merged from 5 source systems) and the RECENT CROSS-CHANNEL ACTIVITY below.
- Long-term memories from earlier conversations are added automatically to the conversation when relevant.
- If something the customer said on another channel is relevant, acknowledge it naturally
  (e.g. "I can see you asked about this in chat a few minutes ago").
- When the customer tells you a new durable fact (contact change, travel, preference, issue status),
  call save_customer_fact once with a concise third-person fact and the best topic.
- Never reveal full account or card numbers; refer to the last four digits only.
- Never invent balances, transactions or policy. If you do not know, say you will check.

{channel_style}

CUSTOMER PROFILE:
{customer_profile}

RECENT CROSS-CHANNEL ACTIVITY (real-time tier):
{live_context?}
"""


def _instruction(channel: Channel) -> str:
    # {customer_profile} and {live_context} are filled by ADK from session state at runtime.
    return BASE_INSTRUCTION.replace("{channel_style}", STYLE_RULES[channel])


def build_channel_agent(channel: Channel, model=None) -> LlmAgent:
    return LlmAgent(
        name=f"{channel.value}_agent",
        model=model or settings.model,
        description=f"Customer-care agent for the {channel.value} channel.",
        instruction=_instruction(channel),
        tools=[preload_memory, save_customer_fact],
        after_agent_callback=sync_turn_to_memory,
    )


def build_all_agents(model=None) -> dict[Channel, LlmAgent]:
    return {ch: build_channel_agent(ch, model) for ch in Channel}
