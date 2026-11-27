"""Run the client servicing journeys (eval/client_use_cases.json) and check every expected result.

    python eval/run_client_use_cases.py            # offline, prints a pass/fail table
    pytest -q tests/test_client_journeys.py         # same journeys as tests
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cx_memory import clock  # noqa: E402

CASES = json.loads((ROOT / "eval" / "client_use_cases.json").read_text())["use_cases"]
MISSING = object()


def get_path(obj, path: str):
    for part in path.split("."):
        if isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return MISSING
        elif isinstance(obj, dict):
            if part not in obj:
                return MISSING
            obj = obj[part]
        else:
            return MISSING
    return obj


def check(out: dict, expect: dict) -> list[str]:
    errors = []
    for key, want in expect.items():
        if key.endswith("_contains"):
            got = get_path(out, key[: -len("_contains")])
            hay = json.dumps(got) if not isinstance(got, str) else got
            for needle in [want] if isinstance(want, str) else want:
                if got is MISSING or needle not in hay:
                    errors.append(f"{key}: '{needle}' not in {str(hay)[:160]!r}")
        else:
            got = get_path(out, key)
            if got != want:
                errors.append(f"{key}: expected {want!r}, got {got if got is not MISSING else '<missing>'!r}")
    return errors


async def run_use_case(gw, uc: dict) -> list[dict]:
    rows, session_id, contact_id = [], None, None
    for i, step in enumerate(uc["steps"], 1):
        do = step["do"]
        if do == "digital_start":
            out = await gw.digital.start(uc["account_id"])
            session_id = out["session_id"]
        elif do == "say":
            out = await gw.digital.message(session_id, step["text"])
        elif do == "agent_pickup":
            token = gw.digital.sessions[session_id].handoff_token
            address = step.get("from") or uc.get("phone") or ""
            out = await gw.crm.inbound_contact({"ContactId": f"{uc['id']}-{i}", "Channel": step["channel"],
                                                "CustomerEndpoint": {"Address": address}, "Attributes": {"handoff_token": token}})
            if out.get("screen_pop"):
                contact_id = out["contact_id"]
        elif do == "wrap_up":
            out = await gw.crm.wrap_up(contact_id, disposition="Resolved - fulfilled", notes="Specialist handled the request.",
                                       agent_id="specialist-12", fulfilment=step.get("fulfilment"))
        elif do == "advance_clock":
            clock.advance(seconds=step["minutes"] * 60)
            out = {}
        else:
            raise ValueError(do)
        errors = check(out, step.get("expect", {}))
        rows.append({"step": i, "do": do, "input": step.get("text", ""), "reply": out.get("text") or
                     (out.get("screen_pop") or {}).get("agent_greeting") or json.dumps(out.get("contact_attributes", ""))[:120],
                     "ok": not errors, "errors": errors})
    return rows


async def main() -> None:
    from google.adk.sessions import InMemorySessionService

    from cx_memory.config import Settings
    from cx_memory.gateway import OmnichannelGateway
    from cx_memory.local_memory import LocalMemoryService
    from cx_memory.profiles import _load
    from tests.fake_llm import FakeLlm

    total = failed = 0
    for uc in CASES:
        clock.reset()
        _load.cache_clear()
        gw = OmnichannelGateway(cfg=Settings(backend="local"), model=FakeLlm(),
                                session_service=InMemorySessionService(), memory_service=LocalMemoryService())
        await gw.seed_all()
        rows = await run_use_case(gw, uc)
        print(f"\n{uc['id']}  {uc['title']}")
        for r in rows:
            total += 1
            failed += not r["ok"]
            print(f"  {'OK ' if r['ok'] else '!! '}{r['step']:>2} {r['do']:<13} {r['input'][:48]:<48} -> {str(r['reply'])[:90]}")
            for e in r["errors"]:
                print(f"       {e}")
    clock.reset()
    print(f"\n{total - failed}/{total} steps passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
