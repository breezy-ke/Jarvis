"""The wiring: builds every long-lived service once, for the API and workers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jarvis.audit.log import AuditLog
from jarvis.clock import Clock, SystemClock
from jarvis.config import Settings
from jarvis.db.session import SessionFactory
from jarvis.llm.config import ModelsConfig, load_models_config
from jarvis.llm.router import ModelRouter
from jarvis.mail.contacts import MailContacts
from jarvis.memory.embeddings import Embedder, create_embedder
from jarvis.memory.store import MemoryStore
from jarvis.notify.push import PushService
from jarvis.policy.config import PoliciesConfig, load_policies
from jarvis.policy.engine import PolicyEngine
from jarvis.policy.registry import ActionRegistry
from jarvis.profile.service import ProfileService
from jarvis.security.crypto import Vault


@dataclass
class Services:
    settings: Settings
    session_factory: SessionFactory
    clock: Clock
    audit: AuditLog
    vault: Vault
    models_config: ModelsConfig
    policies_config: PoliciesConfig
    router: ModelRouter
    registry: ActionRegistry
    policy: PolicyEngine
    profiles: ProfileService
    embedder: Embedder
    memory: MemoryStore
    push: PushService
    persona: str


def load_persona(config_dir: Path) -> str:
    path = config_dir / "persona.md"
    return path.read_text(encoding="utf-8").strip()


def build_services(
    settings: Settings,
    session_factory: SessionFactory,
    *,
    clock: Clock | None = None,
    embedder: Embedder | None = None,
    models_config: ModelsConfig | None = None,
    policies_config: PoliciesConfig | None = None,
) -> Services:
    from jarvis.actions import register_builtin_actions  # avoid an import cycle

    clock = clock or SystemClock()
    audit = AuditLog(clock)
    if settings.secret_key is None:
        if settings.is_production:
            raise RuntimeError("JARVIS_SECRET_KEY is required in production. Run `make secrets`.")
        vault = Vault(Vault.generate_key())  # ephemeral: dev/test only
    else:
        vault = Vault(settings.secret_key.get_secret_value())
    models_path = settings.models_file or settings.config_dir / "models.yaml"
    models_config = models_config or load_models_config(models_path)
    policies_config = policies_config or load_policies(settings.config_dir / "policies.yaml")
    embedder = embedder or create_embedder(settings.embedder)
    router = ModelRouter(
        models_config,
        env=settings.provider_env(),
        session_factory=session_factory,
        clock=clock,
        audit=audit,
        allow_fake=settings.allow_fake_llm and not settings.is_production,
    )
    profiles = ProfileService(audit=audit, clock=clock)
    memory = MemoryStore(embedder=embedder, audit=audit, clock=clock)
    push = PushService(settings=settings, session_factory=session_factory)
    registry = ActionRegistry()
    register_builtin_actions(registry, push=push)
    policy = PolicyEngine(
        config=policies_config,
        registry=registry,
        audit=audit,
        clock=clock,
        gate=profiles,
        contacts=MailContacts(profiles),
    )
    return Services(
        settings=settings,
        session_factory=session_factory,
        clock=clock,
        audit=audit,
        vault=vault,
        models_config=models_config,
        policies_config=policies_config,
        router=router,
        registry=registry,
        policy=policy,
        profiles=profiles,
        embedder=embedder,
        memory=memory,
        push=push,
        persona=load_persona(settings.config_dir),
    )
