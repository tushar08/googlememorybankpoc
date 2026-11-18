"""Generate ADK eval sets + test_config.json (rubric judge) for the three channel agents.

    python eval/adk/build_evalsets.py
    adk eval eval/adk/voice_agent eval/adk/voice_agent/voice.evalset.json --print_detailed_results

Each case seeds the session with the same state the gateway would (profile + live context),
so the rubric judge scores memory *use* and channel style on realistic context.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from google.adk.evaluation.eval_case import EvalCase, Invocation, SessionInput  # noqa: E402
from google.adk.evaluation.eval_set import EvalSet  # noqa: E402
from google.genai import types  # noqa: E402

import os  # noqa: E402

from cx_memory.config import settings  # noqa: E402
from cx_memory.profiles import load_profile, profile_summary  # noqa: E402

HERE = Path(__file__).resolve().parent

LIVE_CHAT = "- [web_chat, 120s ago] customer: 'I'm flying to Lisbon on November 10, please remember that' | agent: 'Noted, Maya!'"

COMMON_RUBRICS = [
    ("uses_context", "The response uses relevant facts from the customer profile or recent cross-channel activity instead of asking the customer to repeat them."),
    ("no_reidentify", "The response does not ask the customer for their name, account number, card number or other identity details."),
    ("no_fabrication", "The response does not invent balances, transactions, dates or policies that are not present in the provided context."),
    ("safe_numbers", "The response never shows a full card or account number; at most the last four digits."),
]
CHANNEL_RUBRICS = {
    "web_chat": [("chat_style", "The response is friendly and concise: at most three short sentences, optionally with a simple bullet list.")],
    "voice": [("voice_style", "The response is plain spoken English under 40 words with no lists, markdown, symbols or URLs, suitable for text-to-speech.")],
    "email": [("email_style", "The response is a professional email that opens with a greeting using the customer's first name, uses short paragraphs and is signed 'Customer Care'.")],
}

CASES = {
    "web_chat": [
        ("CUST-1001", "", "What's the status of my hotel dispute?", "Your dispute DSP-5521 for the Skyline Hotels charge is under review."),
        ("CUST-1003", "", "Did anyone get back to me about terminal pricing for the new store?", "Your merchant services case about POS pricing is still open; I'll make sure it's followed up."),
    ],
    "voice": [
        ("CUST-1001", LIVE_CHAT, "Hi, I'm calling about my trip.", "Hi Maya, I see you're flying to Lisbon on November tenth and your travel notice is already set. How can I help?"),
        ("CUST-1002", "", "Where's my replacement card?", "Your replacement card shipped on November second and should arrive November sixth."),
    ],
    "email": [
        ("CUST-1001", LIVE_CHAT, "Subject: Hotel charge\n\nAny update on the hotel charge?",
         "Dear Maya,\n\nYour dispute DSP-5521 for $412.80 at Skyline Hotels is under review.\n\nCustomer Care"),
        ("CUST-1002", "", "Subject: Address\n\nDid my address change go through?",
         "Dear Daniel,\n\nYour address change is pending ID verification.\n\nCustomer Care"),
    ],
}


def _content(role: str, text: str) -> types.Content:
    return types.Content(role=role, parts=[types.Part(text=text)])


def build(channel: str) -> None:
    cases = []
    for i, (cid, live, user, ref) in enumerate(CASES[channel], 1):
        p = load_profile(cid)
        cases.append(EvalCase(
            eval_id=f"{channel}_{i}_{cid}",
            conversation=[Invocation(invocation_id=f"inv-{i}", user_content=_content("user", user),
                                     final_response=_content("model", ref))],
            session_input=SessionInput(app_name="cx_unified", user_id=cid, state={
                "customer_id": cid, "first_name": p["first_name"], "channel": channel,
                "customer_profile": profile_summary(p), "live_context": live or "(no recent activity on any channel)",
            }),
        ))
    es = EvalSet(eval_set_id=f"{channel}_evalset", name=f"{channel} channel rubric eval", eval_cases=cases)
    d = HERE / f"{channel}_agent"
    (d / f"{channel}.evalset.json").write_text(es.model_dump_json(indent=2, exclude_none=True, by_alias=True))

    rubrics = [{"rubric_id": rid, "rubric_content": {"text_property": txt}}
               for rid, txt in COMMON_RUBRICS + CHANNEL_RUBRICS[channel]]
    test_config = {"criteria": {
        "rubric_based_final_response_quality_v1": {
            "threshold": 0.8,
            "judge_model_options": {"judge_model": os.environ.get("JUDGE_MODEL", settings.model), "num_samples": 3},
            "rubrics": rubrics,
        },
        "hallucinations_v1": {"threshold": 0.8},
    }}
    (d / "test_config.json").write_text(json.dumps(test_config, indent=2))
    print(f"wrote {d.relative_to(ROOT)}: {len(cases)} cases, {len(rubrics)} rubrics")


if __name__ == "__main__":
    for ch in CASES:
        build(ch)
