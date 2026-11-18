"""Agent tools.

* PreloadMemoryTool (ADK built-in): similarity search on Memory Bank before every model call.
* save_customer_fact: lets the agent write an explicit, consolidated fact mid-conversation
  (e.g. "my new number is ..."), so it is available on other channels without waiting for
  end-of-session extraction.
"""
from __future__ import annotations

import logging
import re

from google.adk.tools import ToolContext
from google.adk.tools.preload_memory_tool import PreloadMemoryTool

from .local_memory import fact_entry
from .memory_topics import ALL_TOPIC_LABELS

log = logging.getLogger(__name__)

_PAN = re.compile(r"\b(?:\d[ -]?){13,19}\b")  # full card / account numbers
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def _redact(text: str) -> str:
    text = _PAN.sub("[REDACTED-NUMBER]", text)
    return _SSN.sub("[REDACTED-SSN]", text)


async def save_customer_fact(fact: str, topic: str, tool_context: ToolContext) -> dict:
    """Save one durable fact about the customer to long-term memory so every channel can use it.

    Use this when the customer states something that should be remembered across channels,
    e.g. a new phone number, an upcoming trip, a contact preference, or the status of an issue.
    Do NOT save small talk, and never save full card numbers, account numbers or SSNs.

    Args:
        fact: A single, self-contained fact written in third person,
            e.g. "Customer will travel to Lisbon from 10 to 20 November 2026."
        topic: One of: user_personal_info, user_preferences, key_conversation_details,
            explicit_instructions, open_service_issues, product_holdings,
            travel_and_life_events, contact_and_accessibility_needs.

    Returns:
        A dict with status "saved" or "rejected" and a reason.
    """
    topic = (topic or "").strip().lower()
    if topic not in ALL_TOPIC_LABELS:
        return {"status": "rejected", "reason": f"unknown topic '{topic}'", "allowed": ALL_TOPIC_LABELS}
    fact = _redact((fact or "").strip())
    if len(fact) < 8:
        return {"status": "rejected", "reason": "fact too short"}

    channel = tool_context.state.get("channel", "unknown")
    entry = fact_entry(f"{fact} [channel: {channel}]", topic)
    try:
        # enable_consolidation -> Memory Bank merges/updates instead of duplicating
        await tool_context.add_memory(memories=[entry], custom_metadata={"enable_consolidation": True, "topic": topic})
    except Exception as exc:  # never break the conversation because memory write failed
        log.warning("save_customer_fact failed: %s", exc)
        return {"status": "error", "reason": str(exc)}

    saved = list(tool_context.state.get("facts_saved_this_session", []))
    saved.append({"topic": topic, "fact": fact})
    tool_context.state["facts_saved_this_session"] = saved
    return {"status": "saved", "topic": topic, "fact": fact}


preload_memory = PreloadMemoryTool()
