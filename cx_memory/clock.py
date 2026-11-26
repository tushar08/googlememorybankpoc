"""Demo clock. Lifecycle rules (TTL, expiry, step-up expiry) read time from here.

The demo console can move the clock forward (POST /ops/clock/advance) to show memories expiring
without waiting days. Rate limiters and timeouts use real monotonic time.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

_offset_s = 0.0


def now() -> float:
    return time.time() + _offset_s


def utcnow() -> datetime:
    return datetime.fromtimestamp(now(), tz=timezone.utc)


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def advance(days: float = 0, seconds: float = 0) -> float:
    global _offset_s
    _offset_s += days * 86400 + seconds
    return _offset_s


def reset() -> None:
    global _offset_s
    _offset_s = 0.0


def offset_days() -> float:
    return round(_offset_s / 86400, 2)
