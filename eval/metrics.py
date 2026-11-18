"""Seven rule-based metrics for the storyline benchmark (deterministic, no LLM needed)."""
from __future__ import annotations

import re

from cx_memory.channels import Channel, validate

METRICS = [
    "identity_resolution_accuracy",  # 1 correct customer_id per step
    "routing_accuracy",              # 2 agent matches inbound channel
    "channel_style_compliance",      # 3 web/voice/email format rules
    "cross_channel_recall",          # 4 facts from another channel used without re-asking
    "memory_write_success",          # 5 save_customer_fact called when the customer asks to remember
    "no_repeat_ask",                 # 6 agent never asks customer to re-identify / repeat
    "privacy_isolation",             # 7 no other customer's data, no full card/account numbers
]

_REASK = re.compile(
    r"(what is|what's|can you (please )?(confirm|provide|tell me))\s+(your )?(name|account number|card number|"
    r"date of birth|phone number|email address)|verify your identity",
    re.IGNORECASE,
)
_FULL_NUMBER = re.compile(r"\b(?:\d[ -]?){12,19}\b")


def _contains_any(text: str, words: list[str]) -> bool:
    return any(w.lower() in text.lower() for w in words)


def score_step(step: dict, r: dict) -> dict[str, bool | None]:
    """r: {customer_id, agent, channel, text, tool_calls, first_name}"""
    text = r["text"]
    out: dict[str, bool | None] = {m: None for m in METRICS}
    out["identity_resolution_accuracy"] = r["customer_id"] == step["expect_customer"]
    out["routing_accuracy"] = r["agent"] == f"{r['channel']}_agent"
    out["channel_style_compliance"] = not validate(Channel(r["channel"]), text, r.get("first_name"))
    content_ok = True
    if step.get("expect_any"):
        content_ok &= _contains_any(text, step["expect_any"])
    if step.get("expect_all"):
        content_ok &= all(w.lower() in text.lower() for w in step["expect_all"])
    if step.get("cross_channel") or step.get("long_term") or step.get("concurrent"):
        out["cross_channel_recall"] = content_ok
    if step.get("expect_tool"):
        out["memory_write_success"] = step["expect_tool"] in r.get("tool_calls", [])
    out["no_repeat_ask"] = not _REASK.search(text)
    leak = _contains_any(text, step.get("must_not_contain", [])) or bool(_FULL_NUMBER.search(text))
    out["privacy_isolation"] = not leak
    out["_content_ok"] = content_ok
    return out


def aggregate(step_scores: list[dict]) -> dict[str, dict]:
    summary = {}
    for m in METRICS:
        vals = [s[m] for s in step_scores if s.get(m) is not None]
        summary[m] = {"passed": sum(vals), "total": len(vals), "rate": (sum(vals) / len(vals)) if vals else None}
    return summary
