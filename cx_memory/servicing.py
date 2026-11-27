"""Synthetic servicing systems behind the client journeys.

  * Recommendation engine stub: predicted servicing intent + next-best (personalization) action.
  * Transactions: fee and statement search.
  * Fulfilment: card payment, fee waiver with channel eligibility rules.
  * Workforce: skill-based queues and specialist availability for proficiency routing.
  * Knowledge base: relevant content per reason code for the specialist.

Each OmnichannelGateway gets its own copy, so actions in one demo/test run don't leak into another.
"""
from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import clock

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "source_systems"
COURTESY_WINDOW_DAYS = 365
DIGITAL_WAIVER_LIMIT_USD = 35.0
DIGITAL_WAIVABLE = {"MONTHLY_SERVICE_FEE"}           # deposit-account fees: assistant may waive
SPECIALIST_WAIVABLE = {"LATE_FEE", "WIRE_FEE"}        # card / wire fees: specialist approval
NOT_WAIVABLE = {"FX_FEE"}                             # disclosed fees: policy says no


def _read(name: str) -> dict:
    return json.loads((DATA_DIR / f"{name}.json").read_text())


class ServicingSystems:
    def __init__(self) -> None:
        self.txns = copy.deepcopy(_read("transactions"))
        self.insights = copy.deepcopy(_read("insights"))
        self.queues = copy.deepcopy(_read("specialists"))["queues"]
        self.kb = _read("knowledge")["articles"]
        self.waivers: list[dict] = []
        self.payments: list[dict] = []
        self.callbacks: list[dict] = []
        for st in self.txns["statements"]:  # due dates are relative so the journey works on any demo day
            st.setdefault("due_date", (clock.utcnow() + timedelta(days=st.get("due_in_days", 2))).strftime("%Y-%m-%d"))

    # ------------------------------------------------------------- recommendation engine
    def predicted_intent(self, customer_id: str) -> dict | None:
        row = next((r for r in self.insights["insights"] if r["customer_id"] == customer_id), None)
        return row if row and row.get("predicted_intent") and row["confidence"] >= 0.6 else None

    def next_best_action(self, customer_id: str) -> dict | None:
        return next((r for r in self.insights["next_best_actions"] if r["customer_id"] == customer_id), None)

    # ------------------------------------------------------------- transactions
    def statement(self, customer_id: str) -> dict | None:
        return next((s for s in self.txns["statements"] if s["customer_id"] == customer_id), None)

    def search(self, customer_id: str, *, kind: str | None = None, statement: str | None = None,
               account_hint: str | None = None) -> list[dict]:
        rows = [t for t in self.txns["transactions"] if t["customer_id"] == customer_id]
        if kind:
            rows = [t for t in rows if t["type"] == kind]
        if statement:
            rows = [t for t in rows if t.get("statement") == statement or t["date"].startswith(statement)]
        if account_hint:
            rows = [t for t in rows if account_hint.lower() in t["account"].lower()
                    or (account_hint.lower() == "savings" and t["account"].startswith("ACC-"))
                    or (account_hint.lower() == "card" and t["account"].startswith("card-"))]
        return [{**t, "waived": any(w["txn_id"] == t["txn_id"] for w in self.waivers)} for t in rows]

    def txn(self, txn_id: str) -> dict | None:
        return next((t for t in self.txns["transactions"] if t["txn_id"] == txn_id), None)

    # ------------------------------------------------------------- fulfilment
    def make_payment(self, customer_id: str, amount: float, from_account: str) -> dict:
        st = self.statement(customer_id)
        ref = f"PAY-{uuid.uuid4().hex[:6].upper()}"
        rec = {"ref": ref, "customer_id": customer_id, "amount_usd": round(amount, 2), "from": from_account,
               "to": st["account"] if st else "card", "posted": clock.utcnow().strftime("%Y-%m-%d")}
        self.payments.append(rec)
        if st:
            st["statement_balance_usd"] = round(st["statement_balance_usd"] - amount, 2)
            st["minimum_due_usd"] = max(0.0, round(st["minimum_due_usd"] - amount, 2))
        return rec

    def waiver_eligibility(self, customer_id: str, txn_id: str, channel: str) -> dict:
        """channel: 'digital' (assistant) or 'specialist'."""
        t = self.txn(txn_id)
        if t is None or t["customer_id"] != customer_id or t["type"] != "fee":
            return {"eligible": False, "reason": "no such fee on this customer's accounts"}
        if any(w["txn_id"] == txn_id for w in self.waivers):
            return {"eligible": False, "reason": "this fee was already waived"}
        if t["fee_code"] in NOT_WAIVABLE:
            return {"eligible": False, "reason": "disclosed fee, not waivable under policy (KB-1042)"}
        cutoff = (clock.utcnow() - timedelta(days=COURTESY_WINDOW_DAYS)).strftime("%Y-%m-%d")
        if any(w["customer_id"] == customer_id and w["date"] >= cutoff and w["courtesy"] for w in self.waivers):
            return {"eligible": False, "reason": "one courtesy waiver per 12 months already used"}
        if channel == "digital":
            if t["fee_code"] in DIGITAL_WAIVABLE and t["amount_usd"] <= DIGITAL_WAIVER_LIMIT_USD:
                return {"eligible": True, "channel": "digital", "fee": t}
            return {"eligible": False, "needs_specialist": True, "fee": t,
                    "reason": f"{t['description'].lower()} needs specialist approval"}
        if t["fee_code"] in DIGITAL_WAIVABLE | SPECIALIST_WAIVABLE:
            return {"eligible": True, "channel": "specialist", "fee": t}
        return {"eligible": False, "reason": "fee type not waivable"}

    def waive_fee(self, customer_id: str, txn_id: str, channel: str, approver: str) -> dict:
        e = self.waiver_eligibility(customer_id, txn_id, channel)
        if not e["eligible"]:
            return {"status": "not_waived", **e}
        t = e["fee"]
        rec = {"ref": f"WVR-{uuid.uuid4().hex[:6].upper()}", "customer_id": customer_id, "txn_id": txn_id,
               "amount_usd": t["amount_usd"], "description": t["description"], "channel": channel, "approver": approver,
               "courtesy": True, "date": clock.utcnow().strftime("%Y-%m-%d")}
        self.waivers.append(rec)
        return {"status": "waived", **rec}

    # ------------------------------------------------------------- workforce / routing
    def route(self, reason_code: str, channel: str) -> dict:
        """Proficiency-based routing: the queue whose skills cover the reason code, on that channel, with
        someone available. Falls back to general servicing, and offers a call back if nobody is free."""
        skilled = [q for q in self.queues if reason_code in q["skills"] and channel in q["channels"]]
        open_q = [q for q in skilled if q["agents_available"] > 0]
        if open_q:
            q = min(open_q, key=lambda q: q["avg_wait_s"])
            return {"queue": q["queue"], "skill": reason_code, "available": True, "est_wait_s": q["avg_wait_s"], "match": "skill"}
        gen = next(q for q in self.queues if q["queue"] == "General-Servicing")
        return {"queue": gen["queue"], "skill": reason_code, "available": gen["agents_available"] > 0,
                "est_wait_s": gen["avg_wait_s"], "match": "fallback (no skilled agent free)", "offer_callback": True}

    def knowledge(self, reason_code: str) -> list[dict]:
        return [a for a in self.kb if a["reason_code"] == reason_code]

    def schedule_callback(self, customer_id: str, reason_code: str, window: str) -> dict:
        slot = clock.utcnow() + timedelta(hours=1)
        if "after 6pm" in (window or ""):
            slot = slot.replace(hour=23, minute=0)  # 6pm ET in UTC (synthetic: ignores DST)
        rec = {"ref": f"CB-{uuid.uuid4().hex[:6].upper()}", "customer_id": customer_id, "reason_code": reason_code,
               "slot_utc": slot.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "window": window or "next available"}
        self.callbacks.append(rec)
        return rec


def due_in_days(due_date: str) -> int:
    d = datetime.strptime(due_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (d.date() - clock.utcnow().date()).days
