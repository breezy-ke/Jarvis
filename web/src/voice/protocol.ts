/**
 * The voice socket's JSON events (see core/jarvis/voice/protocol.py) and the
 * transcript they build up. Binary messages are audio and never come through here.
 */

export type ServerState = "listening" | "thinking" | "speaking" | "idle";

export type ServerEvent =
  | { type: "ready"; input_rate: number; output_rate: number }
  | { type: "state"; state: ServerState }
  | { type: "user"; text: string; final?: boolean }
  | { type: "assistant"; text: string; final?: boolean }
  | { type: "filler"; text: string }
  | { type: "interrupt" }
  | { type: "conversation"; id: string }
  | { type: "confirmation"; proposal_id: string | null; summary: string | null }
  | { type: "error"; message: string };

const STATES = new Set(["listening", "thinking", "speaking", "idle"]);

function text(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

/** Parse one text message from the server, or null if it isn't a known event. */
export function parseServerEvent(raw: string): ServerEvent | null {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof data !== "object" || data === null) return null;
  const e = data as Record<string, unknown>;
  switch (e.type) {
    case "ready":
      return typeof e.input_rate === "number" && typeof e.output_rate === "number"
        ? { type: "ready", input_rate: e.input_rate, output_rate: e.output_rate }
        : null;
    case "state":
      return typeof e.state === "string" && STATES.has(e.state)
        ? { type: "state", state: e.state as ServerState }
        : null;
    case "user":
    case "assistant": {
      const t = text(e.text);
      return t === null ? null : { type: e.type, text: t, final: e.final === true };
    }
    case "filler": {
      const t = text(e.text);
      return t === null ? null : { type: "filler", text: t };
    }
    case "interrupt":
      return { type: "interrupt" };
    case "conversation": {
      const id = text(e.id);
      return id === null ? null : { type: "conversation", id };
    }
    case "confirmation":
      return {
        type: "confirmation",
        proposal_id: text(e.proposal_id),
        summary: text(e.summary),
      };
    case "error": {
      const message = text(e.message);
      return message === null ? null : { type: "error", message };
    }
    default:
      return null;
  }
}

export interface TranscriptLine {
  id: number;
  role: "user" | "assistant" | "filler" | "error";
  text: string;
  /** Still being spoken (assistant lines only). */
  open: boolean;
  interrupted?: boolean;
}

export interface VoiceView {
  lines: TranscriptLine[];
  conversationId: string | null;
  confirmation: { proposalId: string; summary: string } | null;
  serverState: ServerState;
  nextId: number;
}

export const MAX_LINES = 60;

export const emptyView: VoiceView = {
  lines: [],
  conversationId: null,
  confirmation: null,
  serverState: "idle",
  nextId: 1,
};

function closeOpen(lines: TranscriptLine[], interrupted = false): TranscriptLine[] {
  const last = lines[lines.length - 1];
  if (!last?.open) return lines;
  return [...lines.slice(0, -1), { ...last, open: false, interrupted: interrupted || undefined }];
}

function add(view: VoiceView, line: Omit<TranscriptLine, "id">): VoiceView {
  const lines = [...view.lines, { ...line, id: view.nextId }].slice(-MAX_LINES);
  return { ...view, lines, nextId: view.nextId + 1 };
}

/**
 * Fold one server event into the transcript.
 *
 * Assistant text arrives in pieces as it's spoken. `final: true` closes the
 * current line: an empty final marks the end of a streamed reply, and a final
 * with text is a complete line on its own (a read-back, "Approved.").
 */
export function reduce(view: VoiceView, event: ServerEvent): VoiceView {
  switch (event.type) {
    case "state":
      return { ...view, serverState: event.state };
    case "user":
      return add(
        { ...view, lines: closeOpen(view.lines) },
        {
          role: "user",
          text: event.text,
          open: false,
        },
      );
    case "assistant": {
      const last = view.lines[view.lines.length - 1];
      if (last?.role === "assistant" && last.open) {
        const updated = { ...last, text: last.text + event.text, open: !event.final };
        return { ...view, lines: [...view.lines.slice(0, -1), updated] };
      }
      if (!event.text) return view; // an end marker with nothing open
      return add(view, { role: "assistant", text: event.text, open: !event.final });
    }
    case "filler":
      return add(view, { role: "filler", text: event.text, open: false });
    case "interrupt":
      return { ...view, lines: closeOpen(view.lines, true) };
    case "conversation":
      return { ...view, conversationId: event.id };
    case "confirmation":
      return {
        ...view,
        confirmation:
          event.proposal_id && event.summary
            ? { proposalId: event.proposal_id, summary: event.summary }
            : null,
      };
    case "error":
      return add(
        { ...view, lines: closeOpen(view.lines) },
        {
          role: "error",
          text: event.message,
          open: false,
        },
      );
    case "ready":
      return view;
  }
}
