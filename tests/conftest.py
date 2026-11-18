import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cx_memory.config import Settings  # noqa: E402
from cx_memory.gateway import OmnichannelGateway  # noqa: E402
from cx_memory.local_memory import LocalMemoryService, fact_entry  # noqa: E402
from cx_memory.profiles import load_profile, profile_facts  # noqa: E402
from tests.fake_llm import FakeLlm  # noqa: E402


@pytest.fixture
async def gw():
    FakeLlm.requests.clear()
    from google.adk.sessions import InMemorySessionService

    mem = LocalMemoryService()
    g = OmnichannelGateway(cfg=Settings(backend="local"), model=FakeLlm(),
                           session_service=InMemorySessionService(), memory_service=mem)
    for cid in ("CUST-1001", "CUST-1002", "CUST-1003"):
        await mem.add_memory(app_name=g.app_name, user_id=cid,
                             memories=[fact_entry(f) for f in profile_facts(load_profile(cid))])
    return g
