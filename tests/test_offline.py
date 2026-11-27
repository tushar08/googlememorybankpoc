"""Core offline tests (no GCP): identity, profiles, channel style, routing, memory write path.
Scenario tests (step-up, CRM, lifecycle, governance, resilience, cost) live in test_scenarios.py.

    pytest -q
"""
import asyncio

import pytest

from cx_memory.channels import Channel, check_email, check_voice, check_web_chat, enforce_voice
from cx_memory.identity import IdentityNotResolved, IdentityResolver
from cx_memory.memory_topics import memory_topics_config
from cx_memory.profiles import load_profile, profile_facts
from tests.fake_llm import FakeLlm

pytestmark = pytest.mark.asyncio
R = IdentityResolver.from_source_systems()

# ---------------- Identity resolution (Phase 3) -------------------------------

def test_01_resolve_web_chat_account_id_with_formatting():
    assert R.resolve(Channel.WEB_CHAT, "acc-7731 0042") == "CUST-1001"


def test_02_resolve_voice_caller_number_variants():
    for n in ["+1 (212) 555-0142", "2125550142", "+12125550142"]:
        assert R.resolve(Channel.VOICE, n) == "CUST-1001"


def test_03_resolve_email_sender_display_name():
    assert R.resolve(Channel.EMAIL, "Maya Patel <MAYA.PATEL@example.com>") == "CUST-1001"


def test_04_unknown_identifier_raises():
    with pytest.raises(IdentityNotResolved):
        R.resolve(Channel.VOICE, "+1 999 000 0000")


# ---------------- Profiles + topics ------------------------------------------

def test_05_profile_merges_five_source_systems():
    p = load_profile("CUST-1001")
    facts = " ".join(profile_facts(p))
    for src in ["CRM", "CoreBanking", "Cards", "Servicing", "Preferences"]:
        assert f"[source: {src}]" in facts


def test_06_memory_bank_has_8_topics_4_managed_4_custom():
    t = memory_topics_config()
    assert len(t) == 8
    assert sum("managed_memory_topic" in x for x in t) == 4
    assert sum("custom_memory_topic" in x for x in t) == 4


# ---------------- Channel style validators ------------------------------------

def test_07_voice_rejects_40_words_lists_urls():
    assert check_voice("word " * 40)
    assert check_voice("- item one\n- item two")
    assert check_voice("Visit https://example.com for details")
    assert not check_voice("Thanks Maya, your dispute is under review.")


def test_08_voice_guard_makes_any_text_tts_safe():
    out = enforce_voice("**Sure!**\n- one\n- two https://x.y " + "word " * 50)
    assert not check_voice(out)


def test_09_web_chat_max_three_sentences_bullets_ok():
    assert not check_web_chat("Hi Maya! Here are options:\n- Lock card\n- Replace card")
    assert check_web_chat("One. Two. Three. Four.")


def test_10_email_greeting_first_name_and_signature():
    good = "Dear Maya,\n\nYour dispute is under review.\n\nCustomer Care"
    assert not check_email(good, "Maya")
    assert check_email("Hello,\n\nUpdate.\n\nThanks", "Maya")


# ---------------- Routing + agents (Phase 2) ----------------------------------

async def test_11_deterministic_routing_each_channel(gw):
    for ch, ident in [("web_chat", "ACC-77310042"), ("voice", "2125550142"), ("email", "maya.patel@example.com")]:
        r = await gw.handle(ch, ident, "hello")
        assert r.agent == f"{ch}_agent" and r.customer_id == "CUST-1001"


async def test_12_unknown_channel_rejected(gw):
    with pytest.raises(ValueError):
        await gw.handle("sms", "2125550142", "hello")


async def test_13_three_agents_share_one_memory_scope_per_partition(gw):
    apps = {r.app_name for r in gw.runners.values()}
    assert apps == {"cx_unified_consumer", "cx_unified_business"} and len(gw.agents) == 3
    scopes = {(await gw.handle(ch, i, "hello")).trace["customer_token"] for ch, i in
              [("web_chat", "ACC-77310042"), ("voice", "2125550142"), ("email", "maya.patel@example.com")]}
    assert len(scopes) == 1


async def test_14_preload_memory_injects_bank_facts_before_model_call(gw):
    await gw.handle("web_chat", "ACC-77310042", "what about my hotel dispute")
    assert "<PAST_CONVERSATIONS>" in FakeLlm.requests[-1]
    assert "DSP-5521" in FakeLlm.requests[-1]


async def test_15_session_state_tier_seeded_with_profile(gw):
    r = await gw.handle("email", "maya.patel@example.com", "hello")
    part, token = gw.scope(r.customer_id)
    s = await gw.session_service.get_session(app_name=part.app_name, user_id=token, session_id=r.session_id)
    assert s.state["first_name"] == "Maya" and "Skyline Hotels" in s.state["customer_profile"]


# ---------------- Memory write path -------------------------------------------

async def test_16_save_customer_fact_writes_to_memory_bank(gw):
    r = await gw.handle("web_chat", "ACC-77310042", "Please remember that I fly to Lisbon on the 10th")
    assert "save_customer_fact" in r.tool_calls
    recs = gw.memory_service.ledger.active(gw.scope("CUST-1001")[1])
    assert any("Lisbon" in r.text and r.source_channel == "web_chat" and r.source_kind == "agent_tool" for r in recs)


async def test_17_after_agent_callback_syncs_turn(gw):
    r = await gw.handle("voice", "2125550142", "my card was declined in a shop")
    part, token = gw.scope(r.customer_id)
    s = await gw.session_service.get_session(app_name=part.app_name, user_id=token, session_id=r.session_id)
    assert s.state["turns_synced"] == 1
    assert any("declined" in f for f in gw.memory_service.inner.all_facts(part.app_name, token))


async def test_18_full_card_numbers_redacted_on_save(gw):
    from cx_memory.tools import _redact
    assert "4111" not in _redact("Customer card 4111 1111 1111 1111 was lost")


# ---------------- Cross-channel behaviour --------------------------------------

async def test_19_chat_turn_visible_to_voice_immediately(gw):
    await gw.handle("web_chat", "ACC-77310042", "I'm worried about my Lisbon trip")
    r = await gw.handle("voice", "+1 212 555 0142", "Hi, calling about my trip")
    assert "[web_chat" in FakeLlm.requests[-1] and "Lisbon" in FakeLlm.requests[-1]
    assert "earlier chat" in r.text


async def test_20_customers_are_isolated(gw):
    await gw.handle("web_chat", "ACC-77310042", "Please remember that I fly to Lisbon on the 10th")
    await gw.handle("voice", "6465550177", "where is my replacement card")
    prompt = FakeLlm.requests[-1]
    assert "Lisbon" not in prompt and "DSP-5521" not in prompt and "Daniel" in prompt


async def test_21_three_channels_served_concurrently(gw):
    rs = await asyncio.gather(
        gw.handle("web_chat", "ACC-77310042", "status?"),
        gw.handle("voice", "2125550142", "status?"),
        gw.handle("email", "maya.patel@example.com", "status?"),
    )
    assert {r.channel for r in rs} == {"web_chat", "voice", "email"}
    assert all(not r.style_violations for r in rs)
    assert gw.live.channels_seen(gw.scope("CUST-1001")[1]) == {"web_chat", "voice", "email"}
