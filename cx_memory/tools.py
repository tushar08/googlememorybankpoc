"""Agent tools.

* PreloadMemoryTool (ADK built-in): similarity search on Memory Bank before every model call.
  It goes through the GovernedMemoryService, so lifecycle filtering, quotas and audit apply.
* save_customer_fact: lets the agent write an explicit fact mid-conversation, so it is available on
  other channels without waiting for extraction. The governance layer decides whether it is allowed.
"""
from __future__ import annotations

import logging

from google.adk.tools import ToolContext
from google.adk.tools.preload_memory_tool import PreloadMemoryTool

from .governance import redact
from .governed_memory import current_turn
from .memory_topics import ALL_TOPIC_LABELS

log = logging.getLogger(__name__)


def _redact(text: str) -> str:
    return redact(text)[0]


async def save_customer_fact(fact: str, topic: str, tool_context: ToolContext) -> dict:
    """Save one durable fact about the customer to long-term memory so every channel can use it.

    Use this when the customer states something that should be remembered across channels,
    e.g. an upcoming trip, a contact preference, or the status of an issue.
    Do NOT save small talk, and never save full card numbers, account numbers or SSNs.
    If the result says needs_step_up, tell the customer you need to verify them before making that change.

    Args:
        fact: A single, self-contained fact written in third person,
            e.g. "Customer will travel to Lisbon from 10 to 20 November 2026."
        topic: One of: user_personal_info, user_preferences, key_conversation_details,
            explicit_instructions, open_service_issues, product_holdings,
            travel_and_life_events, contact_and_accessibility_needs.

    Returns:
        A dict with status "saved", "queued" (memory temporarily unavailable, will be written) or
        "rejected", and a reason.
    """
    topic = (topic or "").strip().lower()
    if topic not in ALL_TOPIC_LABELS:
        return {"status": "rejected", "reason": f"unknown topic '{topic}'", "allowed": ALL_TOPIC_LABELS}
    ctx = current_turn()
    if ctx is None or ctx.memory is None:
        return {"status": "error", "reason": "no governed memory context for this turn"}
    fact = (fact or "").strip()
    try:
        result = await ctx.memory.write_fact(app_name=ctx.app_name, user_id=ctx.customer_token, text=fact,
                                             topic=topic, source_kind="agent_tool", ctx=ctx)
    except Exception as exc:  # never break the conversation because a memory write failed
        log.warning("save_customer_fact failed: %s", exc)
        return {"status": "error", "reason": str(exc)}
    if result["status"] in ("saved", "queued"):
        saved = list(tool_context.state.get("facts_saved_this_session", []))
        saved.append({"topic": topic, "fact": result["fact"]})
        tool_context.state["facts_saved_this_session"] = saved
    return result


preload_memory = PreloadMemoryTool()
