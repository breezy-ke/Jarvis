"""The twelve onboarding modules: what Jarvis learns about you, and in what order."""

from __future__ import annotations

from dataclasses import dataclass

from jarvis.profile.schema import REQUIRED_FIELDS


@dataclass(frozen=True)
class OnboardingModule:
    id: str
    title: str
    goal: str
    fields: tuple[str, ...]
    opening: str
    guidance: str
    optional: bool = False

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(f for f in self.fields if f in REQUIRED_FIELDS)


MODULES: tuple[OnboardingModule, ...] = (
    OnboardingModule(
        id="identity",
        title="About you (and me)",
        goal="How to address the owner, where they are, and how Jarvis itself should come across.",
        fields=(
            "identity.preferred_name",
            "identity.full_name",
            "identity.pronouns",
            "identity.location",
            "identity.timezone",
            "identity.languages",
            "assistant.personality",
            "assistant.verbosity",
            "assistant.voice",
        ),
        opening="Let's start simple: what should I call you?",
        guidance=(
            "Ask for the preferred form of address first. "
            "Pronouns are optional: offer, never press. "
            "Infer the IANA timezone from the city and confirm it. Then ask what personality they "
            "want from Jarvis (for example dry British wit, or strictly business) and how chatty. "
            "Voice presets: bm_george (classic British), bm_daniel, bm_lewis (modern British)."
        ),
    ),
    OnboardingModule(
        id="business",
        title="Your business",
        goal="The consultancy: name, what it sells, why clients choose it, and the rate card.",
        fields=(
            "business.name",
            "business.tagline",
            "business.website",
            "business.services",
            "business.differentiators",
            "business.team",
            "business.rate_card",
        ),
        opening="Tell me about your consultancy: what's it called, and what do you sell?",
        guidance=(
            "Capture each service separately. For the rate card, get a price or range, currency "
            "and unit per service. If they're unsure, record typical ranges and note it."
        ),
    ),
    OnboardingModule(
        id="clients",
        title="Ideal clients",
        goal="Who to hunt for in each lead play, and who never to approach.",
        fields=("clients.ideal_clients", "clients.current_clients", "clients.never_pitch"),
        opening=(
            "Who's your ideal client? We'll go through four hunting grounds: East African SMEs, "
            "international startups, tenders and institutions, and agencies that need "
            "overflow help."
        ),
        guidance=(
            "Create one ideal_clients entry per play the owner cares about (play ids: ea_smes, "
            "international, tenders, agencies) with industries, locations, budget range and buying "
            "signals. Ask about sectors or companies they never want to pitch."
        ),
    ),
    OnboardingModule(
        id="portfolio",
        title="Portfolio & case studies",
        goal="Work Jarvis can cite in proposals and outreach.",
        fields=("portfolio.portfolio_url", "portfolio.highlights"),
        opening="Which projects are you proudest of? A link to your portfolio is a great start.",
        guidance=(
            "Capture two to five highlights: title, client (if shareable), summary, URL and stack."
        ),
    ),
    OnboardingModule(
        id="engineering",
        title="Stacks & conventions",
        goal="How the owner builds software, so Jarvis's code matches their house style.",
        fields=(
            "engineering.primary_stacks",
            "engineering.also_uses",
            "engineering.avoid",
            "engineering.conventions",
            "engineering.hosting",
            "engineering.testing",
        ),
        opening="What do you build client projects with, day to day?",
        guidance=(
            "Probe conventions concretely: language strictness, package manager, formatting, "
            "commit style, branching, testing, CI, hosting. Note anything they refuse to use."
        ),
    ),
    OnboardingModule(
        id="design",
        title="Design taste",
        goal="Aesthetic direction for everything Jarvis designs.",
        fields=(
            "design.style_keywords",
            "design.liked_references",
            "design.disliked_patterns",
            "design.brand_colors",
            "design.accessibility",
        ),
        opening=(
            "Name two or three websites or apps whose design you admire, "
            "and what you like about them."
        ),
        guidance=(
            "Turn likes into style keywords. Ask what design trends they dislike. Record brand "
            "colours if any. Ask what accessibility standard they hold work to "
            "(WCAG 2.2 AA is typical)."
        ),
    ),
    OnboardingModule(
        id="communication",
        title="Writing tone",
        goal="How the owner writes, so drafts sound like them.",
        fields=(
            "communication.tone",
            "communication.formality",
            "communication.greeting",
            "communication.sign_off",
            "communication.signature",
            "communication.languages",
            "communication.style_notes",
        ),
        opening="How would you describe the way you write to clients: formal, warm, brief?",
        guidance=(
            "Ask for their usual greeting and sign-off verbatim, and their email signature. "
            "Ask whether they write in Swahili too, and when. Offer to learn more from their "
            "sent emails later (that's a separate, consented step)."
        ),
    ),
    OnboardingModule(
        id="schedule",
        title="Schedule & routines",
        goal="When the owner works, focuses and must not be disturbed.",
        fields=(
            "schedule.working_hours",
            "schedule.focus_blocks",
            "schedule.routines",
            "schedule.do_not_disturb",
            "schedule.brief_time",
        ),
        opening="What does a normal working week look like for you?",
        guidance=(
            "Get working hours, deep-work blocks, do-not-disturb times and the preferred "
            "time for the daily tech brief."
        ),
    ),
    OnboardingModule(
        id="goals",
        title="Goals",
        goal="What success looks like this quarter and this year.",
        fields=(
            "goals.business_goals",
            "goals.revenue_target",
            "goals.learning_goals",
            "goals.personal_goals",
        ),
        opening="What are you aiming for this quarter, in the business?",
        guidance=(
            "Make goals specific and measurable where the owner is willing. "
            "Personal goals are optional."
        ),
    ),
    OnboardingModule(
        id="people",
        title="VIPs & contacts",
        goal="The people who matter, so Jarvis prioritises them and recognises their emails.",
        fields=("people.contacts",),
        opening=(
            "Who are the people I should always treat as a priority: key clients, partners, family?"
        ),
        guidance=(
            "For each: name, email if known, relationship, VIP yes or no, and notes. Known "
            "contacts' emails are how Jarvis tells a trusted recipient from a new one."
        ),
    ),
    OnboardingModule(
        id="boundaries",
        title="Boundaries & autonomy",
        goal="What Jarvis must never do, must always ask about, and may do on its own.",
        fields=(
            "boundaries.never_do",
            "boundaries.always_ask_before",
            "boundaries.ok_without_asking",
            "boundaries.sensitive_topics",
            "boundaries.spending_limit",
        ),
        opening="Let's set the ground rules. Is there anything I must never do on your behalf?",
        guidance=(
            "Explain that sending, deploying, deleting and paying always need approval regardless. "
            "Capture extra limits, topics to handle carefully, and low-risk things they're happy "
            "to delegate."
        ),
    ),
    OnboardingModule(
        id="personal",
        title="Personal (optional)",
        goal="Anything personal the owner wants Jarvis to keep in mind.",
        fields=("personal.notes", "personal.important_dates"),
        opening=(
            "This part is entirely optional: is there anything personal you'd like me "
            "to keep in mind, like important dates?"
        ),
        guidance="Never press. Accept 'nothing' gracefully and finish the module.",
        optional=True,
    ),
)

MODULES_BY_ID = {m.id: m for m in MODULES}


def _check_coverage() -> None:
    covered = {f for m in MODULES for f in m.fields}
    missing = set(REQUIRED_FIELDS) - covered
    if missing:
        raise RuntimeError(f"onboarding modules miss required profile fields: {sorted(missing)}")


_check_coverage()
