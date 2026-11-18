"""Deterministic fake model so the full ADK pipeline (tools, callbacks, memory) runs offline."""
from __future__ import annotations

import re
from typing import AsyncGenerator, ClassVar

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types


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


class FakeLlm(BaseLlm):
    model: str = "fake-llm"
    requests: ClassVar[list[str]] = []

    @classmethod
    def supported_models(cls) -> list[str]:
        return [r"fake-.*"]

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        prompt = _all_text(llm_request)
        FakeLlm.requests.append(prompt)
        last = llm_request.contents[-1] if llm_request.contents else None
        last_text = " ".join(p.text or "" for p in (last.parts or [])) if last else ""
        has_tool_result = any(p.function_response for c in llm_request.contents for p in (c.parts or []))

        # "remember ..." -> call save_customer_fact once
        m = re.search(r"remember (that )?(.+)", last_text, re.IGNORECASE)
        if m and not has_tool_result:
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(
                name="save_customer_fact", args={"fact": f"Customer says {m.group(2).strip()}", "topic": "travel_and_life_events"}))]))
            return

        channel = "voice" if "VOICE call" in prompt else "email" if "by EMAIL" in prompt else "web_chat"
        name = re.search(r"Customer's name is (\w+)", prompt)
        first = name.group(1) if name else "there"
        knows_chat = "[web_chat" in prompt
        ack = " I can see your earlier chat message." if knows_chat else ""
        if channel == "voice":
            text = f"Thanks {first}, I have your details here.{ack} How can I help today?"
        elif channel == "email":
            text = f"Dear {first},\n\nThank you for your message.{ack} We are looking into it.\n\nCustomer Care"
        else:
            text = f"Hi {first}! Happy to help.{ack}"
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))
