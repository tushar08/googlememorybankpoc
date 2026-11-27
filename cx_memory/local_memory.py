"""Offline stand-in for Agent Platform Memory Bank (used by tests and BACKEND=local).

Implements the ADK BaseMemoryService surface: search_memory, add_memory, add_events_to_memory,
add_session_to_memory, delete_memory (+ purge for erasure).
Retrieval is simple keyword overlap; Memory Bank does semantic similarity search.
Consolidation, supersession and TTL live in the GovernedMemoryService in front of this class,
so they behave the same on both backends.
"""
from __future__ import annotations

import re
import time
import uuid
from collections import defaultdict
from typing import Mapping, Sequence

from google.adk.memory.base_memory_service import BaseMemoryService, SearchMemoryResponse
from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types

_STOP = set("a an the is are was to of for on in and or my me i you it do does what when where how can with your have has".split())


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP and len(t) > 1}


def _text(entry: MemoryEntry) -> str:
    return " ".join(p.text or "" for p in (entry.content.parts or []))


class LocalMemoryService(BaseMemoryService):
    def __init__(self) -> None:
        self._store: dict[tuple[str, str], list[MemoryEntry]] = defaultdict(list)

    # ---- write --------------------------------------------------------------
    async def add_memory(self, *, app_name: str, user_id: str, memories: Sequence[MemoryEntry],
                         custom_metadata: Mapping[str, object] | None = None) -> None:
        bucket = self._store[(app_name, user_id)]
        meta = dict(custom_metadata or {})
        for m in memories:
            mid = meta.get("memory_id") or m.id or "m-" + uuid.uuid4().hex[:20]
            if any(e.id == mid for e in bucket):
                continue  # idempotent replay from the outbox
            bucket.append(m.model_copy(update={"id": mid, "custom_metadata": {**(m.custom_metadata or {}), **meta}}))

    async def add_events_to_memory(self, *, app_name: str, user_id: str, events, session_id=None,
                                   custom_metadata=None) -> None:
        entries = []
        for ev in events:
            if ev.author == "user" and ev.content and ev.content.parts:
                txt = " ".join(p.text or "" for p in ev.content.parts).strip()
                if txt:
                    entries.append(MemoryEntry(content=types.Content(role="user", parts=[types.Part(text=f"Customer said: {txt}")]),
                                               author="user",
                                               timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ev.timestamp))))
        if entries:
            await self.add_memory(app_name=app_name, user_id=user_id, memories=entries)

    async def add_session_to_memory(self, session) -> None:
        await self.add_events_to_memory(app_name=session.app_name, user_id=session.user_id, events=session.events)

    async def delete_memory(self, *, app_name: str, user_id: str, memory_id: str | None = None) -> None:
        bucket = self._store.get((app_name, user_id), [])
        bucket[:] = [e for e in bucket if e.id != memory_id]

    def purge(self, app_name: str, user_id: str) -> None:
        self._store.pop((app_name, user_id), None)

    # ---- read ---------------------------------------------------------------
    async def search_memory(self, *, app_name: str, user_id: str, query: str) -> SearchMemoryResponse:
        q = _tokens(query)
        scored = []
        for e in self._store.get((app_name, user_id), []):
            score = len(q & _tokens(_text(e)))
            if score:
                scored.append((score, e))
        scored.sort(key=lambda x: -x[0])
        return SearchMemoryResponse(memories=[e for _, e in scored[:8]])

    # ---- test helper --------------------------------------------------------
    def all_facts(self, app_name: str, user_id: str) -> list[str]:
        return [_text(e) for e in self._store.get((app_name, user_id), [])]


def fact_entry(fact: str, topic: str | None = None) -> MemoryEntry:
    return MemoryEntry(
        content=types.Content(role="user", parts=[types.Part(text=fact)]),
        author="system",
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        custom_metadata={"topic": topic} if topic else {},
    )
