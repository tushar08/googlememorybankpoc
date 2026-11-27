"""Scenario tests: the realistic cases behind the demo console (offline, no GCP).

    pytest -q tests/test_scenarios.py
"""
import json

import pytest

from cx_memory import clock
from cx_memory.cost import capacity_plan
from cx_memory.governance import redact
from cx_memory.loadgen import run_burst
from tests.fake_llm import FakeLlm

pytestmark = pytest.mark.asyncio
MAYA, DANIEL, LUCIA, ARJUN = "CUST-1001", "CUST-1002", "CUST-1003", "CUST-1004"


def active_texts(gw, cid):
    return [r.text for r in gw.memory_service.ledger.active(gw.scope(cid)[1])]


# ---------------- 1. Customer calls from a different phone ---------------------

async def test_s01_unknown_number_gets_step_up_without_model_or_memory(gw):
    n = len(FakeLlm.requests)
    r = await gw.handle("voice", "+1 (917) 555-0199", "Hi, calling about my trip")
    assert r.agent == "identity_guard" and r.step_up["required"] and r.customer_id is None
    assert len(FakeLlm.requests) == n  # no LLM call, no profile, no memory read


async def test_s02_step_up_links_new_number_and_unlocks_memory(gw):
    new = "+1 (917) 555-0199"
    start = gw.step_up_start("voice", new, "ACC-7731-0042")
    assert start["status"] == "code_sent" and start["delivered_to"].endswith("0142")  # OTP to the phone ON FILE
    bad = await gw.step_up_verify(start["challenge_id"], "000000" if start["demo_code"] != "000000" else "111111")
    assert bad["status"] == "failed" and bad["attempts_left"] == 2
    ok = await gw.step_up_verify(start["challenge_id"], start["demo_code"])
    assert ok["status"] == "verified" and ok["new_identifier_linked"]
    r = await gw.handle("voice", new, "what's happening with my hotel dispute?", ok["session_id"])
    assert r.customer_id == MAYA and r.assurance == "HIGH" and "DSP-5521" in r.text
    again = await gw.handle("voice", new, "hello")  # a later call from the linked number
    assert again.assurance == "MEDIUM" and any("NEW DEVICE" in n for n in again.trace["identity"]["notes"])


async def test_s03_otp_locks_after_three_wrong_codes(gw):
    start = gw.step_up_start("voice", "+1 917 555 0199", "ACC-77310042")
    wrong = "999999" if start["demo_code"] != "999999" else "888888"
    for _ in range(2):
        await gw.step_up_verify(start["challenge_id"], wrong)
    assert (await gw.step_up_verify(start["challenge_id"], wrong))["status"] == "locked"


async def test_s04_household_shared_landline_is_disambiguated(gw):
    r = await gw.handle("voice", "+1 (212) 555-0100", "hi")
    assert r.step_up and r.step_up["reason"] == "ambiguous"
    start = gw.step_up_start("voice", "+1 (212) 555-0100", "ACC-77310077")  # Arjun's account
    ok = await gw.step_up_verify(start["challenge_id"], start["demo_code"])
    assert ok["customer_id"] == ARJUN and not ok["new_identifier_linked"]
    assert gw.step_up_start("voice", "+1 (212) 555-0100", "ACC-55102988")["status"] == "failed"  # Daniel is not on that line


async def test_s05_contact_change_needs_high_assurance(gw):
    r = await gw.handle("voice", "2125550142", "Please update my phone number to 917 555 0123")
    assert any(w.get("needs_step_up") for w in r.trace["writes"]) and "verify" in r.text.lower()
    r2 = await gw.handle("web_chat", "ACC-77310042", "My new number is 917 555 0123")
    saved = [w for w in r2.trace["writes"] if w["status"] == "saved"]
    assert saved and "phone_number" in saved[0]["redacted"] and "917 555 0123" not in saved[0]["fact"]


# ---------------- 2. Contact center + CRM ---------------------------------------

async def test_s06_screen_pop_and_contact_attributes(gw):
    out = await gw.crm.inbound_contact({"ContactId": "c-1", "Channel": "VOICE",
                                        "CustomerEndpoint": {"Address": "+12125550142"}, "Attributes": {"intent": "dispute"}})
    attrs, pop = out["contact_attributes"], out["screen_pop"]
    assert attrs["route_queue"] == "Card-Disputes" and attrs["customer_token"].startswith("ctk1_")
    assert "CUST-" not in json.dumps(attrs)  # the contact flow only gets the token
    assert any(i["ref"] == "DSP-5521" for i in pop["open_issues"])
    assert any("DSP-5521" in m["text"] for m in pop["memory_highlights"])
    assert any("DSP-5521" in a for a in pop["next_best_actions"])


async def test_s07_accessibility_routes_to_trained_queue(gw):
    out = await gw.crm.inbound_contact({"ContactId": "c-2", "Channel": "VOICE", "CustomerEndpoint": {"Address": "6465550177"}})
    assert out["contact_attributes"]["route_queue"] == "Accessibility-Trained-Agents"


