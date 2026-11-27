"""Extraction cost and throughput.

Memory extraction is an LLM call made by Memory Bank: it reads the new conversation events plus the
topic definitions, few-shot examples and the customer's existing related memories (for consolidation),
and writes or updates facts. It is the most expensive memory operation and the one with the tightest quota.
So *when* to extract is a design decision:

  per_turn : extract after every turn            -> freshest, most calls
  salient  : extract only turns with memory-worthy content (dates, money, references, instructions,
             contact/travel/issue words); skip small talk     -> default
  batched  : buffer the whole contact, extract once at session end if any turn was salient
             (safety flush every N turns for long sessions)          -> fewest calls, most lag

Tier 1 (live context) covers the lag, so batched/salient lose nothing in the cross-channel handoff.

All unit prices are ILLUSTRATIVE placeholders. Set PRICE_* from your own rate card before quoting numbers.
"""
from __future__ import annotations

import math
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Prices:
    model_input_per_m: float = field(default_factory=lambda: _f("PRICE_MODEL_INPUT_PER_M", 0.30))
    model_output_per_m: float = field(default_factory=lambda: _f("PRICE_MODEL_OUTPUT_PER_M", 2.50))
    mb_generate_per_1k: float = field(default_factory=lambda: _f("PRICE_MB_GENERATE_PER_1K", 0.25))
    mb_retrieve_per_1k: float = field(default_factory=lambda: _f("PRICE_MB_RETRIEVE_PER_1K", 0.25))
    mb_stored_per_1k_month: float = field(default_factory=lambda: _f("PRICE_MB_STORED_PER_1K_MONTH", 0.25))
    note: str = "ILLUSTRATIVE rates: replace PRICE_* env vars with your contracted rate card"


EXTRACTION_OVERHEAD_TOKENS = int(os.environ.get("EXTRACTION_OVERHEAD_TOKENS", 1800))  # topics + examples + existing memories
EXTRACTION_OUTPUT_TOKENS = int(os.environ.get("EXTRACTION_OUTPUT_TOKENS", 150))


def est_tokens(text: str) -> int:
    return max(1, math.ceil(len(text or "") / 4))


SALIENT = re.compile(
    r"\b(remember|from now on|always|never|prefer|don'?t|call me|email me|text me|contact|"
    r"travel|trip|fly|flying|abroad|move|moving|relocat\w*|address|phone|number|"
    r"dispute|charge|fraud|lost|stolen|declined|replace\w*|refund|complain\w*|close|open|"
    r"loan|mortgage|business|store|language|spanish|hearing|"
    r"january|february|march|april|may|june|july|august|september|october|november|december)\b"
    r"|\$\s?\d|\b\d{1,2}(:\d\d)?\s?(am|pm)\b|\b(DSP|CASE)-\d+\b|\b20\d\d-\d\d-\d\d\b",
    re.IGNORECASE,
)


def is_salient(text: str) -> bool:
    return bool(SALIENT.search(text or ""))


@dataclass
class ExtractionPolicy:
    mode: str = field(default_factory=lambda: os.environ.get("EXTRACTION_POLICY", "salient"))
    batch_turns: int = field(default_factory=lambda: int(os.environ.get("EXTRACTION_BATCH_TURNS", 6)))
    _buffers: dict = field(default_factory=lambda: defaultdict(list))
    _salient: set = field(default_factory=set)

    MODES = ("per_turn", "salient", "batched")

    def offer(self, key: tuple, user_text: str, events: list) -> tuple[list | None, str]:
        """Returns (events to extract now or None, decision)."""
        if self.mode == "per_turn":
            return events, "extract: per_turn"
        if self.mode == "salient":
            return (events, "extract: salient turn") if is_salient(user_text) else (None, "skip: small talk")
        buf = self._buffers[key]
        buf.extend(events)
        if is_salient(user_text):
            self._salient.add(key)
        if len(buf) >= self.batch_turns * 2 and key in self._salient:
            out = list(buf)
            buf.clear()
            self._salient.discard(key)
            return out, f"extract: safety flush of {len(out)} events"
        return None, f"buffered ({len(buf)} events; extract at session end)"

    def flush(self, key: tuple) -> list:
        """Session end: extract the buffer only if something in it was worth remembering."""
        buf = self._buffers.pop(key, [])
        salient = key in self._salient
        self._salient.discard(key)
        return buf if salient else []

    def pending(self) -> int:
        return sum(len(b) for b in self._buffers.values())


