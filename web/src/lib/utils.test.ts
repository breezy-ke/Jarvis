import { describe, expect, it } from "vitest";

import { greeting, percent, shortHash, timeAgo } from "./utils";

describe("utils", () => {
  it("formats relative time", () => {
    const now = new Date("2026-01-05T10:00:00Z");
    expect(timeAgo("2026-01-05T09:58:00Z", now)).toBe("2 minutes ago");
    expect(timeAgo(null, now)).toBe("never");
  });

  it("formats percentages and hashes", () => {
    expect(percent(0.456)).toBe("46%");
    expect(shortHash("abcdef0123456789")).toBe("abcdef…6789");
  });

  it("greets by time of day", () => {
    expect(greeting(new Date(2026, 0, 5, 8))).toBe("Good morning");
    expect(greeting(new Date(2026, 0, 5, 20))).toBe("Good evening");
  });
});
