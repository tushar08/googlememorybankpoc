"""ADK eval entrypoint: exposes root_agent for `adk eval`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from cx_memory.agents import build_channel_agent  # noqa: E402
from cx_memory.channels import Channel  # noqa: E402

root_agent = build_channel_agent(Channel.EMAIL)
