"""after_agent_callback: sync each completed turn to (1) the real-time tier and (2) Memory Bank.

Tier 2 goes through the GovernedMemoryService, whose extraction policy decides whether this turn is
extracted now, buffered, or skipped as small talk.
"""
from __future__ import annotations

import logging

from google.adk.agents.callback_context import CallbackContext

from .governed_memory import current_turn

log = logging.getLogger(__name__)


def _text(content) -> str:
    if not content or not content.parts:
        return ""
    return " ".join(p.text for p in content.parts if getattr(p, "text", None)).strip()


async def sync_turn_to_memory(callback_context: CallbackContext):
    state = callback_context.state
    ctx = current_turn()
    token = state.get("customer_token")
    channel = state.get("channel", "unknown")
    events = callback_context.session.events

    user_text = _text(callback_context.user_content)
    agent_text = ""
    for ev in reversed(events):
        if ev.author == callback_context.agent_name and _text(ev.content):
            agent_text = _text(ev.content)
            break

    # Tier 1: immediate cross-channel visibility
    if ctx is not None and ctx.live is not None and token:
        await ctx.live.append(token, channel, user_text, agent_text)

    # Tier 2: incremental extraction of this turn only (user message + agent reply)
    turn_events = [e for e in events if e.invocation_id == callback_context.invocation_id]
    try:
        await callback_context.add_events_to_memory(events=turn_events)
    except NotImplementedError:
        await callback_context.add_session_to_memory()
    except Exception as exc:  # memory sync must never fail the customer turn
        log.warning("memory sync failed for %s: %s", token, exc)

    state["turns_synced"] = int(state.get("turns_synced", 0)) + 1
    return None
