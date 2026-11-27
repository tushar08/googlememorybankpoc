"""Client servicing journeys from eval/client_use_cases.json, one test per use case."""
import pytest

from eval.run_client_use_cases import CASES, run_use_case

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("uc", CASES, ids=[c["id"] for c in CASES])
async def test_client_journey(gw, uc):
    rows = await run_use_case(gw, uc)
    failures = [f"step {r['step']} {r['do']} {r['input']!r}: {r['errors']}" for r in rows if not r["ok"]]
    assert not failures, "\n".join(failures)
