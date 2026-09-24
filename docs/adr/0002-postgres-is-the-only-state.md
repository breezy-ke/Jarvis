# 0002: Postgres holds all state

- Status: accepted
- Date: 2026-09-24

## Context

Jarvis needs:

- relational data: profile, approvals, audit log, CRM later
- vector search for memory
- durable scheduled jobs that survive power cuts
- rate-limited queues

A separate vector database, job queue and cache would each add a service to
run, back up and secure on a home PC.

## Decision

Use one Postgres 17 database:

- **pgvector** for embeddings, and Postgres full-text search for keywords.
  Memory search fuses both by rank.
- **DBOS Transact**, running inside the core, for durable workflows and cron
  schedules, with state in its own schema.
- **Alembic** migrations, applied automatically at startup under an advisory
  lock.

## Consequences

- One thing to back up (`make backup`) and restore.
- After a crash or power cut, scheduled work catches up and interrupted
  workflows resume.
- Tests run against real Postgres + pgvector, never SQLite, so they exercise
  what production runs.
- If memory grows past what pgvector handles comfortably on one PC (millions
  of rows), we revisit. A personal assistant won't get close for years.
