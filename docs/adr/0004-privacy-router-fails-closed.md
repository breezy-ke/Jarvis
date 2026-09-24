# 0004: A privacy router that fails closed

- Status: accepted
- Date: 2026-09-24

## Context

The owner wants free models where possible. Free tiers differ in what they do
with your data:

- **Groq** contractually doesn't train on inputs, and offers zero data
  retention.
- **Gemini's free API tier** may use prompts to improve products, with human
  review.
- **Some OpenRouter free endpoints** log prompts.

Personal email and client work under NDA must never reach the wrong one.

## Decision

- Each AI call names a task in `config/models.yaml`. The task fixes a data
  class (public, personal or confidential) and an ordered list of candidate
  models.
- Providers carry flags: `local`, `trains_on_data`, `zero_data_retention`,
  `paid`.
- The router tries only the candidates allowed for the task's data class:
  - personal needs local or no-training
  - confidential needs local, or no-training plus zero retention
- If no candidate is left, the call fails with a clear error. There is no
  silent fallback.
- **Callers can tighten a task's class, never loosen it.**
- **Quotas:** the router tracks free-tier usage and skips a model before it
  hits its limits. Cooldowns follow errors.
- **Paid providers** stay off until a monthly budget cap is set, and are
  skipped once it's reached.

## Consequences

- **Swapping models is a config edit.** `make doctor ONLINE=1` flags retired
  model IDs.
- **Private-safe cloud capacity is limited** by Groq's free-tier limits. Local
  models on the GPU carry most personal work. Confidential website builds will
  be slow until a paid, zero-retention engine is added.
- **Tests enforce it:** the shipped config is checked against the rules on
  every CI run.
