"""Merge the 5 synthetic source systems into one customer profile.

The same profile feeds both memory tiers:
  * Tier 1 (real-time session state): `profile_summary()` seeded into every new session.
  * Tier 2 (Memory Bank): `profile_facts()` written as durable facts by scripts/ingest_profiles.py.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .identity import DATA_DIR

SOURCE_SYSTEMS = ["crm", "core_banking", "cards", "servicing", "preferences"]


@lru_cache(maxsize=1)
def _load(data_dir: str = str(DATA_DIR)) -> dict:
    d = Path(data_dir)
    return {name: json.loads((d / f"{name}.json").read_text()) for name in SOURCE_SYSTEMS}


def load_profile(customer_id: str) -> dict:
    src = _load()
    crm = next((c for c in src["crm"]["customers"] if c["customer_id"] == customer_id), None)
    if crm is None:
        raise KeyError(customer_id)
    by_cid = lambda rows: [r for r in rows if r["customer_id"] == customer_id]  # noqa: E731
    prefs = by_cid(src["preferences"]["preferences"])
    return {
        "customer_id": customer_id,
        "first_name": crm["first_name"],
        "last_name": crm["last_name"],
        "segment": crm["segment"],
        "household_id": crm.get("household_id"),
        "customer_since": crm["since"],
        "accounts": by_cid(src["core_banking"]["accounts"]),
        "cards": by_cid(src["cards"]["cards"]),
        "cases": by_cid(src["servicing"]["cases"]),
        "preferences": prefs[0] if prefs else {},
        "sources": SOURCE_SYSTEMS,
    }


def apply_sor_update(ref_id: str, status: str, detail: str | None = None) -> str | None:
    """Apply a system-of-record status change (dispute or servicing case) to the cached SOR data.

    Returns the owning customer_id, or None if the reference is unknown. Stands in for the SOR itself;
    in production the gateway reads the SOR live and only the event reaches the memory layer.
    """
    src = _load()
    for card in src["cards"]["cards"]:
        d = card.get("open_dispute")
        if d and d["id"] == ref_id:
            d["status"] = status
            if detail:
                d["resolution"] = detail
            return card["customer_id"]
    for case in src["servicing"]["cases"]:
        if case["case_id"] == ref_id:
            case["status"] = status
            if detail:
                case["detail"] = detail
            return case["customer_id"]
    return None


def owner_of_ref(ref_id: str) -> str | None:
    src = _load()
    for card in src["cards"]["cards"]:
        if (card.get("open_dispute") or {}).get("id") == ref_id:
            return card["customer_id"]
    for case in src["servicing"]["cases"]:
        if case["case_id"] == ref_id:
            return case["customer_id"]
    return None


def seed_facts(p: dict) -> list[tuple[str, str]]:
    """(fact, topic) pairs for Memory Bank. The CRM name line is left out on purpose: names and
    identifiers come from the live profile, so the memory store holds as little PII as possible."""
    out = []
    for f in profile_facts(p):
        if "[source: CRM]" in f:
            continue
        if "[source: CoreBanking]" in f or ("[source: Cards]" in f and "dispute" not in f):
            topic = "product_holdings"
        elif "dispute" in f or "[source: Servicing]" in f:
            topic = "open_service_issues"
        elif "Hard of hearing" in f or "contact window" in f:
            topic = "contact_and_accessibility_needs"
        else:
            topic = "user_preferences"
        out.append((f, topic))
    return out


def all_customer_ids() -> list[str]:
    return [c["customer_id"] for c in _load()["crm"]["customers"]]


def profile_facts(p: dict) -> list[str]:
    """Atomic, first-person-free facts suitable for Memory Bank (one fact per memory)."""
    facts = [
        f"Customer's name is {p['first_name']} {p['last_name']}; segment {p['segment']}; customer since {p['customer_since']}. [source: CRM]",
    ]
    for a in p["accounts"]:
        facts.append(f"Holds a {a['type']} account ending {a['account_id'][-4:]} ({a['status']}). [source: CoreBanking]")
    for c in p["cards"]:
        line = f"Has {c['product']} ending {c['card_last4']}, status {c['status']}"
        if c.get("lock_reason"):
            line += f" ({c['lock_reason']})"
        if c.get("replacement"):
            line += f"; replacement card {c['replacement']}"
        facts.append(line + ". [source: Cards]")
        if d := c.get("open_dispute"):
            facts.append(
                f"Open card dispute {d['id']} for ${d['amount_usd']:.2f} at {d['merchant']}, opened {d['opened']}, status {d['status']}"
            + (f" ({d['resolution']})" if d.get("resolution") else "") + ". [source: Cards]"
            )
    for k in p["cases"]:
        facts.append(f"Servicing case {k['case_id']} ({k['topic']}) is {k['status']}: {k['detail']}. [source: Servicing]")
    pr = p["preferences"]
    if pr:
        facts.append(
            f"Prefers {pr.get('preferred_channel')} contact, language {pr.get('language')}, contact window {pr.get('contact_window')}. [source: Preferences]"
        )
        for key in ("accessibility", "notes"):
            if pr.get(key):
                facts.append(f"{pr[key]}. [source: Preferences]")
    return facts


def profile_summary(p: dict) -> str:
    """Compact text injected into the agent instruction via session state."""
    return "\n".join(f"- {f}" for f in profile_facts(p))
