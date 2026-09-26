"""The Inbox: your mail as Jarvis sorted it, and replies you approve.

Email text is served as plain text only (HTML was reduced to what a reader sees
when it arrived), so the app never loads anything from the web for an email.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field

from jarvis.api.actions import serialize
from jarvis.api.deps import Owner, State
from jarvis.mail.digest import render
from jarvis.mail.inbox import TABS, ThreadItem
from jarvis.mail.service import DraftView, MailError, MailNotFound, MailService, ThreadView
from jarvis.mail.signals import HARD, LABELS
from jarvis.mail.triage import Category

router = APIRouter(prefix="/api/mail", tags=["mail"])


def _mail(state: State) -> MailService:
    if state.mail is None:
        raise HTTPException(status_code=503, detail="Email isn't running.")
    return state.mail


def _fail(exc: MailError) -> HTTPException:
    return HTTPException(status_code=404 if isinstance(exc, MailNotFound) else 409, detail=str(exc))


def _signals(signals: list[str]) -> list[dict[str, Any]]:
    return [{"id": s, "label": LABELS.get(s, s), "hard": s in HARD} for s in signals]


def thread_item(item: ThreadItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "subject": item.subject,
        "sender": item.sender,
        "sender_address": item.sender_address,
        "category": item.category,
        "priority": item.priority,
        "needs_reply": item.needs_reply,
        "unread": item.unread,
        "in_inbox": item.in_inbox,
        "summary": item.summary,
        "signals": _signals(item.signals),
        "last_message_at": item.last_message_at.isoformat(),
    }


def draft_json(view: DraftView) -> dict[str, Any]:
    fields = view.fields
    original = view.original
    return {
        "id": str(view.draft.id),
        "thread_id": view.draft.thread_id,
        "status": view.draft.status,
        "status_reason": view.draft.status_reason,
        "origin": view.draft.origin,
        "fields": {key: fields.get(key) for key in ("to", "cc", "bcc", "subject", "body")},
        "original_body": original.get("body") if original else None,
        "in_gmail": bool(view.draft.gmail_draft_id),
        "proposal": serialize(view.proposal) if view.proposal is not None else None,
    }


def thread_json(view: ThreadView) -> dict[str, Any]:
    return {
        **thread_item(view.item),
        "jarvis_category": view.jarvis_category,
        "checked": view.checked,
        "tasks": view.tasks,
        "dates": view.dates,
        "messages": [
            {
                "id": m.id,
                "direction": m.direction,
                "sender": m.sender,
                "sender_address": m.sender_address,
                "to": m.to,
                "cc": m.cc,
                "date": m.date.isoformat(),
                "subject": m.subject,
                "body": m.body,
                "attachments": m.attachments,
            }
            for m in view.messages
        ],
        "draft": draft_json(view.draft) if view.draft is not None else None,
    }


# --- Status and sync ------------------------------------------------------------------------


@router.get("/status")
async def status(owner: Owner, state: State) -> dict[str, Any]:
    mail = _mail(state)
    access = await mail.access()
    sync = await mail.sync.state()
    counts = await mail.inbox.counts() if access.level != "none" else dict.fromkeys(TABS, 0)
    digest = await mail.alerts.latest()
    digest_json = None
    if digest is not None:
        title, short, text = render(digest, state.services.policies_config.tz)
        digest_json = {
            "kind": digest.kind,
            "created_at": digest.created_at,
            "title": title,
            "short": short,
            "text": text,
        }
    return {
        "access": access.level,
        "account": access.account,
        "can_add_holds": access.can_add_holds,
        "sync": {
            "status": sync.status,
            "error": sync.error,
            "last_sync_at": sync.last_sync_at,
            "last_full_sync_at": sync.last_full_sync_at,
        },
        "counts": counts,
        "digest": digest_json,
        "rules": {
            "auto_draft": mail.config.drafting.auto_draft,
            "alerts": mail.config.alerts.urgent,
        },
    }


@router.post("/sync", status_code=202)
async def sync_now(owner: Owner, state: State) -> dict[str, bool]:
    _mail(state).check_now()
    return {"ok": True}


# --- Threads ----------------------------------------------------------------------------------


@router.get("/threads")
async def threads(
    owner: Owner,
    state: State,
    tab: str = Query(default="attention", pattern="^(" + "|".join(TABS) + ")$"),
    before: datetime | None = None,
    limit: int = Query(default=30, ge=1, le=100),
) -> list[dict[str, Any]]:
    items = await _mail(state).inbox.threads(tab, limit=limit, before=before)
    return [thread_item(item) for item in items]


@router.get("/threads/{thread_id}")
async def thread(thread_id: str, owner: Owner, state: State) -> dict[str, Any]:
    try:
        return thread_json(await _mail(state).thread_view(thread_id[:64]))
    except MailError as exc:
        raise _fail(exc) from exc


class CategoryIn(BaseModel):
    category: Category


@router.post("/threads/{thread_id}/category")
async def set_category(
    thread_id: str, body: CategoryIn, owner: Owner, state: State
) -> dict[str, Any]:
    mail = _mail(state)
    try:
        await mail.record_verdict(thread_id[:64], body.category)
        return thread_json(await mail.thread_view(thread_id[:64]))
    except MailError as exc:
        raise _fail(exc) from exc


# --- Drafts -------------------------------------------------------------------------------------


class DraftIn(BaseModel):
    instructions: str | None = Field(default=None, max_length=1_000)
    reply_all: bool = False
    write: bool = True  # False: an empty reply for you to write


@router.post("/threads/{thread_id}/draft")
async def draft(thread_id: str, body: DraftIn, owner: Owner, state: State) -> dict[str, Any]:
    mail = _mail(state)
    try:
        created = await mail.draft_reply(
            thread_id[:64],
            instructions=body.instructions,
            reply_all=body.reply_all,
            write=body.write,
            origin="owner",
        )
        if created is None:
            raise MailError("There's no email from someone else here to reply to.")
        return draft_json(await mail.draft_view(created.id))
    except MailError as exc:
        raise _fail(exc) from exc


class DraftEditIn(BaseModel):
    to: list[str] | None = Field(default=None, max_length=20)
    cc: list[str] | None = Field(default=None, max_length=20)
    bcc: list[str] | None = Field(default=None, max_length=20)
    subject: str | None = Field(default=None, max_length=300)
    body: str | None = Field(default=None, max_length=20_000)


@router.patch("/drafts/{draft_id}")
async def edit_draft(
    draft_id: uuid.UUID, body: DraftEditIn, owner: Owner, state: State
) -> dict[str, Any]:
    try:
        view = await _mail(state).edit_draft(draft_id, body.model_dump(exclude_none=True))
        return draft_json(view)
    except MailError as exc:
        raise _fail(exc) from exc


class SendIn(BaseModel):
    payload_hash: str = Field(min_length=64, max_length=64)


@router.post("/drafts/{draft_id}/send")
async def send(draft_id: uuid.UUID, body: SendIn, owner: Owner, state: State) -> dict[str, Any]:
    mail = _mail(state)
    try:
        await mail.send_draft(draft_id, approved_hash=body.payload_hash)
        return draft_json(await mail.draft_view(draft_id))
    except MailError as exc:
        raise _fail(exc) from exc


@router.post("/drafts/{draft_id}/undo")
async def undo(draft_id: uuid.UUID, owner: Owner, state: State) -> dict[str, Any]:
    mail = _mail(state)
    try:
        await mail.undo_send(draft_id)
        return draft_json(await mail.draft_view(draft_id))
    except MailError as exc:
        raise _fail(exc) from exc


@router.delete("/drafts/{draft_id}", status_code=204)
async def discard(draft_id: uuid.UUID, owner: Owner, state: State) -> Response:
    await _mail(state).discard_draft(draft_id)
    return Response(status_code=204)


# --- Calendar holds ----------------------------------------------------------------------------


class HoldIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    start: str = Field(min_length=10, max_length=40)
    end: str | None = Field(default=None, max_length=40)
    all_day: bool = False
    thread_id: str | None = Field(default=None, max_length=64)


@router.post("/holds")
async def hold(body: HoldIn, owner: Owner, state: State) -> dict[str, Any]:
    try:
        proposal = await _mail(state).propose_hold(
            title=body.title,
            start=body.start,
            end=body.end,
            all_day=body.all_day,
            thread_id=body.thread_id,
        )
    except MailError as exc:
        raise _fail(exc) from exc
    return serialize(proposal)
