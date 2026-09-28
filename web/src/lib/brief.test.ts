import { describe, expect, it } from "vitest";

import { clockTime, deliveryNotes, longDay, safeHref } from "./brief";

describe("brief helpers", () => {
  it("names the day and the time the way you'd say them", () => {
    expect(longDay("2026-09-28")).toBe("Monday 28 September");
    expect(clockTime("2026-09-28T04:00:00Z", "Africa/Nairobi")).toBe("07:00");
  });

  it("only links plain web addresses", () => {
    expect(safeHref("https://web.dev/blog")).toBe("https://web.dev/blog");
    expect(safeHref("http://example.test/")).toBe("http://example.test/");
    expect(safeHref("javascript:alert(1)")).toBeNull();
    expect(safeHref("data:text/html,hi")).toBeNull();
    expect(safeHref("not a url")).toBeNull();
  });

  it("explains the channels that didn't get the brief, and only those", () => {
    const at = "2026-09-28T04:00:00Z";
    const notes = deliveryNotes({
      app: { status: "sent", at },
      push: { status: "skipped", at, detail: "Quiet hours" },
      voice: { status: "skipped", at, detail: "Telegram isn't linked" },
      telegram: { status: "failed", at, error: "TelegramError: Can't reach Telegram" },
      inbox: { status: "skipped", at, detail: "Jarvis can only read your Gmail." },
    });
    expect(notes).toEqual([
      {
        channel: "Telegram",
        failed: true,
        text: "didn't go out yet (TelegramError: Can't reach Telegram). Jarvis tries again every half hour today.",
      },
      { channel: "Gmail inbox copy", failed: false, text: "Jarvis can only read your Gmail." },
    ]);
  });
});
