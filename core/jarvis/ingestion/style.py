"""Learn the owner's writing style from their own sent emails.

Everything here is deterministic statistics: no model call, and no email
bodies are stored. Only the derived style profile is kept, as suggestions
for the owner to confirm.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from statistics import median

_QUOTED_REPLY_RE = re.compile(
    r"(?ims)^(on .{5,200}wrote:|-{2,}\s*original message\s*-{2,}|from: .+?sent: ).*"
)
_SIGNATURE_SPLIT_RE = re.compile(r"(?m)^--\s*$")
_GREETING_RE = re.compile(
    r"^(hi|hello|hey|dear|good (?:morning|afternoon|evening)|greetings|habari|hujambo)"
    r"\b[^\n,]*[,!]?",
    re.IGNORECASE,
)
_SIGN_OFF_RE = re.compile(
    r"^(best(?: regards)?|kind regards|regards|warm regards|thanks|thank you|cheers|sincerely|"
    r"many thanks|asante|all the best|talk soon)[,!.]?\s*$",
    re.IGNORECASE,
)
_EMOJI_RE = re.compile("[\U0001f300-\U0001faff☀-➿]")
_SWAHILI_HINTS = frozenset(
    {"habari", "asante", "sawa", "karibu", "tafadhali", "ndiyo", "hapana", "pole", "kesho", "leo"}
)


@dataclass
class StyleProfile:
    emails_analyzed: int = 0
    median_words: int = 0
    greetings: list[str] = field(default_factory=list)
    sign_offs: list[str] = field(default_factory=list)
    uses_emoji: bool = False
    uses_swahili: bool = False
    formality: str = "unknown"
    notes: list[str] = field(default_factory=list)


def strip_quoted(body: str) -> str:
    body = _QUOTED_REPLY_RE.split(body)[0]
    body = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith(">"))
    return _SIGNATURE_SPLIT_RE.split(body)[0].strip()


def analyze(bodies: list[str]) -> StyleProfile:
    cleaned = [strip_quoted(b) for b in bodies if b and b.strip()]
    cleaned = [c for c in cleaned if len(c.split()) >= 3]
    profile = StyleProfile(emails_analyzed=len(cleaned))
    if not cleaned:
        return profile

    greetings: Counter[str] = Counter()
    sign_offs: Counter[str] = Counter()
    words: list[int] = []
    contractions = 0
    swahili = 0
    emoji = 0
    for text in cleaned:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        words.append(len(text.split()))
        if lines and (match := _GREETING_RE.match(lines[0])):
            greetings[
                re.sub(r"\s+\S+[,!]?$", "", match.group(0)).strip().title() or match.group(0)
            ] += 1
        for line in lines[-4:]:
            if _SIGN_OFF_RE.match(line):
                sign_offs[line.rstrip(",.!").strip().title()] += 1
                break
        contractions += len(re.findall(r"\b\w+'(?:s|re|ll|ve|d|t|m)\b", text.lower()))
        tokens = set(re.findall(r"[a-z]+", text.lower()))
        swahili += bool(tokens & _SWAHILI_HINTS)
        emoji += bool(_EMOJI_RE.search(text))

    n = len(cleaned)
    profile.median_words = int(median(words))
    profile.greetings = [g for g, _ in greetings.most_common(3)]
    profile.sign_offs = [s for s, _ in sign_offs.most_common(3)]
    profile.uses_emoji = emoji / n >= 0.15
    profile.uses_swahili = swahili / n >= 0.1
    rate = contractions / max(sum(words), 1) * 100
    profile.formality = "casual" if rate > 2.0 else "neutral" if rate > 0.6 else "formal"
    length = (
        "short" if profile.median_words < 60 else "medium" if profile.median_words < 150 else "long"
    )
    profile.notes = [
        f"Typically writes {length} emails (median {profile.median_words} words).",
        f"Register is mostly {profile.formality}.",
    ]
    if profile.uses_swahili:
        profile.notes.append("Sometimes writes in Swahili.")
    if profile.uses_emoji:
        profile.notes.append("Uses emoji occasionally.")
    return profile


def to_suggestions(profile: StyleProfile) -> dict[str, object]:
    """Profile fields to suggest (the owner confirms each one)."""
    suggestions: dict[str, object] = {}
    if profile.emails_analyzed == 0:
        return suggestions
    if profile.greetings:
        suggestions["communication.greeting"] = profile.greetings[0]
    if profile.sign_offs:
        suggestions["communication.sign_off"] = profile.sign_offs[0]
    if profile.formality != "unknown":
        suggestions["communication.formality"] = profile.formality
    suggestions["communication.style_notes"] = profile.notes
    if profile.uses_swahili:
        suggestions["communication.languages"] = ["English", "Swahili"]
    return suggestions
