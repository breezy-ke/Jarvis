# 0010: A brief from feeds and official APIs; public summaries, private notes; an inbox copy, not an email

- Status: accepted
- Date: 2026-09-28

## Context

- **The need:** a tech brief every morning at 07:00 that knows the owner, arrives
  on the app, a push, Telegram, their inbox and as audio, learns from their
  👍/👎, and links every item to its source. The exit test: on time seven days
  in a row, every item linked, and feedback that measurably changes the order.
- **Sources:**
  - Sites publish RSS or Atom feeds.
  - Hacker News, GitHub (search and security advisories) and CISA (exploited
    vulnerabilities) have official public APIs.
  - Scraping pages instead would break often, and ignore what sites ask.
- **Two kinds of data meet in a brief:**
  - Public articles.
  - The owner's profile (stacks, clients, goals), which is personal.
  - Free public models (Gemini's free tier) may train on what they're sent.
    The private options (the GPU, Groq with zero retention) are slower or more
    limited.
- **The web is untrusted:** a feed or page can carry instructions for an AI,
  a bomb for an XML parser, or a link to an address inside the owner's
  network.
- **Email:** the owner wants a copy in their inbox, but sending mail to
  themselves would go through Gmail's sending path and count as sent mail.
- **Timing:** the brief must go out at 07:00 even after a restart or a
  scheduler problem. It must never go out twice, and never go out a day late.

## Decision

- **Feeds and official APIs only**, listed in `config/sources.yaml`, read every
  hour with conditional requests. Each story is stored once. Article pages are
  read only for the stories that make the brief, and only where robots.txt
  allows.
- **One safe fetcher for every web request.** It allows http(s) only, and
  public addresses only, re-checked after each redirect. It pins the checked
  address, and caps size and time. Feeds are parsed with `defusedxml`.
- **Ranking by code, not a model:** source weight × freshness × closeness to
  the profile (local embeddings) × votes × boosts. It's cheap, repeatable and
  explainable ("Why this?"). The security watch is also code, straight from
  the advisory data.
- **Two writers:**
  - A **public** model (`public_summarize`) writes neutral summaries from public
    text only.
  - A **private** model (`brief`) sees a trimmed profile summary and writes why
    each story matters, one thing to do today, and the spoken version.
  - Neither has tools, and neither can produce a link: their answer formats
    have no link fields, and addresses are stripped.
  - Links come from the sources' own records.
  - With no model at all, the brief still goes out, with the sources' own
    summaries.
- **The inbox copy is placed, not sent:** Gmail's `messages.insert`, allowed by
  the `gmail.modify` scope Jarvis already has. It's labelled `Jarvis/Brief`,
  with a mark only Jarvis can make (encrypted with `JARVIS_SECRET_KEY`), so
  the email sync skips genuine copies and flags imitations.
- **Timing in the core, from the database:** a loop wakes when the next step is
  due, and at least every 30 seconds. It prepares at 06:40 and delivers at 07:00.
  - What's done is recorded per channel, so a restart resumes, and no channel
    gets a brief twice.
  - A failed channel is retried that day.
  - A day that has passed is skipped.
  - DBOS still runs the nightly clean-up.
- **Research reuses the pieces:**
  - SearXNG on the stack, for search without cookies or an account.
  - The safe fetcher and robots.txt.
  - A private model with no tools, and citations checked by code.

## Consequences

- **Good:**
  - Every item links to where it came from, and a model can't change that.
  - The owner's profile never reaches a public model. A test checks every
    prompt sent to one.
  - A planted link can't reach Jarvis's own services.
  - Nothing is emailed, and nothing is sent twice.
  - Because the timing works from the database, a week of mornings can be
    simulated on a test clock, including a morning offline.
- **Costs:**
  - A site without a feed can't be a source.
  - Robots.txt sometimes means a summary is built from the feed's own text.
  - Summaries depend on a free tier's limits. When those are reached, the
    brief falls back to the sources' own summaries.
  - "Rising on GitHub" is a search for new repositories with many stars, not
    GitHub's Trending page, which has no official API.
  - The search engines behind SearXNG see research questions, though not who
    asked.
  - The brief is only as punctual as the PC is awake: a PC asleep at 07:00
    delivers late.
