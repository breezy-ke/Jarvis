from __future__ import annotations

import json
from typing import Any

import pytest
import respx
from pydantic import BaseModel
from sqlalchemy import select

from jarvis.api.app import create_app
from jarvis.api.deps import AppState
from jarvis.clock import FrozenClock
from jarvis.db.models import ActionProposal
from jarvis.db.session import SessionFactory, transaction
from jarvis.memory.embeddings import HashEmbedder
from jarvis.policy.registry import ActionSpec, ExecutionContext
from jarvis.services import Services, build_services
from tests.conftest import fake_models_config, make_settings
from tests.integration.api_helpers import Harness, login, register, setup_token, step_up

pytestmark = pytest.mark.db


def sse_events(body: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = line.split(":", 1)[1].strip()
        if name and data:
            events.append((name, json.loads(data)))
    return events


# --- Authentication ------------------------------------------------------------------------


async def test_first_run_requires_the_setup_code(harness: Harness) -> None:
    status = (await harness.client.get("/api/auth/status")).json()
    assert status == {"setup_complete": False, "authenticated": False}
    for token in (None, "wrong-code"):
        resp = await harness.client.post("/api/auth/register/options", json={"setup_token": token})
        assert resp.status_code == 403
    codes = await register(harness)
    assert len(codes) == 10
    assert (await harness.client.get("/api/auth/status")).json()["authenticated"]


async def test_nobody_else_can_register_after_setup(harness: Harness) -> None:
    await register(harness)
    await harness.client.post("/api/auth/logout")
    harness.client.cookies.clear()
    resp = await harness.client.post(
        "/api/auth/register/options", json={"setup_token": await setup_token(harness)}
    )
    assert resp.status_code == 403
    assert "already set up" in resp.json()["detail"]


async def test_login_logout_and_protected_routes(harness: Harness) -> None:
    await register(harness)
    await harness.client.post("/api/auth/logout")
    harness.client.cookies.clear()
    assert (await harness.client.get("/api/status")).status_code == 401
    await login(harness)
    status = (await harness.client.get("/api/status")).json()
    assert status["autonomy"]["open"] is False
    assert status["pending_approvals"] == 0


async def test_passkey_from_another_origin_is_rejected(harness: Harness) -> None:
    await register(harness)
    harness.client.cookies.clear()
    options = (await harness.client.post("/api/auth/login/options")).json()
    phished = harness.authenticator.assertion(options["options"], origin="https://evil.example")
    resp = await harness.client.post(
        "/api/auth/login/verify",
        json={"challenge_id": options["challenge_id"], "credential": phished},
    )
    assert resp.status_code == 401


async def test_challenges_are_single_use(harness: Harness) -> None:
    await register(harness)
    options = (await harness.client.post("/api/auth/login/options")).json()
    body = {
        "challenge_id": options["challenge_id"],
        "credential": harness.authenticator.assertion(options["options"]),
    }
    assert (await harness.client.post("/api/auth/login/verify", json=body)).status_code == 200
    replay = await harness.client.post("/api/auth/login/verify", json=body)
    assert replay.status_code == 400


async def test_recovery_codes(harness: Harness) -> None:
    codes = await register(harness)
    harness.client.cookies.clear()
    assert (
        await harness.client.post("/api/auth/recovery", json={"code": "AAAA-BBBB-CCCC"})
    ).status_code == 401
    assert (
        await harness.client.post("/api/auth/recovery", json={"code": codes[0].lower()})
    ).status_code == 200
    harness.client.cookies.clear()
    assert (
        await harness.client.post("/api/auth/recovery", json={"code": codes[0]})
    ).status_code == 401


async def test_cross_site_writes_are_blocked(harness: Harness) -> None:
    await register(harness)
    for headers in ({"Origin": "https://evil.example"}, {"Origin": ""}):
        resp = await harness.client.post(
            "/api/system/kill-switch", json={"engaged": True}, headers=headers
        )
        assert resp.status_code == 403


async def test_security_headers(harness: Harness) -> None:
    resp = await harness.client.get("/api/health")
    assert resp.status_code == 200
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert "img-src 'self' data: blob:" in resp.headers["content-security-policy"]
    # The voice socket is allowed by name (older Safari doesn't count it as 'self').
    assert "connect-src 'self' ws://localhost:8080;" in resp.headers["content-security-policy"]
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["cache-control"] == "no-store"


# --- Chat ---------------------------------------------------------------------------------


async def test_chat_streams_over_sse(harness: Harness) -> None:
    await register(harness)
    resp = await harness.client.post("/api/chat", json={"text": "Good morning"})
    events = sse_events(resp.text)
    names = [e[0] for e in events]
    assert names[0] == "start"
    assert names[-1] == "done"
    assert "You said: Good morning" in events[-1][1]["text"]
    conversations = (await harness.client.get("/api/conversations")).json()
    assert len(conversations) == 1
    messages = (
        await harness.client.get(f"/api/conversations/{conversations[0]['id']}/messages")
    ).json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    missing = await harness.client.post(
        "/api/chat", json={"text": "hi", "conversation_id": "00000000-0000-0000-0000-000000000000"}
    )
    assert missing.status_code == 404


# --- Approvals --------------------------------------------------------------------------------


async def _propose(h: Harness, kind: str, payload: dict[str, Any]) -> ActionProposal:
    async with transaction(h.services.session_factory) as session:
        return await h.services.policy.propose(
            session, kind=kind, payload=payload, rationale="test", created_by="agent:test"
        )


async def test_approved_notification_runs(harness: Harness) -> None:
    await register(harness)
    proposal = await _propose(harness, "notify.owner", {"title": "Hi", "body": "Test"})
    assert proposal.status == "pending"  # autonomy is off until the profile is signed off
    listed = (await harness.client.get("/api/actions")).json()
    assert [a["id"] for a in listed] == [str(proposal.id)]
    wrong = await harness.client.post(
        f"/api/actions/{proposal.id}/approve", json={"payload_hash": "0" * 64}
    )
    assert wrong.status_code == 409
    ok = await harness.client.post(
        f"/api/actions/{proposal.id}/approve", json={"payload_hash": proposal.payload_hash}
    )
    assert ok.json()["status"] == "approved"
    await harness.services.policy.execute_due(harness.services.session_factory)
    done = (await harness.client.get(f"/api/actions/{proposal.id}")).json()
    assert done["status"] == "executed"
    assert done["result"]["delivered"] == 0  # push isn't configured in tests


class InvitePayload(BaseModel):
    title: str
    attendees: list[str]


async def test_undo_inside_the_window(harness: Harness) -> None:
    ran: list[str] = []

    async def invite(ctx: ExecutionContext, payload: InvitePayload) -> dict[str, Any]:
        ran.append(payload.title)
        return {}

    harness.services.registry.register(
        ActionSpec("calendar.invite", InvitePayload, invite, lambda p: f"Invite: {p.title}")
    )
    await register(harness)
    proposal = await _propose(
        harness, "calendar.invite", {"title": "Kickoff", "attendees": ["client@acme.co.ke"]}
    )
    assert "New recipient" in (proposal.status_reason or "")
    ok = await harness.client.post(
        f"/api/actions/{proposal.id}/approve", json={"payload_hash": proposal.payload_hash}
    )
    assert ok.json()["status"] == "approved"
    harness.clock.advance(seconds=20)
    undone = await harness.client.post(f"/api/actions/{proposal.id}/undo")
    assert undone.json()["status"] == "cancelled"
    harness.clock.advance(minutes=5)
    await harness.services.policy.execute_due(harness.services.session_factory)
    assert ran == []
    history = (await harness.client.get("/api/actions", params={"view": "history"})).json()
    assert history[0]["status"] == "cancelled"


class DeployPayload(BaseModel):
    project: str


async def test_high_risk_approval_needs_a_fresh_passkey_tap(harness: Harness) -> None:
    ran: list[str] = []

    async def deploy(ctx: ExecutionContext, payload: DeployPayload) -> dict[str, Any]:
        ran.append(payload.project)
        return {"url": "https://example.com"}

    harness.services.registry.register(
        ActionSpec("deploy.production", DeployPayload, deploy, lambda p: f"Deploy {p.project}")
    )
    await register(harness)
    proposal = await _propose(harness, "deploy.production", {"project": "clinic-site"})
    assert proposal.status == "pending"
    body = {"payload_hash": proposal.payload_hash}
    plain = await harness.client.post(f"/api/actions/{proposal.id}/approve", json=body)
    assert "pwa_passkey" in plain.json()["detail"]
    no_tap = await harness.client.post(
        f"/api/actions/{proposal.id}/approve", json={**body, "use_passkey": True}
    )
    assert "Confirm with your passkey" in no_tap.json()["detail"]
    await step_up(harness)
    harness.clock.advance(minutes=3)  # the tap went stale
    stale = await harness.client.post(
        f"/api/actions/{proposal.id}/approve", json={**body, "use_passkey": True}
    )
    assert stale.status_code == 409
    await step_up(harness)
    approved = await harness.client.post(
        f"/api/actions/{proposal.id}/approve", json={**body, "use_passkey": True}
    )
    assert approved.json()["status"] == "approved"
    harness.clock.advance(minutes=2)
    await harness.services.policy.execute_due(harness.services.session_factory)
    assert ran == ["clinic-site"]


# --- Profile, memory, onboarding ---------------------------------------------------------------


async def test_profile_editing_and_sign_off(harness: Harness) -> None:
    await register(harness)
    bad = await harness.client.patch("/api/profile", json={"changes": {"identity.shoe_size": "44"}})
    assert bad.status_code == 422
    good = await harness.client.patch(
        "/api/profile", json={"changes": {"identity.preferred_name": "Brian"}}
    )
    assert good.json()["data"]["identity"]["preferred_name"] == "Brian"
    assert (await harness.client.post("/api/profile/sign-off")).status_code == 409
    versions = (await harness.client.get("/api/profile/versions")).json()
    assert versions[0]["version"] == 1


async def test_memory_crud_and_export(harness: Harness) -> None:
    await register(harness)
    created = await harness.client.post(
        "/api/memory/facts",
        json={"category": "client", "subject": "Acme", "predicate": "budget", "value": "KES 300k"},
    )
    fact_id = created.json()["id"]
    assert created.json()["status"] == "confirmed"
    edited = await harness.client.patch(f"/api/memory/facts/{fact_id}", json={"value": "KES 350k"})
    new_id = edited.json()["id"]
    listing = (await harness.client.get("/api/memory/facts", params={"q": "acme"})).json()
    assert [f["value"] for f in listing] == ["KES 350k"]
    export = await harness.client.get("/api/memory/export")
    assert "attachment" in export.headers["content-disposition"]
    assert "token" not in export.text.lower()
    assert (await harness.client.delete(f"/api/memory/facts/{new_id}")).status_code == 200
    assert (await harness.client.delete(f"/api/memory/facts/{new_id}")).status_code == 404


async def test_onboarding_over_http(harness: Harness) -> None:
    await register(harness)
    overview = (await harness.client.get("/api/onboarding")).json()
    assert len(overview["modules"]) == 12
    stream = await harness.client.post("/api/onboarding/identity/message", json={"text": None})
    assert sse_events(stream.text)[-1][0] == "done"
    transcript = (await harness.client.get("/api/onboarding/identity/transcript")).json()
    assert transcript[0]["role"] == "assistant"
    assert (await harness.client.post("/api/onboarding/nope/skip")).status_code == 404


async def test_ingestion_consent_and_upload(harness: Harness) -> None:
    await register(harness)
    sources = (await harness.client.get("/api/ingestion/sources")).json()
    assert {s["id"] for s in sources} == {
        "gmail_style",
        "calendar",
        "github",
        "website",
        "documents",
    }
    blocked = await harness.client.post(
        "/api/ingestion/documents/upload", files={"file": ("cv.txt", b"hello", "text/plain")}
    )
    assert blocked.status_code == 409
    await harness.client.post("/api/ingestion/documents/consent", json={"consent": True})
    uploaded = await harness.client.post(
        "/api/ingestion/documents/upload",
        files={"file": ("cv.txt", b"Full-stack developer", "text/plain")},
    )
    assert uploaded.status_code == 200, uploaded.text


async def test_google_callback_rejects_forged_state(harness: Harness) -> None:
    await register(harness)
    status = (await harness.client.get("/api/integrations/google")).json()
    assert status == {**status, "configured": False, "connected": False}
    resp = await harness.client.get(
        "/api/integrations/google/callback", params={"state": "forged", "code": "x"}
    )
    assert resp.status_code == 400
    assert "Couldn&#x27;t connect Google" in resp.text  # escaped HTML
    assert "<script" not in resp.text


# --- System ------------------------------------------------------------------------------------


async def test_kill_switch_models_policies_audit(harness: Harness) -> None:
    await register(harness)
    engaged = await harness.client.post(
        "/api/system/kill-switch", json={"engaged": True, "reason": "test"}
    )
    assert engaged.json()["engaged"] is True
    assert (await harness.client.get("/api/status")).json()["kill_switch"]["engaged"] is True
    models = (await harness.client.get("/api/system/models")).json()
    assert models["tasks"]["chat"]["candidates"][0]["available"] is True
    policies = (await harness.client.get("/api/system/policies")).json()
    assert policies["action_kinds"]["notify.owner"]["available"] is True
    assert policies["action_kinds"]["email.send"]["available"] is False
    audit = (await harness.client.get("/api/audit")).json()
    assert audit[0]["event_type"] == "system.kill_switch"
    assert (await harness.client.get("/api/audit/verify")).json()["ok"] is True


async def test_push_subscription_validation(harness: Harness) -> None:
    await register(harness)
    bad = await harness.client.post(
        "/api/push/subscribe",
        json={"endpoint": "http://insecure.example/x", "keys": {"p256dh": "a", "auth": "b"}},
    )
    assert bad.status_code == 422
    good = await harness.client.post(
        "/api/push/subscribe",
        json={"endpoint": "https://push.example/abc", "keys": {"p256dh": "a", "auth": "b"}},
    )
    assert good.json() == {"ok": True}
    info = (await harness.client.get("/api/push/public-key")).json()
    assert info["configured"] is False


async def test_crash_recovery_runs_at_startup(
    session_factory: SessionFactory, clock: FrozenClock
) -> None:
    settings = make_settings(JARVIS_ENABLE_SCHEDULER=False)

    def factory(s: Any, sf: SessionFactory) -> Services:
        return build_services(
            s, sf, clock=clock, embedder=HashEmbedder(), models_config=fake_models_config()
        )

    app = create_app(settings, services_factory=factory, run_background=False)
    async with app.router.lifespan_context(app):
        state: AppState = app.state.jarvis
        async with transaction(state.services.session_factory) as session:
            proposal = await state.services.policy.propose(
                session,
                kind="notify.owner",
                payload={"title": "a", "body": "b"},
                rationale="x",
                created_by="t",
            )
            proposal.status = "executing"
    app2 = create_app(settings, services_factory=factory, run_background=False)
    async with app2.router.lifespan_context(app2), session_factory() as session:
        row = await session.scalar(select(ActionProposal))
    assert row is not None
    assert row.status == "unknown_outcome"


async def test_github_ingestion_over_http(harness: Harness) -> None:
    await register(harness)
    await harness.client.post(
        "/api/ingestion/github/consent", json={"consent": True, "settings": {"username": "octo"}}
    )
    with respx.mock(assert_all_called=True) as mock:
        mock.get(url__regex=r"^https://api\.github\.com/users/octo/repos").respond(200, json=[])
        resp = await harness.client.post("/api/ingestion/github/run")
    assert resp.status_code == 200, resp.text
    assert resp.json()["repos_scanned"] == 0
