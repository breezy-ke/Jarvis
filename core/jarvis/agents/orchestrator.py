"""Jarvis's main conversational agent.

The model is chosen per call by the router, so the agent has no fixed model.
Its tools are deliberately narrow: memory, status, capabilities, and
`propose_action`, the only path to the outside world, gated by policy.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic_ai import Agent, RunContext
from sqlalchemy import func, select

from jarvis.agents.tools import AgentDeps, audit_tool
from jarvis.db.models import ActionProposal
from jarvis.db.session import transaction
from jarvis.memory.retrieval import search_episodes, search_facts
from jarvis.memory.store import CATEGORIES, FactInput, FactStatus, MemoryStoreError
from jarvis.policy.engine import PolicyError
from jarvis.policy.state import get_kill_switch
from jarvis.policy.types import Status
from jarvis.profile.service import completeness

UPCOMING_CAPABILITIES = {
    "Email (read, draft, send with approval)": "Phase 3",
    "Voice conversation and 'Hey Jarvis' wake word": "Phase 2",
    "Daily tech brief and web research": "Phase 4",
    "Lead generation and outreach": "Phase 5",
    "Website/app builder (UI Studio)": "Phase 6",
    "Calendar, reminders, proposals and invoices": "Phase 7",
}


def build_orchestrator(persona: str) -> Agent[AgentDeps, str]:
    agent: Agent[AgentDeps, str] = Agent(
        deps_type=AgentDeps, instructions=persona, name="jarvis", retries=2
    )

    @agent.tool
    async def search_memory(ctx: RunContext[AgentDeps], query: str) -> str:
        """Search long-term memory about the owner: facts and past conversations.

        Args:
            query: What to look for, in natural language.
        """
        services = ctx.deps.services
        async with services.session_factory() as session:
            facts = await search_facts(session, services.embedder, query, now=services.clock.now())
            episodes = await search_episodes(session, services.embedder, query)
        await audit_tool(ctx.deps, "search_memory", "Searched memory", {"hits": len(facts)})
        lines = [
            f"- (id {f.fact.id}) [{f.fact.category}] {f.fact.subject} — {f.fact.predicate}: "
            f"{f.fact.value}" + ("" if f.fact.status == FactStatus.CONFIRMED else " (inferred)")
            for f in facts
        ]
        lines += [f"- (past conversation) {e.episode.summary}" for e in episodes if e.score > 0.2]
        return "\n".join(lines) if lines else "Nothing relevant in memory."

    @agent.tool
    async def remember(
        ctx: RunContext[AgentDeps],
        subject: str,
        predicate: str,
        value: str,
        category: str = "note",
    ) -> str:
        """Save something the owner EXPLICITLY asked you to remember.

        Args:
            subject: Who or what it is about, e.g. "owner" or "Acme Ltd".
            predicate: The relationship, e.g. "prefers", "budget", "birthday".
            value: The fact itself.
            category: One of identity, business, client, project, preference,
                contact, schedule, goal, skill, note.
        """
        services = ctx.deps.services
        if category not in CATEGORIES or category == "profile_suggestion":
            category = "note"
        try:
            async with transaction(services.session_factory) as session:
                await services.memory.add(
                    session,
                    FactInput(
                        category=category,
                        subject=subject,
                        predicate=predicate,
                        value=value,
                        source="conversation",
                        source_ref=str(ctx.deps.conversation_id)
                        if ctx.deps.conversation_id
                        else None,
                        status=FactStatus.CONFIRMED,
                        confidence=1.0,
                    ),
                    actor=ctx.deps.actor,
                )
        except MemoryStoreError as exc:
            return f"Could not save that: {exc}"
        return "Saved to memory."

    @agent.tool
    async def forget(ctx: RunContext[AgentDeps], fact_ids: list[str]) -> str:
        """Forget specific memories. First call search_memory to find their ids.

        Args:
            fact_ids: The ids shown by search_memory.
        """
        services = ctx.deps.services
        forgotten = 0
        async with transaction(services.session_factory) as session:
            for raw in fact_ids[:20]:
                try:
                    fact_id = uuid.UUID(raw)
                except ValueError:
                    continue
                if await services.memory.forget(session, fact_id, hard=False, actor=ctx.deps.actor):
                    forgotten += 1
        return (
            f"Forgot {forgotten} item(s). They are hidden now and erased permanently within 7 days."
        )

    @agent.tool
    async def propose_action(
        ctx: RunContext[AgentDeps], kind: str, payload: dict[str, Any], rationale: str
    ) -> str:
        """Propose an action in the outside world (notify, send, deploy...).

        Your owner's policies decide whether it runs automatically or waits
        for approval. Never claim an action happened unless this tool says it ran.

        Args:
            kind: The action kind, e.g. "notify.owner". See list_capabilities.
            payload: The action's parameters.
            rationale: One sentence on why, shown to the owner.
        """
        services = ctx.deps.services
        try:
            async with transaction(services.session_factory) as session:
                proposal = await services.policy.propose(
                    session,
                    kind=kind,
                    payload=payload,
                    rationale=rationale,
                    created_by=ctx.deps.actor,
                    conversation_id=ctx.deps.conversation_id,
                )
        except PolicyError as exc:
            return f"Not possible: {exc}"
        status = proposal.status
        if status == Status.APPROVED:
            return f"Approved by policy; it will run shortly ({proposal.summary})."
        if status == Status.PENDING:
            reason = f" Note: {proposal.status_reason}" if proposal.status_reason else ""
            return (
                f"Waiting for the owner's approval in the Approvals tab ({proposal.summary})."
                f"{reason}"
            )
        if status == Status.DRAFT_ONLY:
            return "Saved as a draft only: policy doesn't allow Jarvis to do this."
        return f"Refused: {proposal.status_reason}"

    @agent.tool
    async def get_status(ctx: RunContext[AgentDeps]) -> str:
        """Jarvis's own status: pending approvals, kill switch, autonomy and profile."""
        services = ctx.deps.services
        async with services.session_factory() as session:
            pending = await session.scalar(
                select(func.count())
                .select_from(ActionProposal)
                .where(ActionProposal.status == Status.PENDING)
            )
            kill = await get_kill_switch(session)
            gate = await services.profiles.status(session)
            snapshot = await services.profiles.current(session)
        score = completeness(snapshot.profile).score
        return (
            f"Pending approvals: {pending or 0}. Kill switch: {'ON' if kill.engaged else 'off'}. "
            f"Autonomy: {'on' if gate.open else 'off'} ({gate.reason}). "
            f"Profile {score:.0%} complete."
        )

    @agent.tool
    async def list_capabilities(ctx: RunContext[AgentDeps]) -> str:
        """What Jarvis can do right now, and what is coming in later phases."""
        services = ctx.deps.services
        now = [f"- {kind}" for kind in services.registry.kinds()]
        later = [f"- {name} ({phase})" for name, phase in UPCOMING_CAPABILITIES.items()]
        return (
            "Actions available now:\n"
            + "\n".join(now)
            + "\nAlso available: chat, memory (remember/forget/search), onboarding, status.\n"
            + "Coming later:\n"
            + "\n".join(later)
        )

    return agent
