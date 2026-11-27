import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cx_memory import clock  # noqa: E402
from cx_memory.config import Settings  # noqa: E402
from cx_memory.gateway import OmnichannelGateway  # noqa: E402
from cx_memory.local_memory import LocalMemoryService  # noqa: E402
from cx_memory.profiles import _load  # noqa: E402
from tests.fake_llm import FakeLlm  # noqa: E402


@pytest.fixture
async def gw():
    FakeLlm.requests.clear()
    clock.reset()
    _load.cache_clear()  # SOR events mutate the cached source systems
    from google.adk.sessions import InMemorySessionService

    g = OmnichannelGateway(cfg=Settings(backend="local"), model=FakeLlm(),
                           session_service=InMemorySessionService(), memory_service=LocalMemoryService())
    await g.seed_all()
    yield g
    clock.reset()
    _load.cache_clear()
