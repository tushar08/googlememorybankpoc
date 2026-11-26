"""Partitions and quotas.

A partition is an isolated memory domain: its own Memory Bank scope (app_name; in production its own
Agent Engine instance and region), its own API quota budget and its own per-customer caps.
Why partition:
  * blast radius: a runaway consumer workload cannot exhaust the business-banking quota;
  * data residency / line-of-business separation: different instance, region and IAM per partition;
  * cost attribution: every meter is reported per partition.

Memory Bank enforces project-level request quotas per region (requests per minute for
generate / retrieve / create). The gateway keeps a client-side token bucket per partition and
operation below those limits, so it can queue work (outbox) instead of hitting 429s at peak.
"""
from __future__ import annotations

import os
import time
from collections import defaultdict
from dataclasses import dataclass, field

from . import clock


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass
class TokenBucket:
    per_minute: int
    burst: int
    tokens: float = 0.0
    updated: float = field(default_factory=time.monotonic)
    rejected: int = 0

    def __post_init__(self) -> None:
        self.tokens = float(self.burst)

    def _refill(self) -> None:
        t = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (t - self.updated) * self.per_minute / 60.0)
        self.updated = t

    def try_take(self, n: float = 1.0) -> bool:
        self._refill()
        if self.tokens >= n:
            self.tokens -= n
            return True
        self.rejected += 1
        return False

    def view(self) -> dict:
        self._refill()
        return {"per_minute": self.per_minute, "burst": self.burst, "available": round(self.tokens, 1), "rejected": self.rejected}


@dataclass
class Partition:
    name: str
    app_name: str
    region: str
    segments: tuple[str, ...]
    read_qpm: int
    write_qpm: int
    extract_qpm: int
    max_memories_per_customer: int
    daily_writes_per_customer: int
    buckets: dict[str, TokenBucket] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.buckets = {
            "read": TokenBucket(self.read_qpm, max(5, self.read_qpm // 6)),
            "write": TokenBucket(self.write_qpm, max(5, self.write_qpm // 6)),
            "extract": TokenBucket(self.extract_qpm, max(5, self.extract_qpm // 6)),
        }

    def view(self) -> dict:
        return {
            "name": self.name, "app_name": self.app_name, "region": self.region, "segments": list(self.segments),
            "max_memories_per_customer": self.max_memories_per_customer,
            "daily_writes_per_customer": self.daily_writes_per_customer,
            "quotas": {k: b.view() for k, b in self.buckets.items()},
        }


class PartitionRouter:
    def __init__(self, base_app_name: str = "cx_unified") -> None:
        self.partitions = {
            "consumer": Partition(
                "consumer", f"{base_app_name}_consumer", os.environ.get("CONSUMER_REGION", "us-central1"),
                ("Retail", "Private Client"),
                read_qpm=_int("CONSUMER_READ_QPM", 600), write_qpm=_int("CONSUMER_WRITE_QPM", 300),
                extract_qpm=_int("CONSUMER_EXTRACT_QPM", 120),
                max_memories_per_customer=_int("MAX_MEMORIES_PER_CUSTOMER", 200),
                daily_writes_per_customer=_int("DAILY_WRITES_PER_CUSTOMER", 50)),
            "business": Partition(
                "business", f"{base_app_name}_business", os.environ.get("BUSINESS_REGION", "us-central1"),
                ("Small Business", "Commercial"),
                read_qpm=_int("BUSINESS_READ_QPM", 300), write_qpm=_int("BUSINESS_WRITE_QPM", 150),
                extract_qpm=_int("BUSINESS_EXTRACT_QPM", 60),
                max_memories_per_customer=_int("MAX_MEMORIES_PER_CUSTOMER", 200),
                daily_writes_per_customer=_int("DAILY_WRITES_PER_CUSTOMER", 50)),
        }
        self._daily: dict[tuple[str, str], int] = defaultdict(int)

    def for_segment(self, segment: str) -> Partition:
        for p in self.partitions.values():
            if segment in p.segments:
                return p
        return self.partitions["consumer"]

    def by_app(self, app_name: str) -> Partition:
        for p in self.partitions.values():
            if p.app_name == app_name:
                return p
        return self.partitions["consumer"]

    def take_daily_write(self, partition: Partition, token: str) -> bool:
        key = (token, clock.utcnow().strftime("%Y-%m-%d"))
        if self._daily[key] >= partition.daily_writes_per_customer:
            return False
        self._daily[key] += 1
        return True

    def view(self) -> list[dict]:
        return [p.view() for p in self.partitions.values()]