async def test_s08_wrap_up_writes_crm_activity_memory_and_event(gw):
    await gw.crm.inbound_contact({"ContactId": "c-3", "Channel": "VOICE", "CustomerEndpoint": {"Address": "2125550142"}})
    out = await gw.crm.wrap_up("c-3", disposition="Resolved - info given",
                               notes="Customer wants a paper copy of the dispute letter mailed. Card 4111 1111 1111 1111 mentioned.",
                               agent_id="a-42")
    act = out["crm_activity"]
    assert act["taxonomy_code"] == "SERVICING.DISPUTE.STATUS_INQUIRY" and "4111 1111" not in act["notes"]
    assert out["memory"]["status"] == "saved"
    assert any(e["payload"].get("activity_id") == act["activity_id"] for e in gw.bus.tail("crm.activity.v1"))


async def test_s09_sor_event_supersedes_stale_memory(gw):
    token = gw.scope(MAYA)[1]
    old = [r for r in gw.memory_service.ledger.active(token) if "DSP-5521" in r.text][0]
    await gw.publish_sor_event("Cards", "DSP-5521", "Resolved", "credit of $412.80 issued")
    new = [r for r in gw.memory_service.ledger.active(token) if "DSP-5521" in r.text]
    assert len(new) == 1 and new[0].version == 2 and new[0].supersedes == old.memory_id and old.status == "SUPERSEDED"
    r = await gw.handle("voice", "2125550142", "any news on my hotel dispute?")
    assert "Resolved" in r.text and old.memory_id not in [m["memory_id"] for m in r.trace["memories_used"]]


# ---------------- 3. Tokenized identity -------------------------------------------

async def test_s10_memory_store_bus_and_scopes_hold_no_raw_ids(gw):
    await gw.handle("web_chat", "ACC-77310042", "Please remember that I fly to Lisbon on November 10, 2026")
    store = gw.memory_service.inner._store
    assert all(not uid.startswith("CUST-") for _, uid in store)
    dump = json.dumps([[e.model_dump(mode="json") for e in v] for v in store.values()])
    assert "ACC-77310042" not in dump and "Maya Patel" not in dump
    ev = gw.bus.tail("cx.interaction.v1")[-1]["payload"]
    assert "Lisbon" not in json.dumps(ev) and ev["taxonomy_code"] == "TRAVEL.NOTICE.CREATE"
    assert gw.scope(MAYA)[1] != gw.vault.tokenize(MAYA, "business")  # partition-scoped tokens


# ---------------- 4. Lifecycle ----------------------------------------------------

async def test_s11_travel_memory_expires_after_trip(gw):
    await gw.handle("web_chat", "ACC-77310042", "Please remember that I fly to Lisbon on November 10, 2026 until November 20, 2026")
    rec = [r for r in gw.memory_service.ledger.active(gw.scope(MAYA)[1]) if "Lisbon" in r.text and r.source_kind == "agent_tool"][0]
    assert clock.iso(rec.expires_at).startswith("2026-12-04")  # last trip date + 14 days
    clock.advance(days=(rec.expires_at - clock.now()) / 86400 + 1)
    expired = await gw.memory_service.sweep_expired()
    assert rec.memory_id in [e["memory_id"] for e in expired] and rec.status == "EXPIRED"
    assert not gw.memory_service.ledger.is_usable(rec.memory_id)


async def test_s12_erasure_respects_legal_hold_and_export_has_audit(gw):
    part, token = gw.scope(DANIEL)
    gw.memory_service.set_legal_hold(token, True, actor="legal")
    assert (await gw.memory_service.erase_customer(part.app_name, token, actor="privacy", reason="test"))["status"] == "refused"
    gw.memory_service.set_legal_hold(token, False, actor="legal")
    out = await gw.memory_service.erase_customer(part.app_name, token, actor="privacy", reason="test")
    assert out["status"] == "erased" and out["records"] > 0 and not gw.memory_service.ledger.active(token)
    assert not gw.memory_service.inner.all_facts(part.app_name, token)
    actions = [e["action"] for e in gw.audit.for_customer(token)]
    assert "ERASE_REFUSED" in actions and "ERASE" in actions


# ---------------- 5. Governance and compliance ---------------------------------------

async def test_s13_consent_withdrawn_means_session_only(gw):
    token = gw.scope(LUCIA)[1]
    gw.memory_service.set_consent(token, False, actor="privacy")
    r = await gw.handle("web_chat", "ACC-90014533", "Please remember that I'm opening a second store in March")
    assert r.trace["writes"][0]["status"] == "rejected" and "consent" in r.trace["writes"][0]["reason"]
    assert "skip: memory consent withdrawn" in r.trace["extraction"]


async def test_s14_special_category_blocked_and_pan_redacted(gw):
    r = await gw.handle("web_chat", "ACC-77310042", "Please remember that I'm in hospital for chemo next month")
    assert r.trace["writes"][0]["status"] == "rejected" and "special-category" in r.trace["writes"][0]["reason"]
    text, found = redact("card 4111 1111 1111 1111, ssn 123-45-6789, acct ACC-77310042")
    assert "card ending 1111" in text and "[REDACTED-SSN]" in text and "account ending 0042" in text
    assert redact("order 1234567890123")[1] == ["long_number"]  # not Luhn-valid: still masked, labelled differently


