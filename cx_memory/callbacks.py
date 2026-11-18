"""after_agent_callback: sync each completed turn to (1) the real-time tier and (2) Memory Bank."""
from __future__ import annotations

import logging

from google.adk.agents.callback_context import CallbackContext

log = logging.getLogger(__name__)

# Set by the gateway at startup (avoids a global import cycle).
LIVE_CONTEXT = None


def _text(content) -> str:
    if not content or not content.parts:
        return ""
    return " ".join(p.text for p in content.parts if getattr(p, "text", None)).strip()


async def sync_turn_to_memory(callback_context: CallbackContext):
    state = callback_context.state
    customer_id = state.get("customer_id")
    channel = state.get("channel", "unknown")
    events = callback_context.session.events

    user_text = _text(callback_context.user_content)
    agent_text = ""
    for ev in reversed(events):
        if ev.author == callback_context.agent_name and _text(ev.content):
            agent_text = _text(ev.content)
            break

    # Tier 1 — immediate cross-channel visibility
    if LIVE_CONTEXT is not None and customer_id:
        await LIVE_CONTEXT.append(customer_id, channel, user_text, agent_text)

    # Tier 2 — incremental extraction: only this turn's events (user msg + agent reply)
    turn_events = [e for e in events if e.invocation_id == callback_context.invocation_id]
    try:
        await callback_context.add_events_to_memory(events=turn_events)
    except NotImplementedError:
        await callback_context.add_session_to_memory()
    except Exception as exc:  # memory sync must never fail the customer turn
        log.warning("memory sync failed for %s: %s", customer_id, exc)

    state["turns_synced"] = int(state.get("turns_synced", 0)) + 1
    return None
