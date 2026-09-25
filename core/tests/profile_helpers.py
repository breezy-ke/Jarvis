"""A complete profile, and signing it off so Jarvis may act on its own."""

from __future__ import annotations

from typing import Any

from jarvis.db.session import transaction
from jarvis.services import Services

FULL_PROFILE: dict[str, Any] = {
    "identity.preferred_name": "Brian",
    "identity.location": "Nairobi, Kenya",
    "identity.timezone": "Africa/Nairobi",
    "business.name": "Breezy Digital",
    "business.services": ["Web design", "Web apps"],
    "business.rate_card": [{"service": "Landing page", "price": "80000", "currency": "KES"}],
    "clients.ideal_clients": [
        {"play": "ea_smes", "description": "Nairobi SMEs with outdated sites"}
    ],
    "portfolio.highlights": [{"title": "Clinic site", "summary": "Booking + SEO"}],
    "engineering.primary_stacks": ["Next.js", "Laravel"],
    "engineering.conventions": ["TypeScript strict"],
    "design.style_keywords": ["minimal"],
    "design.liked_references": ["linear.app: calm typography"],
    "communication.tone": "warm and concise",
    "communication.sign_off": "Best regards",
    "schedule.working_hours": "Mon-Fri 08:00-18:00",
    "goals.business_goals": ["3 new retainers this quarter"],
    "people.contacts": [
        {"name": "Achieng", "email": "Achieng@Client.co.ke", "relationship": "client", "vip": True}
    ],
    "boundaries.always_ask_before": ["sending any email"],
    "boundaries.never_do": ["pay anyone"],
    "assistant.personality": "dry British wit",
}


async def open_autonomy(services: Services) -> None:
    """Fill in the profile and sign it off: the autonomy gate opens."""
    async with transaction(services.session_factory) as session:
        await services.profiles.update(session, FULL_PROFILE, created_by="owner")
        await services.profiles.sign_off(session)
