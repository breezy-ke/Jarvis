# 0001: Build our own small core, not a fork of an agent platform

- Status: accepted
- Date: 2026-09-24

## Context

Open-source agent platforms such as OpenClaw and Hermes Agent already offer
chat, tools and plugins. But Jarvis will hold the owner's inbox, client list
and outreach, so it needs more than features. It needs:

- a narrow, auditable surface
- human approval enforced in code
- a privacy-aware choice of model for every call

The broad platforms write and install their own skills. At least one of them
had serious security advisories in 2026, including remote code execution. We
would still have to build onboarding, approvals, the lead engine and the
website studio ourselves.

## Decision

Build a small Python core from proven, permissively licensed parts:

- FastAPI
- Pydantic AI
- SQLAlchemy
- DBOS
- py_webauthn

The core speaks MCP, so vetted tools can plug in later. Jarvis may draft new
skills but never installs code itself. Changes land as reviewed pull requests.

## Consequences

- We own more code, so every capability ships with tests. CI runs lint, types,
  unit, integration, property-based and end-to-end tests.
- The attack surface stays small and readable. Adding a capability means
  adding a registered action kind with a policy entry, not a plugin with
  ambient authority.
