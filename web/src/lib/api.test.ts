import { describe, expect, it } from "vitest";

import { parseSSE } from "./api";

describe("parseSSE", () => {
  it("parses complete events and keeps the partial remainder", () => {
    const { events, rest } = parseSSE(
      'event: start\ndata: {"conversation_id":"c1"}\n\nevent: delta\ndata: {"text":"Hel"}\n\nevent: del',
    );
    expect(events).toEqual([
      { event: "start", data: { conversation_id: "c1" } },
      { event: "delta", data: { text: "Hel" } },
    ]);
    expect(rest).toBe("event: del");
  });

  it("handles CRLF line endings and ignores pings", () => {
    const { events } = parseSSE(': ping\r\n\r\nevent: done\r\ndata: {"text":"ok"}\r\n\r\n');
    expect(events).toEqual([{ event: "done", data: { text: "ok" } }]);
  });
});
