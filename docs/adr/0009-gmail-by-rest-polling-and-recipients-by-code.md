# 0009: Gmail by REST and polling; mail agents without tools; recipients by code

- Status: accepted
- Date: 2026-09-26

## Context

- **The need:** Jarvis reads the owner's personal Gmail, sorts it, drafts
  replies and sends them. Sending is outward-facing and can't be undone once
  it's out.
- **Every email is untrusted input.** Anyone can write anything to the owner,
  including instructions aimed at an AI.
- **No public address:** Jarvis is reachable only over Tailscale (ADR 0006).
  Gmail's push notifications go through a Cloud Pub/Sub topic, which needs
  billing on the Cloud project and either an address Google can reach or a
  subscription with credentials of its own.
- **IMAP** would need Google's full-mail scope, which includes permanent
  deletion, and has no Gmail labels, drafts or change history of its own.
- **Gmail replaces** a Message-ID the sender sets, so it can't be used to
  recognise Jarvis's own sends.

## Decision

- **The Gmail REST API with the `gmail.modify` scope:** read, label, draft and
  send, but no permanent deletion. `calendar.events` is added for private
  holds.
- **Polling:** every 60 seconds Jarvis asks Gmail what changed since the last
  sync point (`history.list`). When Gmail no longer knows that point (it
  answers 404 after about a week), Jarvis reads the recent mail again. The
  sync point lives in the database, so restarts lose nothing.
- **Mail agents without tools:** sorting and drafting are model calls with a
  typed answer and nothing else. They can't act, and what they return is
  data. The email reaches them wrapped as untrusted content.
- **Code checks before the model, rules after it:** authentication results,
  look-alike senders, hidden text and dangerous links are checked by code.
  The model can't argue an email out of "suspicious".
- **Recipients by code:** the drafting model writes only the body. The
  recipients, the subject and the threading headers come from the
  conversation itself (reply or reply to all), whatever the email or the
  model says.
- **Sending at most once:** `email.send` is always L2. After approval and the
  undo window, Jarvis sends exactly the approved email with an
  `X-Jarvis-Action` header naming the proposal. If Gmail's answer is lost, the
  send becomes "unknown outcome". A later sync confirms it only if that
  header turns up in Sent mail with the approved recipients. It's never sent
  again automatically.
- **Jarvis's draft is the original; the Gmail copy follows it.** The copy is
  updated only while the owner hasn't changed it in Gmail (checked by a hash
  of its body), and removed after sending only if it's still unchanged.

## Consequences

- **Good:**
  - No public endpoint and no billing.
  - A narrow scope.
  - Nothing is lost across restarts.
  - Everything can be tested against a fake Gmail, including the injection
    suite, which drives an obedient model through the whole pipeline.
- **Costs:**
  - New mail shows up within about a minute, plus the time to sort it.
  - Polling uses a little quota and network even when nothing changes. A
    history check costs 2 quota units, far inside Gmail's per-user limits.
  - `gmail.modify` is a restricted scope. An app with fewer than 100 users
    needs no verification, so the owner clicks through Google's "unverified
    app" screen once. Google could change that policy.
  - A send can stay "unknown outcome" if the email never shows up in Sent
    mail. Then the owner decides whether to send it again.
  - A business mailbox (Phase 5) will need an IMAP/SMTP adapter with its own
    sync. The store, the agents and the send rules carry over.
