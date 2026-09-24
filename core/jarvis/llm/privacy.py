"""Which providers may see which data. The router uses this to fail closed."""

from __future__ import annotations

from jarvis.llm.config import PrivacyClass, ProviderConfig


def allowed(provider: ProviderConfig, privacy: PrivacyClass) -> bool:
    """True if this provider may receive data of this class.

    * PUBLIC: any provider.
    * PERSONAL: local models, or providers that don't train on your data.
    * CONFIDENTIAL: local models, or providers that don't train on your data
      *and* keep none of it (zero data retention).
    """
    if provider.local:
        return True
    if privacy == PrivacyClass.PUBLIC:
        return True
    if privacy == PrivacyClass.PERSONAL:
        return not provider.trains_on_data
    return (not provider.trains_on_data) and provider.zero_data_retention


def explain(provider_name: str, provider: ProviderConfig, privacy: PrivacyClass) -> str:
    if allowed(provider, privacy):
        return f"{provider_name} may see {privacy.value} data"
    if privacy == PrivacyClass.PERSONAL:
        return f"{provider_name} may train on your data, so it never sees personal data"
    return f"{provider_name} is not zero-retention, so it never sees confidential data"
