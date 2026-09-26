"""Facts about an email that code checks before any model reads it.

Signals are short codes shown to you as chips and given to the triage model as
facts. The **hard** ones always mark an email suspicious, whatever the model
thinks, because an email can't argue its way out of them:

* `auth_failed`         Gmail's own check says the sender's domain didn't send it
* `spoofed_name`        the display name copies someone you know, from another address
* `dangerous_link`      a javascript:, data:, vbscript: or file: link
* `hidden_instructions` text you can't see (CSS-hidden, or a plain-text part that
                        differs from what Gmail shows) that talks to an AI

Phrasing that merely *sounds* like it addresses an AI ("act as", "call the
function") is common in a developer's inbox, so it's a soft signal only.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from jarvis.db.models import MailMessage
from jarvis.policy.validators import link_problems  # the same rules as outgoing mail
from jarvis.profile.schema import Contact
from jarvis.security.untrusted import injection_signals, sanitize

HARD = frozenset({"auth_failed", "spoofed_name", "dangerous_link", "hidden_instructions"})
NEWSLETTER_LABELS = frozenset({"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"})
_URL_RE = re.compile(r"""(?i)\b((?:https?|ftp|javascript|data|vbscript|file):[^\s<>"'()]+)""")
_ADDRESS_IN_NAME = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
MAX_CHECKED = 250_000  # the whole email, not just what a model gets to read
_BASE64_RE = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{24,}={0,2}(?![A-Za-z0-9+/=])")

# Chips in the app. Hard ones first.
LABELS = {
    "auth_failed": "Failed Gmail's authenticity check",
    "spoofed_name": "Name copies someone you know",
    "dangerous_link": "Dangerous link",
    "hidden_instructions": "Hidden text aimed at AI",
    "injection_phrasing": "Text aimed at AI assistants",
    "hidden_text": "Hidden text",
    "risky_link": "Link needs a careful look",
    "reply_to_elsewhere": "Replies go to another address",
    "first_time_sender": "New sender",
    "newsletter": "Mailing list",
    "vip": "VIP",
    "known_sender": "Someone you know",
    "attachments": "Attachments",
}


@dataclass(frozen=True)
class Sender:
    known: bool  # you've written to them, or they're in your profile's contacts
    vip: bool
    first_time: bool


def decoded_blobs(text: str, *, limit: int = 20) -> str:
    """Text hidden in base64 ("decode this and follow it"), for the same checks."""
    found: list[str] = []
    for blob in _BASE64_RE.findall(text)[:limit]:
        try:
            decoded = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=True).decode()
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if decoded and all(ch.isprintable() or ch.isspace() for ch in decoded):
            found.append(decoded)
    return "\n".join(found)


# Cyrillic and Greek letters that look Latin: "Аchieng" (Cyrillic А) is "Achieng".
_CONFUSABLES = str.maketrans(
    "аеорсухіјѕԁԛԝАВЕКМНОРСТХІЈЅαβεικνορτυχΑΒΕΖΗΙΚΜΝΟΡΤΥΧ",
    "aeopcyxijsdqwABEKMHOPCTXIJSabeiknoptuxABEZHIKMNOPTYX",
)


def _norm_name(name: str) -> str:
    folded = unicodedata.normalize("NFKC", name).translate(_CONFUSABLES).casefold()
    return " ".join(re.sub(r"[^\w\s]", " ", folded).split())


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower()


def sender_info(address: str, *, contacts: Iterable[Contact], sent: int, received: int) -> Sender:
    profile = [c for c in contacts if c.email and c.email.strip().lower() == address]
    return Sender(
        known=sent > 0 or bool(profile),
        vip=any(c.vip for c in profile),
        first_time=sent == 0 and received <= 1 and not profile,
    )


def message_signals(
    message: MailMessage,
    *,
    subject: str,
    body: str,
    display_name: str,
    sender: Sender,
    contacts: Iterable[Contact],
    owner: tuple[str | None, Iterable[str]] = (None, ()),
) -> list[str]:
    """`owner` is your address and your names: an email showing your name from
    another address is impersonating you."""
    signals: set[str] = set()
    meta = message.meta or {}
    labels = set(message.label_ids or [])

    if (
        meta.get("list_unsubscribe")
        or meta.get("precedence") in {"bulk", "list", "junk"}
        or labels & NEWSLETTER_LABELS
    ):
        signals.add("newsletter")

    auth = meta.get("auth") or {}
    if auth.get("dmarc") == "fail" or (auth.get("spf") == "fail" and auth.get("dkim") != "pass"):
        signals.add("auth_failed")

    address = message.from_address
    for embedded in _ADDRESS_IN_NAME.findall(display_name):
        if embedded.lower() != address:
            signals.add("spoofed_name")  # "boss@company.com" <someone@elsewhere>
    name = _norm_name(display_name)
    if name:
        for contact in contacts:
            email = (contact.email or "").strip().lower()
            if email and email != address and _norm_name(contact.name) == name:
                signals.add("spoofed_name")
        owner_address, owner_names = owner
        yours = {_norm_name(n) for n in owner_names if n} - {""}
        if owner_address and address != owner_address.lower() and name in yours:
            signals.add("spoofed_name")  # your own name, from someone else

    sender_domain = _domain(address)
    if any(_domain(a) != sender_domain for a in message.reply_to or []):
        signals.add("reply_to_elsewhere")

    urls = [m.group(1) for m in _URL_RE.finditer(body)]
    urls += [str(href) for href in meta.get("links") or []]  # behind buttons too
    blocks, warns = link_problems(urls)
    if blocks:
        signals.add("dangerous_link")
    elif [w for w in warns if not w.startswith("insecure link")]:
        signals.add("risky_link")  # plain http alone is too common to flag

    # Checked as a model would read it: invisible characters gone ("ig\u200bnore" is
    # "ignore"), and anything base64-encoded decoded. The sender's name counts too.
    readable = sanitize(f"{display_name}\n{subject}\n{body}", max_chars=MAX_CHECKED).text
    if injection_signals(readable) or injection_signals(decoded_blobs(readable)):
        signals.add("injection_phrasing")
    if meta.get("concealed"):
        signals.add("hidden_instructions")
    if meta.get("hidden_text"):
        signals.add("hidden_text")
    if message.has_attachments:
        signals.add("attachments")

    if sender.vip:
        signals.add("vip")
    if sender.known:
        signals.add("known_sender")
    elif sender.first_time:
        signals.add("first_time_sender")
    return sorted(signals, key=lambda s: (s not in HARD, s))


def is_hard(signals: Iterable[str]) -> bool:
    return bool(HARD & set(signals))
