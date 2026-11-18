"""Channel definitions, style rules and deterministic output validators.

Validators are used three ways: (1) offline tests, (2) rule-based eval checks,
(3) optional runtime guard in the gateway (logs a violation; voice is also trimmed).
"""
from __future__ import annotations

import re
from enum import Enum


class Channel(str, Enum):
    WEB_CHAT = "web_chat"
    VOICE = "voice"
    EMAIL = "email"


STYLE_RULES: dict[Channel, str] = {
    Channel.WEB_CHAT: (
        "You are answering in WEB CHAT. Be friendly and concise: 1-3 short sentences. "
        "A simple bullet list is allowed when listing options. No headings, no long paragraphs."
    ),
    Channel.VOICE: (
        "You are answering on a VOICE call; your reply is read aloud by text-to-speech. "
        "Use strictly fewer than 40 words of plain spoken English. "
        "Never use lists, bullets, markdown, emojis, symbols or URLs. Spell numbers naturally."
    ),
    Channel.EMAIL: (
        "You are answering by EMAIL. Use a professional email style: start with a greeting that uses "
        "the customer's first name (e.g. 'Dear Maya,'), write short paragraphs, and sign off exactly with "
        "a final line 'Customer Care'."
    ),
}

_MARKDOWN = re.compile(r"(^\s*[-*•]\s)|(^\s*\d+\.\s)|(\*\*)|(__)|(^#)|(`)", re.MULTILINE)
_URL = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?]+(?=\s|$)")


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'’-]+\b", text))


def check_web_chat(text: str) -> list[str]:
    errors: list[str] = []
    prose = "\n".join(l for l in text.splitlines() if not re.match(r"^\s*[-*•]\s", l))
    sentences = len(_SENTENCE_END.findall(prose)) or (1 if prose.strip() else 0)
    if sentences > 3:
        errors.append(f"web_chat: {sentences} sentences (max 3)")
    if re.search(r"^#", text, re.MULTILINE):
        errors.append("web_chat: headings not allowed")
    if word_count(text) > 90:
        errors.append("web_chat: too long (>90 words)")
    return errors


def check_voice(text: str) -> list[str]:
    errors: list[str] = []
    n = word_count(text)
    if n >= 40:
        errors.append(f"voice: {n} words (must be < 40)")
    if _MARKDOWN.search(text):
        errors.append("voice: list/markdown detected")
    if _URL.search(text):
        errors.append("voice: URL detected")
    if "\n" in text.strip():
        errors.append("voice: multi-line output")
    return errors


def check_email(text: str, first_name: str | None = None) -> list[str]:
    errors: list[str] = []
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    if not lines:
        return ["email: empty"]
    greeting = lines[0]
    if not re.match(r"^(dear|hi|hello|good (morning|afternoon|evening))\b", greeting, re.IGNORECASE):
        errors.append("email: missing greeting")
    if first_name and first_name.lower() not in greeting.lower():
        errors.append(f"email: greeting does not use first name '{first_name}'")
    if lines[-1] != "Customer Care":
        errors.append("email: must be signed 'Customer Care'")
    paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    if any(word_count(p) > 80 for p in paragraphs):
        errors.append("email: paragraph longer than 80 words")
    return errors


def validate(channel: Channel, text: str, first_name: str | None = None) -> list[str]:
    if channel is Channel.WEB_CHAT:
        return check_web_chat(text)
    if channel is Channel.VOICE:
        return check_voice(text)
    return check_email(text, first_name)


def enforce_voice(text: str) -> str:
    """Last-resort guard for TTS: strip markdown/URLs, collapse lines, hard-cap at 39 words."""
    text = _URL.sub("", text)
    text = re.sub(r"[*_`#•]", "", text)
    text = re.sub(r"^\s*(\d+\.|-)\s*", "", text, flags=re.MULTILINE)
    text = " ".join(text.split())
    words = text.split(" ")
    if len(words) >= 40:
        text = " ".join(words[:39]).rstrip(",;:") + "."
    return text