class CostMeter:
    FIELDS = ("turns", "model_calls", "model_input_tokens", "model_output_tokens", "retrieve_calls",
              "extraction_calls", "extraction_input_tokens", "extraction_skipped", "explicit_writes",
              "writes_blocked", "throttled", "degraded_reads", "outboxed_writes")

    def __init__(self, prices: Prices | None = None) -> None:
        self.prices = prices or Prices()
        self.by_partition: dict[str, dict[str, int]] = defaultdict(lambda: {f: 0 for f in self.FIELDS})

    def add(self, partition: str, **kw: int) -> None:
        row = self.by_partition[partition]
        for k, v in kw.items():
            row[k] += v

    def estimate(self, row: dict) -> dict:
        p = self.prices
        model = row["model_input_tokens"] / 1e6 * p.model_input_per_m + row["model_output_tokens"] / 1e6 * p.model_output_per_m
        extraction_llm = (row["extraction_input_tokens"] / 1e6 * p.model_input_per_m
                          + row["extraction_calls"] * EXTRACTION_OUTPUT_TOKENS / 1e6 * p.model_output_per_m)
        mb_fees = (row["extraction_calls"] + row["explicit_writes"]) / 1000 * p.mb_generate_per_1k + row["retrieve_calls"] / 1000 * p.mb_retrieve_per_1k
        total = model + extraction_llm + mb_fees
        return {"agent_model_usd": round(model, 6), "extraction_usd": round(extraction_llm, 6),
                "memory_bank_fees_usd": round(mb_fees, 6), "total_usd": round(total, 6),
                "per_turn_usd": round(total / row["turns"], 6) if row["turns"] else None}

    def snapshot(self) -> dict:
        out = {}
        for part, row in self.by_partition.items():
            out[part] = {**row, "cost": self.estimate(row)}
        return {"partitions": out, "prices": self.prices.__dict__}


def capacity_plan(*, contacts_per_month: int, turns_per_contact: float, salient_share: float, mode: str,
                  batch_turns: int, avg_turn_tokens: int, peak_factor: float, extract_qpm_quota: int,
                  read_qpm_quota: int, memories_per_customer: int, customers: int, prices: Prices | None = None) -> dict:
    """Back-of-envelope monthly projection for one partition."""
    p = prices or Prices()
    turns = contacts_per_month * turns_per_contact
    if mode == "per_turn":
        extractions = turns
    elif mode == "salient":
        extractions = turns * salient_share
    else:  # one extraction per contact that had anything salient (+ safety flushes on very long contacts)
        p_any = 1 - (1 - salient_share) ** turns_per_contact
        extractions = contacts_per_month * p_any * math.ceil(turns_per_contact / max(1, batch_turns))
    retrieves = turns  # preload_memory: one similarity search per model call (approx one per turn)
    extraction_tokens = extractions * (EXTRACTION_OVERHEAD_TOKENS + avg_turn_tokens * (min(turns_per_contact, batch_turns) if mode == "batched" else 1))
    model_in = turns * (avg_turn_tokens + 2200)  # instruction + profile + live context + preloaded memories
    model_out = turns * 80
    row = {"turns": turns, "model_input_tokens": model_in, "model_output_tokens": model_out, "retrieve_calls": retrieves,
           "extraction_calls": extractions, "extraction_input_tokens": extraction_tokens, "explicit_writes": turns * 0.05}
    cost = CostMeter(p).estimate(row)
    storage = customers * memories_per_customer / 1000 * p.mb_stored_per_1k_month
    seconds = 30 * 24 * 3600
    peak_extract_qpm = extractions / seconds * 60 * peak_factor
    peak_read_qpm = retrieves / seconds * 60 * peak_factor
    return {
        "inputs": {"contacts_per_month": contacts_per_month, "turns_per_contact": turns_per_contact, "mode": mode,
                   "salient_share": salient_share, "batch_turns": batch_turns, "peak_factor": peak_factor},
        "monthly": {"turns": round(turns), "extraction_calls": round(extractions), "retrieve_calls": round(retrieves),
                    "extraction_input_tokens": round(extraction_tokens)},
        "cost_usd": {**cost, "storage_usd": round(storage, 2), "total_with_storage_usd": round(cost["total_usd"] + storage, 2),
                     "per_contact_usd": round((cost["total_usd"] + storage) / contacts_per_month, 5) if contacts_per_month else None},
        "throughput": {"peak_extract_qpm": round(peak_extract_qpm, 1), "extract_qpm_quota": extract_qpm_quota,
                       "extract_headroom": round(extract_qpm_quota / peak_extract_qpm, 2) if peak_extract_qpm else None,
                       "peak_read_qpm": round(peak_read_qpm, 1), "read_qpm_quota": read_qpm_quota,
                       "read_headroom": round(read_qpm_quota / peak_read_qpm, 2) if peak_read_qpm else None,
                       "needs_quota_increase": peak_extract_qpm > extract_qpm_quota or peak_read_qpm > read_qpm_quota},
        "note": p.note,
    }
