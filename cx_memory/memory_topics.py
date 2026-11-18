"""The 8 Memory Bank topics: 4 managed + 4 custom (banking CX)."""
from __future__ import annotations

MANAGED_TOPICS = [
    "USER_PERSONAL_INFO",
    "USER_PREFERENCES",
    "KEY_CONVERSATION_DETAILS",
    "EXPLICIT_INSTRUCTIONS",
]

CUSTOM_TOPICS = {
    "open_service_issues": (
        "Unresolved service issues the customer has raised on any channel: card disputes, lost or "
        "locked cards, replacement cards, pending address changes, complaints. Include the reference "
        "ID, amount, and current status when mentioned. Update the status when it changes."
    ),
    "product_holdings": (
        "Banking products the customer holds or is applying for (checking, savings, credit cards, "
        "business accounts, merchant services), identified by product type and last four digits only. "
        "Never store full account or card numbers."
    ),
    "travel_and_life_events": (
        "Upcoming travel (destinations and dates), relocations, new business locations, or other life "
        "events that affect how the bank should service the customer."
    ),
    "contact_and_accessibility_needs": (
        "How and when the customer wants to be contacted, preferred language, and any accessibility "
        "needs (e.g. hard of hearing, prefers written confirmation)."
    ),
}

ALL_TOPIC_LABELS = [t.lower() for t in MANAGED_TOPICS] + list(CUSTOM_TOPICS)

# Few-shot example for the custom topics (recommended by Memory Bank docs for custom topics)
EXAMPLE_CONVERSATION = [
    ("user", "Hi, I'm calling about the hotel charge I disputed last week, reference DSP-5521. Also I fly to Lisbon on the 10th."),
    ("model", "Thanks, I can see dispute DSP-5521 is under review. I've noted your Lisbon trip."),
]
EXAMPLE_FACTS = [
    "Customer has an open card dispute DSP-5521 for a hotel charge; status under review.",
    "Customer is travelling to Lisbon starting November 10.",
]


def memory_topics_config() -> list[dict]:
    topics = [{"managed_memory_topic": {"managed_topic_enum": t}} for t in MANAGED_TOPICS]
    topics += [{"custom_memory_topic": {"label": k, "description": v}} for k, v in CUSTOM_TOPICS.items()]
    assert len(topics) == 8
    return topics


def generate_examples_config() -> list[dict]:
    return [{
        "conversation_source": {
            "events": [{"content": {"role": r, "parts": [{"text": t}]}} for r, t in EXAMPLE_CONVERSATION]
        },
        "generated_memories": [{"fact": f} for f in EXAMPLE_FACTS],
    }]
