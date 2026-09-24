# 0003: Agents only propose; code decides; approvals are hash-pinned

- Status: accepted
- Date: 2026-09-24

## Context

Models make mistakes and can be manipulated by content they read (prompt
injection). "No room for mistakes" can't mean a perfect model. It has to mean
that mistakes are caught before they matter.

## Decision

- Agents have no side-effecting tools. The one way to act is
  `propose_action(kind, payload, rationale)`.
- A deterministic policy engine decides each proposal from
  `config/policies.yaml`:
  - validators
  - autonomy level L0–L3
  - risk
  - approval channels
  - undo window
  - daily caps
  - quiet hours
  - the kill switch
  - the "profile signed off" gate
- An approval covers the SHA-256 of the canonical payload bytes. The executor
  re-parses exactly those bytes, so any edit means approving again.
- High and critical risk need a fresh passkey tap. Money, deletions and
  production deploys can never be automatic, and startup fails if the config
  says otherwise.
- Execution is at most once: `executing` is committed before the side effect,
  and a crash leaves `unknown_outcome` for the owner to review.
- Every step goes to a hash-chained audit log.

## Consequences

- More approvals early on. Autonomy can be raised per action kind later, and
  graduated auto-send (Phase 3) needs a record of approvals made without
  edits.
- The safety argument doesn't depend on the model. A property-based test
  drives random event sequences through the engine to check the invariants.
