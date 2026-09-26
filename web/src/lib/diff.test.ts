import { describe, expect, it } from "vitest";

import { changed, lineDiff } from "./diff";

describe("lineDiff", () => {
  it("marks what you removed and added, keeping the rest", () => {
    expect(
      lineDiff("Hi Achieng,\nTuesday works.\nBrian", "Hi Achieng,\nWednesday works.\nBrian"),
    ).toEqual([
      { kind: "same", text: "Hi Achieng," },
      { kind: "removed", text: "Tuesday works." },
      { kind: "added", text: "Wednesday works." },
      { kind: "same", text: "Brian" },
    ]);
  });

  it("handles one side being empty", () => {
    expect(lineDiff("", "New")).toEqual([
      { kind: "removed", text: "" },
      { kind: "added", text: "New" },
    ]);
    expect(lineDiff("Old\nlines", "Old")).toEqual([
      { kind: "same", text: "Old" },
      { kind: "removed", text: "lines" },
    ]);
  });

  it("knows when nothing really changed", () => {
    expect(changed("Same text\n", "Same text")).toBe(false);
    expect(changed(null, "Anything")).toBe(false);
    expect(changed("Before", "After")).toBe(true);
  });
});
