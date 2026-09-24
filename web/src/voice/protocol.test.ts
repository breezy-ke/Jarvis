import { describe, expect, it } from "vitest";

import { MAX_LINES, emptyView, parseServerEvent, reduce, type ServerEvent } from "./protocol";

function play(events: ServerEvent[]) {
  return events.reduce(reduce, emptyView);
}

describe("parseServerEvent", () => {
  it("reads the events the server sends", () => {
    expect(parseServerEvent('{"type":"ready","input_rate":16000,"output_rate":24000}')).toEqual({
      type: "ready",
      input_rate: 16000,
      output_rate: 24000,
    });
    expect(parseServerEvent('{"type":"assistant","text":"Hi "}')).toEqual({
      type: "assistant",
      text: "Hi ",
      final: false,
    });
    expect(parseServerEvent('{"type":"confirmation","proposal_id":null,"summary":null}')).toEqual({
      type: "confirmation",
      proposal_id: null,
      summary: null,
    });
  });

  it("refuses anything malformed or unknown", () => {
    for (const raw of [
      "not json",
      "[1,2]",
      "null",
      '{"type":"state","state":"dancing"}',
      '{"type":"user"}',
      '{"type":"ready","input_rate":"16000"}',
      '{"type":"surprise"}',
    ]) {
      expect(parseServerEvent(raw)).toBeNull();
    }
  });
});

describe("reduce", () => {
  it("builds a spoken reply from its pieces and closes it at the end marker", () => {
    const view = play([
      { type: "user", text: "What's on today?", final: true },
      { type: "assistant", text: "Two ", final: false },
      { type: "assistant", text: "meetings.", final: false },
      { type: "assistant", text: "", final: true },
    ]);
    expect(view.lines.map((l) => [l.role, l.text, l.open])).toEqual([
      ["user", "What's on today?", false],
      ["assistant", "Two meetings.", false],
    ]);
  });

  it("keeps a read-back as its own line", () => {
    const view = play([
      { type: "assistant", text: "Done.", final: false },
      { type: "assistant", text: "", final: true },
      { type: "assistant", text: "To confirm: Invite to Kickoff.", final: true },
      { type: "confirmation", proposal_id: "p1", summary: "Invite to Kickoff" },
    ]);
    expect(view.lines.map((l) => l.text)).toEqual(["Done.", "To confirm: Invite to Kickoff."]);
    expect(view.confirmation).toEqual({ proposalId: "p1", summary: "Invite to Kickoff" });
    expect(
      reduce(view, { type: "confirmation", proposal_id: null, summary: null }).confirmation,
    ).toBeNull();
  });

  it("marks a reply cut off by barge-in", () => {
    const view = play([
      { type: "assistant", text: "Your week starts", final: false },
      { type: "interrupt" },
      { type: "user", text: "Stop, just tomorrow.", final: true },
    ]);
    expect(view.lines[0]).toMatchObject({
      text: "Your week starts",
      open: false,
      interrupted: true,
    });
    expect(view.lines[1]).toMatchObject({ role: "user", text: "Stop, just tomorrow." });
  });

  it("tracks state, the conversation, fillers and errors", () => {
    const view = play([
      { type: "state", state: "thinking" },
      { type: "conversation", id: "c-1" },
      { type: "filler", text: "One moment." },
      { type: "error", message: "The speech server is down." },
    ]);
    expect(view.serverState).toBe("thinking");
    expect(view.conversationId).toBe("c-1");
    expect(view.lines.map((l) => l.role)).toEqual(["filler", "error"]);
  });

  it("keeps only the most recent lines", () => {
    const events: ServerEvent[] = Array.from({ length: MAX_LINES + 10 }, (_, i) => ({
      type: "user",
      text: `line ${i}`,
      final: true,
    }));
    const view = play(events);
    expect(view.lines).toHaveLength(MAX_LINES);
    expect(view.lines[view.lines.length - 1]?.text).toBe(`line ${MAX_LINES + 9}`);
    expect(new Set(view.lines.map((l) => l.id)).size).toBe(MAX_LINES); // ids stay unique
  });
});
