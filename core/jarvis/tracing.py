"""Optional LLM tracing to a self-hosted Arize Phoenix (OpenTelemetry).

Traces include prompts and replies, so they only ever go to the Phoenix
container on your own machine. Set PHOENIX_COLLECTOR_ENDPOINT to enable.
"""

from __future__ import annotations

import logging

from jarvis.config import Settings

log = logging.getLogger("jarvis.tracing")


def configure_tracing(settings: Settings) -> bool:
    if not settings.phoenix_endpoint:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning(
            "tracing requested but OpenTelemetry isn't installed (install the tracing extra)"
        )
        return False
    from pydantic_ai import Agent, InstrumentationSettings

    provider = TracerProvider(resource=Resource.create({"service.name": "jarvis"}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.phoenix_endpoint))
    )
    trace.set_tracer_provider(provider)
    Agent.instrument_all(InstrumentationSettings(tracer_provider=provider))
    log.info("tracing to %s", settings.phoenix_endpoint)
    return True
