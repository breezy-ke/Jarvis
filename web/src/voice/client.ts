/**
 * A live voice conversation with Jarvis over `/api/voice/ws`.
 *
 * The socket carries your microphone (binary, 16 kHz) up and Jarvis's voice
 * (binary, 24 kHz) and events (JSON) down. Jarvis's server decides when you've
 * finished speaking; this client only decides when to send your microphone:
 * never while muted, only while held in hold-to-talk, and, if talking over
 * Jarvis is off, not while Jarvis is speaking (avoids echo on speakers).
 */
import type { AudioIO } from "./audio";
import { silenceFrames } from "./pcm";
import { parseServerEvent, type ServerEvent } from "./protocol";

export type ClientStatus =
  | { kind: "idle" }
  | { kind: "connecting" }
  | { kind: "live" }
  | { kind: "closed"; code: number; reason: string | null };

export interface VoiceClientOptions {
  url: string;
  audio: AudioIO;
  onEvent: (event: ServerEvent) => void;
  onStatus: (status: ClientStatus) => void;
  /** Jarvis's voice started or stopped coming out of the speaker. */
  onPlayback?: (playing: boolean) => void;
  createSocket?: (url: string) => WebSocket;
}

const OPEN = 1; // WebSocket.OPEN

export function voiceSocketUrl(
  location: Pick<Location, "protocol" | "host">,
  conversationId?: string | null,
): string {
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  const url = new URL(`${scheme}//${location.host}/api/voice/ws`);
  if (conversationId) url.searchParams.set("conversation", conversationId);
  return url.toString();
}

/** What to tell the owner when the socket closes, or null for a normal goodbye. */
export function explainClose(code: number, reason: string): string | null {
  switch (code) {
    case 1000:
      return null;
    case 4401:
      return "Your session has expired. Sign in again.";
    case 4429:
      return "Voice is already open somewhere else (another tab or the Windows app). Close it there and try again.";
    case 4503:
      return reason || "Voice isn't available right now. Check Settings → Voice.";
    case 1006:
      return "The connection to Jarvis dropped. Check your connection and try again.";
    default:
      return reason || `The voice connection closed (${code}).`;
  }
}

export class VoiceClient {
  /** Your microphone and Jarvis's voice levels, for the orb (0 to about 1). */
  readonly levels = { input: 0, output: 0 };
  muted = false;
  bargeIn = true;
  holdToTalk = false;
  private holding = false;
  private socket: WebSocket | null = null;
  private serverSpeaking = false;
  private closed = false;
  private failure: string | null = null; // our own reason for closing, if any

  constructor(private readonly options: VoiceClientOptions) {}

  connect(): void {
    const { url, createSocket } = this.options;
    this.closed = false;
    this.failure = null;
    this.options.onStatus({ kind: "connecting" });
    const socket = createSocket ? createSocket(url) : new WebSocket(url);
    socket.binaryType = "arraybuffer";
    socket.onmessage = (message: MessageEvent) => void this.receive(message.data);
    socket.onclose = (event: CloseEvent) => {
      this.closed = true;
      void this.teardown();
      this.options.onStatus({
        kind: "closed",
        code: event.code,
        reason: this.failure ?? explainClose(event.code, event.reason),
      });
    };
    this.socket = socket;
  }

  /** Stop Jarvis talking now (the Stop button, or pressing hold-to-talk). */
  interrupt(): void {
    this.options.audio.flush();
    this.sendJson({ type: "interrupt" });
  }

  /** Hold-to-talk: press to speak (interrupting Jarvis), release to send the turn. */
  hold(down: boolean): void {
    if (!this.holdToTalk || down === this.holding) return;
    this.holding = down;
    if (down) {
      if (this.serverSpeaking || this.options.audio.playing) this.interrupt();
      return;
    }
    // Trailing silence lets Jarvis's turn detection hear that you've finished.
    for (const frame of silenceFrames(0.8)) this.sendAudio(frame);
  }

  close(): void {
    this.closed = true;
    this.socket?.close(1000);
    void this.teardown();
  }

  private async receive(data: unknown): Promise<void> {
    if (data instanceof ArrayBuffer) {
      this.options.audio.play(data);
      return;
    }
    if (typeof data !== "string") return;
    const event = parseServerEvent(data);
    if (!event) return;
    if (event.type === "ready") {
      await this.startMicrophone();
    } else if (event.type === "interrupt") {
      this.options.audio.flush();
    } else if (event.type === "state") {
      this.serverSpeaking = event.state === "speaking";
    }
    this.options.onEvent(event);
  }

  private async startMicrophone(): Promise<void> {
    const { audio } = this.options;
    audio.onPlayback = (playing, level) => {
      this.levels.output = playing ? level : 0;
      this.options.onPlayback?.(playing);
    };
    try {
      await audio.start((pcm, level) => this.fromMicrophone(pcm, level));
    } catch (error) {
      this.failure = error instanceof Error ? error.message : "The microphone couldn't start.";
      this.close(); // the socket's close event reports the failure
      return;
    }
    if (this.closed) {
      await audio.stop(); // the conversation ended while the microphone was opening
      return;
    }
    this.options.onStatus({ kind: "live" });
  }

  private fromMicrophone(pcm: ArrayBuffer, level: number): void {
    this.levels.input = level;
    if (this.muted) return;
    if (this.holdToTalk && !this.holding) return;
    if (!this.bargeIn && (this.serverSpeaking || this.options.audio.playing)) return;
    this.sendAudio(pcm);
  }

  private sendAudio(pcm: ArrayBuffer): void {
    if (this.socket?.readyState === OPEN) this.socket.send(pcm);
  }

  private sendJson(message: Record<string, unknown>): void {
    if (this.socket?.readyState === OPEN) this.socket.send(JSON.stringify(message));
  }

  private async teardown(): Promise<void> {
    this.socket = null;
    this.serverSpeaking = false;
    this.holding = false;
    this.levels.input = 0;
    this.levels.output = 0;
    await this.options.audio.stop();
  }
}
