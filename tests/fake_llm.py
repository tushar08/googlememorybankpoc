"""Deterministic fake model so the full ADK pipeline (tools, callbacks, memory) runs offline.

It behaves just enough like a real agent to make the offline demo meaningful:
  * "remember ...", "from now on ...", "my new number ..." -> calls save_customer_fact with a fitting topic;
  * reads the tool result back (saved / queued / rejected with step-up);
  * quotes the most relevant preloaded memory, so recall across channels is visible.
"""
from __future__ import annotations

import json
import re
from typing import AsyncGenerator, ClassVar

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

_STOP = set("a an the is are was to of for on in and or my me i you it do does what when where how can with your have has "
            "hi hello about any please this that am im i'm calling call there just".split())
_WRITE = re.compile(r"\b(remember (that )?|from now on|my new (phone |mobile )?(number|email|address)|update my (phone|number|email|address))(.*)",
                    re.IGNORECASE)


def _all_text(req: LlmRequest) -> str:
    parts = []
    si = req.config.system_instruction if req.config else None
    if si:
        parts.append(si if isinstance(si, str) else " ".join(p.text or "" for p in si.parts))
    for c in req.contents or []:
        for p in c.parts or []:
            if p.text:
                parts.append(p.text)
    return "\n".join(parts)


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP and len(t) > 2}


def _topic(text: str) -> str:
    t = text.lower()
    if re.search(r"call|contact|email me|text me|number|phone|address", t):
        return "contact_and_accessibility_needs"
    if re.search(r"dispute|charge|refund|case", t):
        return "open_service_issues"
    if re.search(r"fly|flying|trip|travel|moving|abroad", t):
        return "travel_and_life_events"
    return "user_preferences"


def _best_memory(prompt: str, query: str) -> str | None:
    m = re.search(r"<PAST_CONVERSATIONS>(.*?)</PAST_CONVERSATIONS>", prompt, re.DOTALL)
    if not m:
        return None
    q = _tokens(query)
    best, score = None, 0
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("Time:"):
            continue
        fact = re.sub(r"^[\w-]+:\s", "", line)
        s = len(q & _tokens(fact)) - (1.5 if fact.startswith("Customer said") else 0)  # prefer curated facts
        s += 0.25 if "[source:" in fact else 0  # and, on a tie, facts backed by a system of record
        if s > score:
            best, score = fact, s
    if not best:
        return None
    best = re.sub(r"\s*\[(source|channel):[^\]]*\]", "", best).strip().rstrip(".")
    return " ".join(best.split()[:18])


class FakeLlm(BaseLlm):
    model: str = "fake-llm"
    requests: ClassVar[list[str]] = []

    @classmethod
    def supported_models(cls) -> list[str]:
        return [r"fake-.*"]

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        prompt = _all_text(llm_request)
        FakeLlm.requests.append(prompt)
        contents = list(llm_request.contents or [])
        last_idx, last_text = -1, ""
        for i, c in enumerate(contents):
            txt = " ".join(p.text or "" for p in (c.parts or []) if p.text)
            if c.role == "user" and txt and "<PAST_CONVERSATIONS>" not in txt:
                last_idx, last_text = i, txt
        # only tool results produced in THIS turn (after the latest customer message)
        tool_results = [p.function_response.response for c in contents[last_idx + 1:] for p in (c.parts or []) if p.function_response]

        m = _WRITE.search(last_text)
        if m and not tool_results:
            fact = re.sub(r"^(that\s+)?", "", m.group(0)).strip().rstrip(".")
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(
                name="save_customer_fact", args={"fact": f"Customer says: {fact}", "topic": _topic(fact)}))]))
            return

        channel = "voice" if "VOICE call" in prompt else "email" if "by EMAIL" in prompt else "web_chat"
        name = re.search(r"Customer's name is (\w+)", prompt)
        first = name.group(1) if name else "there"
        ack = " I can see your earlier chat message." if "[web_chat" in prompt else ""
        note = ""
        if tool_results:
            r = tool_results[-1] or {}
            r = r if isinstance(r, dict) else json.loads(str(r))
            if r.get("status") == "saved":
                note = " I've noted that for every channel."
            elif r.get("status") == "queued":
                note = " I've noted that; it will sync shortly."
            elif r.get("needs_step_up"):
                note = " For your security I need to verify you with a passcode before I can change that."
            else:
                note = " I can't store that detail."
        else:
            mem = _best_memory(prompt, last_text)
            if mem:
                note = f" I have a note that: {mem}."
        if channel == "voice":
            text = f"Thanks {first}.{note or ' I have your details here.'}{ack}"
        elif channel == "email":
            text = f"Dear {first},\n\nThank you for your message.{note}{ack} We are looking into it.\n\nCustomer Care"
        else:
            text = f"Hi {first}!{note or ' Happy to help.'}{ack}"
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))