async def test_s15_audit_chain_detects_tampering(gw):
    await gw.handle("voice", "2125550142", "where is my dispute")
    assert gw.audit.verify()["ok"]
    gw.audit.entries[3]["details"]["tampered"] = True
    assert not gw.audit.verify()["ok"]


async def test_s16_agents_cannot_write_sor_owned_topics(gw):
    from cx_memory.governance import Assurance
    from cx_memory.governed_memory import TurnContext
    part, token = gw.scope(MAYA)
    ctx = TurnContext(token, part.app_name, part.name, "web_chat", Assurance.HIGH, "web_chat_agent")
    out = await gw.memory_service.write_fact(app_name=part.app_name, user_id=token, text="Customer has a platinum card ending 9999",
                                             topic="product_holdings", ctx=ctx)
    assert out["status"] == "rejected" and "system of record" in out["reason"]


# ---------------- 6. Resilience -----------------------------------------------------

async def test_s17_memory_outage_degrades_gracefully_and_outbox_replays(gw):
    gw.memory_service.faults.memory_bank = "down"
    r = await gw.handle("web_chat", "ACC-77310042", "Please remember that I fly to Lisbon on November 10, 2026")
    assert r.text and r.trace["degraded"] and r.trace["writes"][0]["status"] == "queued"
    for _ in range(3):
        await gw.handle("voice", "2125550142", "hello")
    assert gw.memory_service.breaker.state == "open"
    gw.memory_service.faults.memory_bank = "ok"
    gw.memory_service.breaker.reset()
    out = await gw.memory_service.drain_outbox(force=True)
    assert out["delivered"] >= 1 and out["pending"] == 0
    part, token = gw.scope(MAYA)
    assert any("Lisbon" in f for f in gw.memory_service.inner.all_facts(part.app_name, token))


async def test_s18_slow_memory_read_hits_timeout_budget(gw):
    gw.memory_service.faults.memory_bank, gw.memory_service.faults.slow_ms = "slow", 1200
    gw.memory_service.read_timeout_s = 0.1
    r = await gw.handle("voice", "2125550142", "where is my dispute")
    assert r.text and any("budget" in d for d in r.trace["degraded"])


# ---------------- 7. Partitions, quotas, cost, throughput ------------------------------

async def test_s19_partitions_isolate_quota(gw):
    consumer, business = gw.router.partitions["consumer"], gw.router.partitions["business"]
    consumer.buckets["write"].tokens = 0
    consumer.buckets["write"].per_minute = 0
    r = await gw.handle("web_chat", "ACC-77310042", "Please remember that I fly to Lisbon on November 10, 2026")
    assert r.trace["writes"][0]["status"] == "queued" and r.trace["partition"] == "consumer"
    r2 = await gw.handle("web_chat", "ACC-90014533", "Please remember that I open a second store in March")
    assert r2.trace["partition"] == "business" and r2.trace["writes"][0]["status"] == "saved"
    assert business.app_name != consumer.app_name


async def test_s20_extraction_policy_skips_small_talk_and_batches(gw):
    r = await gw.handle("voice", "2125550142", "ok thanks")
    assert r.trace["extraction"] == ["skip: small talk"]
    gw.memory_service.policy.mode = "batched"
    r1 = await gw.handle("voice", "2125550142", "my card was declined")
    assert r1.trace["extraction"][0].startswith("buffered")
    assert (await gw.end_session(r1.session_id))["extraction"].startswith("extracted")


async def test_s21_capacity_plan_and_burst(gw):
    kw = dict(contacts_per_month=1_000_000, turns_per_contact=4, salient_share=0.4, batch_turns=6, avg_turn_tokens=60,
              peak_factor=3, extract_qpm_quota=120, read_qpm_quota=600, memories_per_customer=25, customers=800_000)
    calls = {m: capacity_plan(mode=m, **kw)["monthly"]["extraction_calls"] for m in ("per_turn", "salient", "batched")}
    assert calls["batched"] < calls["salient"] < calls["per_turn"]
    out = await run_burst(contacts=40, concurrency=20, turns_per_contact=2, mb_latency_ms=5)
    assert out["throughput"]["turns_per_s"] > 0 and out["memory_ops"]["retrieve_calls"] > 0


async def test_s22_per_turn_trace_explains_the_answer(gw):
    r = await gw.handle("email", "maya.patel@example.com", "Any update on the hotel dispute?")
    t = r.trace
    assert t["identity"]["method"] == "sender_address_match" and r.assurance == "MEDIUM"
    used = [m for m in t["memories_used"] if "DSP-5521" in m["text"]]
    assert used and used[0]["source"].startswith("sor_seed") and t["taxonomy_code"].startswith("SERVICING.DISPUTE")
    assert t["cost_delta"]["turns"] == 1 and t["cost_delta"]["est_usd"] > 0
