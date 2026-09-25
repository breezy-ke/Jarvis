from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.policy.config import ActionKindPolicy
from jarvis.policy.types import Autonomy, Risk
from jarvis.policy.validators import (
    KnownContacts,
    NoKnownContacts,
    ValidationContext,
    attachment_mentioned,
    links_safe,
    opt_out_present,
    recipients_known,
)
from tests.policy_helpers import SetContacts


def ctx(payload: dict[str, Any], contacts: KnownContacts | None = None) -> ValidationContext:
    return ValidationContext(
        kind="x.y",
        payload=payload,
        policy=ActionKindPolicy(description="t", autonomy=Autonomy.L2, risk=Risk.MEDIUM),
        session=cast(AsyncSession, None),
        now=datetime(2026, 1, 1, tzinfo=UTC),
        contacts=contacts or NoKnownContacts(),
    )


@pytest.mark.parametrize(
    ("body", "outcome"),
    [
        ("If you'd prefer not to hear from me again, just reply STOP.", "pass"),
        ("Reply 'stop' and I won't contact you again.", "pass"),
        ("Click here to unsubscribe.", "pass"),
        ("Looking forward to working together!", "block"),
    ],
)
async def test_opt_out(body: str, outcome: str) -> None:
    assert (await opt_out_present(ctx({"body": body}))).outcome == outcome


@pytest.mark.parametrize(
    ("body", "outcome"),
    [
        ("See https://breezy.co.ke/work for examples", "pass"),
        ("Portfolio: http://example.com", "warn"),
        ("Short link https://bit.ly/abc", "warn"),
        ("Raw IP https://203.0.113.5/login", "warn"),
        ("Lookalike https://xn--pple-43d.com", "warn"),
        ("Run data:text/html;base64,PHNjcmlwdD4=", "block"),
    ],
)
async def test_links(body: str, outcome: str) -> None:
    assert (await links_safe(ctx({"body": body}))).outcome == outcome


async def test_recipients_invalid_address_blocks() -> None:
    result = await recipients_known(ctx({"to": ["not-an-address"]}))
    assert result.outcome == "block"


async def test_recipients_unknown_warns_and_bcc_is_flagged() -> None:
    result = await recipients_known(ctx({"to": ["a@b.co"], "bcc": ["c@d.co"]}))
    assert result.outcome == "warn"
    assert "BCC" in result.message


async def test_people_already_on_the_conversation_count_as_known() -> None:
    contacts = SetContacts(threads={"t1": {"achieng@client.co.ke", "otieno@client.co.ke"}})
    reply = {"thread_id": "t1", "to": ["achieng@client.co.ke"], "cc": ["Otieno@Client.co.ke"]}
    assert (await recipients_known(ctx(reply, contacts))).outcome == "pass"
    elsewhere = {**reply, "thread_id": "t2"}  # the same people, on another conversation
    assert (await recipients_known(ctx(elsewhere, contacts))).outcome == "warn"
    added = {**reply, "cc": ["new@else.test"]}
    result = await recipients_known(ctx(added, contacts))
    assert result.outcome == "warn"
    assert "new@else.test" in result.message


async def test_attachment_consistency() -> None:
    assert (await attachment_mentioned(ctx({"body": "PFA the invoice"}))).outcome == "warn"
    ok = await attachment_mentioned(ctx({"body": "PFA the invoice", "attachments": ["inv.pdf"]}))
    assert ok.outcome == "pass"
