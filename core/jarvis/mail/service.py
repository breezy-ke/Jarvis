"""Email, end to end: sync, sort, draft, and send with your approval.

`MailService` runs two loops while Jarvis is up:

* **sync**: every `interval_seconds` (or at once when you tap "Check now"), bring
  Jarvis's copy of your mail up to date, then tidy up after it: confirm sends
  Jarvis made, and withdraw drafts overtaken by events (you replied from Gmail,
  or someone wrote again).
* **triage**: sort each new email, then label it in Gmail (once autonomy is on)
  and, for people you know, draft a reply.

Drafts live here first (encrypted); a draft with a body becomes an `email.send`
proposal that waits for your approval. Nothing is sent any other way.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
from pydantic import ValidationError
from sqlalchemy import select

from jarvis.db.models import ActionProposal, MailDraft, MailMessage, MailThread
from jarvis.db.session import transaction
from jarvis.ingestion.google import GoogleAuth
from jarvis.llm.router import RouterError
from jarvis.mail.actions import (
    JARVIS_LABELS,
    EmailDraftPayload,
    EmailSendPayload,
    register_mail_actions,
)
from jarvis.mail.config import EmailConfig
from jarvis.mail.digest import MailAlerts
from jarvis.mail.drafting import Composed, ReplyDrafter
from jarvis.mail.gmail import GmailAuthError, GmailClient, GmailError
from jarvis.mail.inbox import Inbox
from jarvis.mail.mime import parse_gmail_message, to_raw
from jarvis.mail.store import MailStore, Stored
from jarvis.mail.sync import MailAccess, MailSync, SyncReport
from jarvis.mail.triage import Triage, TriageWorker
from jarvis.notify.owner import OwnerNotifier
from jarvis.policy.engine import PolicyError
from jarvis.policy.state import get_kill_switch
from jarvis.policy.types import Channel, Status
from jarvis.security.hashing import sha256_hex
from jarvis.services import Services

log = logging.getLogger("jarvis.mail")

TRIAGE_IDLE_SECONDS = 30.0
EDITABLE = frozenset({"to", "cc", "bcc", "subject", "body"})
# A reply whose send request ended like this was definitely not sent: it can go again.
SENDABLE_AGAIN = (Status.CANCELLED, Status.REJECTED, Status.FAILED)
# Nobody has said yes to these (or they definitely didn't go), so a newer event can replace them.
REPLACEABLE = (
    Status.PENDING,
    Status.DRAFT_ONLY,
    Status.REJECTED,
    Status.CANCELLED,
    Status.REFUSED,
    Status.EXPIRED,
    Status.FAILED,
)


class MailError(RuntimeError):
    """Something you asked for can't be done (the message says why)."""


