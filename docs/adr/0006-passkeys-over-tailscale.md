# 0006: Passkeys for sign-in; reach the PC only over Tailscale

- Status: accepted
- Date: 2026-09-24

## Context

- **Remote access:** the owner uses Jarvis from a phone, away from home.
  Opening a port on a home router exposes the PC to the internet.
- **HTTPS:** browsers only allow the microphone, passkeys and app install over
  HTTPS.
- **Passwords:** they can be phished.

## Decision

- **Tailscale** (free personal plan) connects the owner's devices over
  WireGuard. `tailscale serve` gives Jarvis a valid HTTPS certificate at
  `https://<pc>.<tailnet>.ts.net`. Nothing listens on a public address.
- **Sign-in is passkeys only** (WebAuthn, with user verification). The first
  passkey needs a one-time setup code printed on the PC, so knowing the
  address isn't enough to claim Jarvis. Ten one-time recovery codes cover a
  lost device.
- **Sessions are cookies** (HttpOnly, SameSite=Strict, Secure); only their hash
  is stored. High-risk approvals need a fresh passkey tap, not just a session.

## Consequences

- **Tailscale key expiry:** the owner must turn it off for the PC in the
  Tailscale admin console, or the PC drops off after 180 days. The setup guide
  and script say so.
- **Passkeys are bound to the address.** Changing the Tailscale machine name
  or tailnet means registering passkeys again. The recovery codes and
  `make setup-token` path cover it.
- **Telegram** (Phase 2) will be a second channel, answering only the owner's
  chat ID. It won't be able to approve high-risk actions.