def body_hash(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").strip().split("\n")]
    return sha256_hex("\n".join(lines))


@dataclass(frozen=True)
class DraftView:
    draft: MailDraft
    fields: dict[str, Any]
    proposal: ActionProposal | None


class MailService:
    def __init__(
        self,
        services: Services,
        *,
        google: GoogleAuth,
        http: httpx.AsyncClient,
        config: EmailConfig,
        notifier: OwnerNotifier | None = None,
    ) -> None:
        self._s = services
        self._google = google
        self._http = http
        self.config = config
        self.store = MailStore(services.vault, services.clock)
        self.sync = MailSync(
            session_factory=services.session_factory,
            clock=services.clock,
            store=self.store,
            config=config,
            client=self.client,
            access=self.access,
        )
        self.triage = TriageWorker(services, self.store)
        self.drafter = ReplyDrafter(services, self.store)
        self.inbox = Inbox(services, self.store)
        self.notifier = notifier or OwnerNotifier(services)
        self.alerts = MailAlerts(
            services,
            store=self.store,
            inbox=self.inbox,
            notifier=self.notifier,
            config=config,
            access=self.access,
        )
        self._labels: dict[str, str] | None = None
        self._wake_sync = asyncio.Event()
        self._wake_triage = asyncio.Event()
        register_mail_actions(services.registry, self)

    # --- Google ------------------------------------------------------------------------

    async def access(self) -> MailAccess:
        async with self._s.session_factory() as session:
            connection = await self._google.connection(session)
        return MailAccess(
            connection.mail_access, connection.account_email, can_add_holds=connection.can_add_holds
        )

    def client(self) -> GmailClient:
        async def token() -> str:
            async with transaction(self._s.session_factory) as session:
                return await self._google.access_token(session)

        return GmailClient(self._http, token, endpoints=self._google.endpoints)

    async def owner_name(self) -> str | None:
        async with self._s.session_factory() as session:
            identity = (await self._s.profiles.current(session)).profile.identity
        return identity.full_name or identity.preferred_name

    async def label_ids(self) -> dict[str, str]:
        """Jarvis's labels in Gmail (name -> id), created the first time they're needed."""
        if self._labels is not None:
            return self._labels
        client = self.client()
        by_name = {str(item["name"]): str(item["id"]) for item in await client.labels()}
        for name in JARVIS_LABELS.values():
            if name not in by_name:
                try:
                    created = await client.create_label(name)
                    by_name[name] = str(created["id"])
                except GmailError as exc:
                    if exc.status != 409:  # 409: created meanwhile; the next listing has it
                        raise
                    by_name = {str(i["name"]): str(i["id"]) for i in await client.labels()}
        self._labels = {name: by_name[name] for name in JARVIS_LABELS.values()}
        return self._labels

    async def _stood_down(self) -> bool:
        async with self._s.session_factory() as session:
            return (await get_kill_switch(session)).engaged

    async def _autonomy_open(self) -> bool:
        if await self._stood_down():
            return False
        async with self._s.session_factory() as session:
            return (await self._s.profiles.status(session)).open

    # --- Loops -------------------------------------------------------------------------

    def check_now(self) -> None:
        self._wake_sync.set()

    async def run(self, stop: asyncio.Event) -> None:
        await asyncio.gather(self._sync_loop(stop), self._triage_loop(stop))

    async def _wait(self, event: asyncio.Event, stop: asyncio.Event, seconds: float) -> None:
        waiters = [asyncio.ensure_future(event.wait()), asyncio.ensure_future(stop.wait())]
        try:
            await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await waiter
        event.clear()

    async def _sync_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.sync_round()
            await self._wait(self._wake_sync, stop, self.config.sync.interval_seconds)

    async def sync_round(self) -> SyncReport | None:
        try:
            report = await self.sync.sync_once()
        except GmailAuthError as exc:
            log.warning("mail: Google access needs attention: %s", exc)
            return None
        except (GmailError, httpx.HTTPError) as exc:
            log.warning("mail: sync failed, will retry: %s", exc)
            return None
        except Exception:  # keep syncing next round
            log.exception("mail: sync round failed")
            return None
        if report is not None:
            try:
                await self.after_sync(report)
                await self.alerts.after_sync(report)
            except Exception:
                log.exception("mail: tidying up after a sync failed")
            if report.new_inbound:
                self._wake_triage.set()
        return report

    async def _triage_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            done = await self.triage_round(limit=10, stop=stop)
            if done == 0:
                await self._wait(self._wake_triage, stop, TRIAGE_IDLE_SECONDS)

    async def triage_round(self, *, limit: int = 10, stop: asyncio.Event | None = None) -> int:
        done = 0
        for thread_id in await self.triage.pending(limit=limit):
            if stop is not None and stop.is_set():
                break
            try:
                triage = await self.triage.triage_thread(thread_id)
                if triage is not None:
                    done += 1
                    await self.after_triage(triage)
            except Exception:
                log.exception("mail: sorting a thread failed")
        return done

    # --- After a sync --------------------------------------------------------------------

    async def after_sync(self, report: SyncReport) -> None:
        for stored in report.new_outbound:
            if stored.jarvis_action:
                await self._confirm_send(stored)
        for stored in report.stored:
            if not stored.new:
                continue
            if stored.direction == "out" and not stored.jarvis_action:
                await self._withdraw_drafts(stored.thread_id, "You replied from Gmail.")
            elif stored.direction == "in":
                await self._withdraw_drafts(
                    stored.thread_id, "A newer message arrived.", keep_for=stored.message_id
                )

    async def _confirm_send(self, stored: Stored) -> None:
        """A send whose answer was lost turned up in your Sent mail: it went out."""
        try:
            proposal_id = uuid.UUID(str(stored.jarvis_action))
        except ValueError:
            return
        async with transaction(self._s.session_factory) as session:
            proposal = await session.get(ActionProposal, proposal_id)
            message = await session.get(MailMessage, stored.message_id)
            if proposal is None or message is None or proposal.kind != "email.send":
                return
            if sorted(message.to_addresses) != sorted((proposal.payload or {}).get("to") or []):
                log.warning("mail: a sent email names a Jarvis send but not its recipients")
                return
            confirmed = await self._s.policy.confirm_outcome(
                session,
                proposal_id,
                result={"message_id": stored.message_id, "thread_id": stored.thread_id},
                evidence="found in your Sent mail",
            )
        if confirmed or proposal.status == Status.EXECUTED:
            draft_id = (proposal.payload or {}).get("draft_id")
            if draft_id:
                await self.after_send(uuid.UUID(draft_id), message_id=stored.message_id)

    async def _withdraw_drafts(
        self, thread_id: str, reason: str, *, keep_for: str | None = None
    ) -> None:
        async with transaction(self._s.session_factory) as session:
            drafts = list(
                await session.scalars(
                    select(MailDraft)
                    .where(MailDraft.thread_id == thread_id)
                    .where(MailDraft.status == "pending")
                    .with_for_update()
                )
            )
            stale: list[MailDraft] = []
            for draft in drafts:
                if keep_for is not None and draft.reply_to_message_id == keep_for:
                    continue
                if draft.proposal_id is not None:
                    proposal = await session.get(ActionProposal, draft.proposal_id)
                    if proposal is not None and proposal.status not in REPLACEABLE:
                        continue  # approved, going out or maybe sent: your decision stands
                    await self._s.policy.withdraw(session, draft.proposal_id, reason=reason)
                await self._withdraw_mirrors(session, draft.id, reason)
                draft.status, draft.status_reason = "superseded", reason
                draft.updated_at = self._s.clock.now()
                stale.append(draft)
        for draft in stale:
            await self._remove_gmail_copy(draft)

    # --- After triage ----------------------------------------------------------------------

    async def after_triage(self, triage: Triage) -> None:
        access = await self.access()
        if access.level == "full" and await self._autonomy_open():
            label = JARVIS_LABELS[triage.category]
            async with transaction(self._s.session_factory) as session:
                await self._s.policy.propose(
                    session,
                    kind="email.label",
                    payload={"message_id": triage.message_id, "label": label},
                    rationale=f"Sorted as {triage.category.replace('_', ' ')}",
                    created_by="agent:triage",
                )
        try:
            await self.alerts.after_triage(triage)
        except Exception:
            log.exception("mail: an urgent-mail alert failed")
        if not (triage.needs_reply and access.account and self._wants_auto_draft(triage)):
            return
        if await self._stood_down():
            return  # "stand down" means no unprompted work (each draft would ping you)
        async with self._s.session_factory() as session:
            exists = await session.scalar(
                select(MailDraft.id)
                .where(MailDraft.thread_id == triage.thread_id)
                .where(MailDraft.reply_to_message_id == triage.message_id)
                .limit(1)
            )
        if exists is None:
            try:
                await self.draft_reply(triage.thread_id, origin="auto")
            except Exception:
                log.exception("mail: drafting a reply failed")

    def _wants_auto_draft(self, triage: Triage) -> bool:
        rule = self.config.drafting.auto_draft
        if rule == "off" or triage.category in ("suspicious", "newsletter"):
            return False
        return rule == "all" or triage.sender.known or triage.sender.vip

    # --- Drafts ------------------------------------------------------------------------------

    async def draft_reply(
        self,
        thread_id: str,
        *,
        instructions: str | None = None,
        reply_all: bool = False,
        origin: str = "owner",
        write: bool = True,
        actor: str | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> MailDraft | None:
        """Draft a reply to the latest email from someone else in the thread.

        `origin` is "auto" (Jarvis on its own), "owner" (the app) or "chat" (you
        asked by chat, voice or Telegram; `actor` is who relayed it). The send
        proposal joins `conversation_id`, so voice can read it back to you.
        """
        access = await self.access()
        if access.level == "none" or not access.account:
            raise MailError("Connect Google in Sources first.")
        try:
            composed = await self.drafter.compose(
                thread_id,
                owner=access.account,
                instructions=instructions,
                reply_all=reply_all,
                write=write,
            )
        except RouterError as exc:
            raise MailError(
                "No model can draft right now. Try again shortly, or write the reply yourself."
            ) from exc
        if composed is None:
            return None
        await self._withdraw_drafts(thread_id, "Replaced by a new draft.")
        now = self._s.clock.now()
        async with transaction(self._s.session_factory) as session:
            draft = MailDraft(
                id=uuid.uuid4(),
                thread_id=thread_id,
                reply_to_message_id=composed.reply_to_message_id,
                content_enc=self.store.enc_json(composed.fields()),
                origin=origin,
                status="pending",
                created_at=now,
                updated_at=now,
            )
            session.add(draft)
            await session.flush()
            if composed.body:
                try:
                    proposal = await self._propose_send(
                        session,
                        draft,
                        composed.fields(),
                        created_by=actor or ("agent:drafter" if origin == "auto" else "owner"),
                        rationale=self._rationale(composed, origin),
                        conversation_id=conversation_id,
                    )
                except PolicyError as exc:
                    raise MailError(f"Jarvis couldn't prepare this reply: {exc}") from exc
                draft.proposal_id = proposal.id
                draft.original_hash = body_hash(composed.body)
        if composed.body:
            await self._mirror(draft.id, composed.fields())
        return draft

    def _rationale(self, composed: Composed, origin: str) -> str:
        if origin == "auto":
            return "Drafted by Jarvis: the email needs a reply and the sender is someone you know."
        return "The reply you asked for."

    async def _propose_send(
        self,
        session: Any,
        draft: MailDraft,
        fields: dict[str, Any],
        *,
        created_by: str,
        rationale: str,
        conversation_id: uuid.UUID | None = None,
    ) -> ActionProposal:
        payload = {**fields, "draft_id": str(draft.id)}
        return await self._s.policy.propose(
            session,
            kind="email.send",
            payload=payload,
            rationale=rationale,
            created_by=created_by,
            evidence=[{"thread_id": draft.thread_id, "reply_to": draft.reply_to_message_id}],
            conversation_id=conversation_id,
        )

    async def draft_view(self, draft_id: uuid.UUID) -> DraftView:
        async with self._s.session_factory() as session:
            draft = await session.get(MailDraft, draft_id)
            if draft is None:
                raise MailError("That draft doesn't exist.")
            proposal = (
                await session.get(ActionProposal, draft.proposal_id) if draft.proposal_id else None
            )
        return DraftView(draft, self.store.dec_json(draft.content_enc, {}), proposal)

    async def edit_draft(self, draft_id: uuid.UUID, fields: dict[str, Any]) -> DraftView:
        """Your edit replaces the draft (and its proposal) as one new version."""
        unknown = set(fields) - EDITABLE
        if unknown:
            raise MailError(f"These can't be changed: {', '.join(sorted(unknown))}.")
        view = await self.draft_view(draft_id)
        if view.draft.status != "pending":
            raise MailError(f"This draft is {view.draft.status}; start a new reply instead.")
        if view.proposal is not None and view.proposal.status not in (
            Status.PENDING,
            Status.REFUSED,
            *SENDABLE_AGAIN,
        ):
            raise MailError(f"This reply is {view.proposal.status}: it can't be edited now.")
        try:
            clean = EmailSendPayload.model_validate({**view.fields, **fields}).model_dump(
                mode="json", exclude={"draft_id"}
            )
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'email'}: {e['msg']}" for e in exc.errors()
            )
            raise MailError(f"Check the reply: {problems}") from exc
        async with transaction(self._s.session_factory) as session:
            draft = await session.get(MailDraft, draft_id, with_for_update=True)
            assert draft is not None
            if draft.proposal_id is not None:
                await self._s.policy.withdraw(
                    session, draft.proposal_id, reason="Replaced by your edit.", actor="owner"
                )
            draft.content_enc = self.store.enc_json(clean)
            draft.updated_at = self._s.clock.now()
            proposal = await self._propose_send(
                session, draft, clean, created_by="owner", rationale="The reply as you edited it."
            )
            draft.proposal_id = proposal.id
        await self._mirror(draft_id, clean)
        return await self.draft_view(draft_id)

    async def send_draft(self, draft_id: uuid.UUID, *, approved_hash: str) -> ActionProposal:
        """You tapped Send: approve exactly what you saw. The undo window starts now.

        After an Undo (or a rejection, or a send that definitely failed) the same
        reply goes back through the engine as a new request and is approved at once.
        """
        view = await self.draft_view(draft_id)
        if view.draft.status != "pending":
            raise MailError(f"This reply was {view.draft.status}: start a new one.")
        if view.proposal is None:
            raise MailError("Write the reply first.")
        if (await self.access()).level != "full":
            raise MailError("Jarvis can only read your Gmail: give it your inbox in Sources.")
        async with transaction(self._s.session_factory) as session:
            try:
                proposal_id = view.proposal.id
                if view.proposal.status in SENDABLE_AGAIN:
                    draft = await session.get(MailDraft, draft_id, with_for_update=True)
                    assert draft is not None
                    again = await self._propose_send(
                        session, draft, view.fields, created_by="owner", rationale="Sent by you."
                    )
                    draft.proposal_id = proposal_id = again.id
                return await self._s.policy.approve(
                    session, proposal_id, approved_hash=approved_hash, channel=Channel.PWA
                )
            except PolicyError as exc:
                raise MailError(str(exc)) from exc

    async def undo_send(self, draft_id: uuid.UUID) -> ActionProposal:
        view = await self.draft_view(draft_id)
        if view.proposal is None:
            raise MailError("There's nothing to undo.")
        async with transaction(self._s.session_factory) as session:
            try:
                return await self._s.policy.cancel(session, view.proposal.id, channel=Channel.PWA)
            except PolicyError as exc:
                raise MailError(str(exc)) from exc

    async def discard_draft(self, draft_id: uuid.UUID) -> None:
        async with transaction(self._s.session_factory) as session:
            draft = await session.get(MailDraft, draft_id, with_for_update=True)
            if draft is None or draft.status != "pending":
                return
            if draft.proposal_id is not None:
                await self._s.policy.withdraw(
                    session, draft.proposal_id, reason="Discarded by you.", actor="owner"
                )
            await self._withdraw_mirrors(session, draft.id, "Discarded by you.")
            draft.status, draft.status_reason = "discarded", "Discarded by you."
            draft.updated_at = self._s.clock.now()
        await self._remove_gmail_copy(draft)

    async def after_send(self, draft_id: uuid.UUID | None, *, message_id: str) -> None:
        if draft_id is None:
            return
        async with transaction(self._s.session_factory) as session:
            draft = await session.get(MailDraft, draft_id, with_for_update=True)
            if draft is None or draft.status == "sent":
                return
            draft.status, draft.status_reason = "sent", None
            draft.sent_message_id = message_id
            await self._withdraw_mirrors(session, draft.id, "The reply was sent.")
            draft.updated_at = self._s.clock.now()
        await self._remove_gmail_copy(draft)

    # --- The copy in Gmail's Drafts --------------------------------------------------------

    async def _mirror(self, draft_id: uuid.UUID, fields: dict[str, Any]) -> None:
        """Ask for the draft to appear in Gmail too (email.draft), once autonomy is on."""
        access = await self.access()
        if access.level != "full" or not await self._autonomy_open():
            return
        async with transaction(self._s.session_factory) as session:
            await self._withdraw_mirrors(session, draft_id, "Replaced by a newer version.")
            await self._s.policy.propose(
                session,
                kind="email.draft",
                payload={**fields, "draft_id": str(draft_id)},
                rationale="So you can see the reply in Gmail too. Nothing is sent.",
                created_by="agent:drafter",
            )

    async def _withdraw_mirrors(self, session: Any, draft_id: uuid.UUID, reason: str) -> None:
        """Take back "save it in Gmail" requests for this reply that nobody decided on."""
        undecided = await session.scalars(
            select(ActionProposal.id)
            .where(ActionProposal.kind == "email.draft")
            .where(ActionProposal.status == Status.PENDING)
            .where(ActionProposal.payload["draft_id"].astext == str(draft_id))
        )
        for proposal_id in list(undecided):
            await self._s.policy.withdraw(session, proposal_id, reason=reason)

    async def mirror_draft(self, payload: EmailDraftPayload) -> dict[str, Any]:
        """The email.draft executor: create or update the Gmail copy, never over your edits."""
        async with self._s.session_factory() as session:
            draft = await session.get(MailDraft, payload.draft_id)
        if draft is None or draft.status != "pending":
            return {"skipped": "The reply was already sent or set aside."}
        access = await self.access()
        if access.level != "full" or not access.account:
            raise RuntimeError("Jarvis can't write to your Gmail drafts yet: reconnect Google.")
        client = self.client()
        raw = to_raw(
            payload.mime(sender=access.account, sender_name=await self.owner_name(), action_id=None)
        )
        if draft.gmail_draft_id:
            current = await client.get_draft(draft.gmail_draft_id)
            if (
                current is not None
                and draft.gmail_body_hash
                and (self._draft_hash(current) != draft.gmail_body_hash)
            ):
                return {"skipped": "You changed the copy in Gmail, so Jarvis left it as it is."}
            saved = (
                await client.update_draft(draft.gmail_draft_id, raw, payload.thread_id)
                if current is not None
                else await client.create_draft(raw, payload.thread_id)
            )
        else:
            saved = await client.create_draft(raw, payload.thread_id)
        gmail_id = str(saved["id"])
        stored = await client.get_draft(gmail_id)
        async with transaction(self._s.session_factory) as session:
            row = await session.get(MailDraft, payload.draft_id, with_for_update=True)
            if row is not None:
                row.gmail_draft_id = gmail_id
                row.gmail_body_hash = self._draft_hash(stored) if stored else None
        return {"gmail_draft_id": gmail_id}

    def _draft_hash(self, resource: dict[str, Any]) -> str:
        return body_hash(parse_gmail_message(resource["message"]).body)

    async def _remove_gmail_copy(self, draft: MailDraft) -> None:
        """Delete Jarvis's copy in Gmail's Drafts, unless you've changed it there."""
        if not draft.gmail_draft_id:
            return
        try:
            client = self.client()
            current = await client.get_draft(draft.gmail_draft_id)
            if current is not None and self._draft_hash(current) == draft.gmail_body_hash:
                await client.delete_draft(draft.gmail_draft_id)
        except (GmailError, httpx.HTTPError):
            log.warning("mail: couldn't tidy Jarvis's draft copy in Gmail", exc_info=True)

    # --- Housekeeping ------------------------------------------------------------------------

    async def purge_old_bodies(self) -> int:
        """Forget email text past `body_retention_days` (Gmail keeps it; Jarvis refetches it)."""
        cutoff = self._s.clock.now() - timedelta(days=self.config.sync.body_retention_days)
        async with transaction(self._s.session_factory) as session:
            return await self.store.purge_bodies(session, older_than=cutoff)

    # --- For the app ---------------------------------------------------------------------------

    async def thread(self, thread_id: str) -> MailThread:
        async with self._s.session_factory() as session:
            thread = await session.get(MailThread, thread_id)
        if thread is None:
            raise MailError("That conversation isn't in Jarvis.")
        return thread
