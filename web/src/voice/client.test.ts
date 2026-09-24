import { describe, expect, it, vi } from "vitest";

import type { AudioIO } from "./audio";
import { VoiceClient, explainClose, voiceSocketUrl, type ClientStatus } from "./client";
import type { ServerEvent } from "./protocol";

class FakeSocket {
  binaryType = "blob";
  readyState = 1;
  sent: (string | ArrayBuffer)[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;

  send(data: string | ArrayBuffer) {
    this.sent.push(data);
  }

  close(code = 1000, reason = "") {
    this.readyState = 3;
    this.onclose?.({ code, reason } as CloseEvent);
  }

  receive(data: string | ArrayBuffer) {
    this.onmessage?.({ data } as MessageEvent);
  }

  json(): Record<string, unknown>[] {
    return this.sent.filter((d) => typeof d === "string").map((d) => JSON.parse(d as string));
  }

  audio(): ArrayBuffer[] {
    return this.sent.filter((d): d is ArrayBuffer => d instanceof ArrayBuffer);
  }
}

class FakeAudio implements AudioIO {
  playing = false;
  onPlayback?: (playing: boolean, level: number) => void;
  played: ArrayBuffer[] = [];
  flushed = 0;
  stopped = 0;
  mic: ((pcm: ArrayBuffer, level: number) => void) | null = null;
  failWith: Error | null = null;

  async start(onFrame: (pcm: ArrayBuffer, level: number) => void) {
    if (this.failWith) throw this.failWith;
    this.mic = onFrame;
  }
  play(pcm: ArrayBuffer) {
    this.played.push(pcm);
  }
  flush() {
    this.flushed += 1;
  }
  async stop() {
    this.stopped += 1;
  }
  speak(frames = 1) {
    for (let i = 0; i < frames; i++) this.mic?.(new ArrayBuffer(640), 0.2);
  }
}

const READY = JSON.stringify({ type: "ready", input_rate: 16000, output_rate: 24000 });

async function setup() {
  const socket = new FakeSocket();
  const audio = new FakeAudio();
  const events: ServerEvent[] = [];
  const statuses: ClientStatus[] = [];
  const client = new VoiceClient({
    url: "ws://localhost/api/voice/ws",
    audio,
    onEvent: (e) => events.push(e),
    onStatus: (s) => statuses.push(s),
    createSocket: () => socket as unknown as WebSocket,
  });
  client.connect();
  return { socket, audio, events, statuses, client };
}

async function settle() {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe("VoiceClient", () => {
  it("opens the microphone once Jarvis is ready, then streams it", async () => {
    const { socket, audio, statuses } = await setup();
    expect(socket.binaryType).toBe("arraybuffer");
    audio.speak(); // nothing is open yet
    expect(socket.audio()).toHaveLength(0);
    socket.receive(READY);
    await settle();
    expect(statuses.map((s) => s.kind)).toEqual(["connecting", "live"]);
    audio.speak(3);
    expect(socket.audio()).toHaveLength(3);
  });

  it("plays Jarvis's voice and stops it when told to", async () => {
    const { socket, audio, events } = await setup();
    socket.receive(READY);
    await settle();
    socket.receive(new ArrayBuffer(4800));
    expect(audio.played).toHaveLength(1);
    socket.receive(JSON.stringify({ type: "interrupt" }));
    await settle();
    expect(audio.flushed).toBe(1);
    expect(events.map((e) => e.type)).toEqual(["ready", "interrupt"]);
  });

  it("interrupting stops playback and tells Jarvis", async () => {
    const { socket, audio, client } = await setup();
    socket.receive(READY);
    await settle();
    client.interrupt();
    expect(audio.flushed).toBe(1);
    expect(socket.json()).toEqual([{ type: "interrupt" }]);
  });

  it("without talk-over, the microphone waits while Jarvis speaks", async () => {
    const { socket, audio, client } = await setup();
    client.bargeIn = false;
    socket.receive(READY);
    await settle();
    socket.receive(JSON.stringify({ type: "state", state: "speaking" }));
    await settle();
    audio.speak(2);
    expect(socket.audio()).toHaveLength(0);
    socket.receive(JSON.stringify({ type: "state", state: "idle" }));
    await settle();
    audio.playing = true; // the last words are still coming out of the speaker
    audio.speak();
    expect(socket.audio()).toHaveLength(0);
    audio.playing = false;
    audio.speak();
    expect(socket.audio()).toHaveLength(1);
  });

  it("with talk-over on, you can speak over Jarvis", async () => {
    const { socket, audio } = await setup();
    socket.receive(READY);
    await settle();
    socket.receive(JSON.stringify({ type: "state", state: "speaking" }));
    await settle();
    audio.speak();
    expect(socket.audio()).toHaveLength(1);
  });

  it("hold to talk sends only while held, then a short silence", async () => {
    const { socket, audio, client } = await setup();
    client.holdToTalk = true;
    socket.receive(READY);
    await settle();
    audio.speak();
    expect(socket.audio()).toHaveLength(0);
    client.hold(true);
    audio.speak(5);
    expect(socket.audio()).toHaveLength(5);
    client.hold(false);
    expect(socket.audio()).toHaveLength(5 + 40); // 0.8 s of silence ends the turn
    audio.speak();
    expect(socket.audio()).toHaveLength(45);
  });

  it("pressing hold to talk while Jarvis speaks interrupts it", async () => {
    const { socket, audio, client } = await setup();
    client.holdToTalk = true;
    socket.receive(READY);
    await settle();
    socket.receive(JSON.stringify({ type: "state", state: "speaking" }));
    await settle();
    client.hold(true);
    expect(audio.flushed).toBe(1);
    expect(socket.json()).toEqual([{ type: "interrupt" }]);
  });

  it("muting stops the microphone going out", async () => {
    const { socket, audio, client } = await setup();
    socket.receive(READY);
    await settle();
    client.muted = true;
    audio.speak(3);
    expect(socket.audio()).toHaveLength(0);
  });

  it("explains a blocked microphone and hangs up", async () => {
    const { socket, audio, statuses } = await setup();
    audio.failWith = new Error("Microphone access is blocked.");
    socket.receive(READY);
    await settle();
    expect(socket.readyState).toBe(3);
    expect(statuses.at(-1)).toEqual({
      kind: "closed",
      code: 1000,
      reason: "Microphone access is blocked.",
    });
  });

  it("closes the microphone if the call ended while it was opening", async () => {
    const { socket, audio } = await setup();
    let release: () => void = () => {};
    audio.start = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          release = resolve;
        }),
    );
    socket.receive(READY);
    socket.close(4503, "Voice is off.");
    const stoppedBefore = audio.stopped;
    release();
    await settle();
    expect(audio.stopped).toBeGreaterThan(stoppedBefore);
  });

  it("reports why the server closed the call", async () => {
    const { socket, statuses } = await setup();
    socket.close(4429);
    expect(statuses.at(-1)).toMatchObject({ kind: "closed", code: 4429 });
    expect((statuses.at(-1) as { reason: string }).reason).toMatch(/already open/);
  });
});

describe("helpers", () => {
  it("builds the socket address from the page's", () => {
    expect(voiceSocketUrl({ protocol: "https:", host: "jarvis.tail.ts.net" })).toBe(
      "wss://jarvis.tail.ts.net/api/voice/ws",
    );
    expect(voiceSocketUrl({ protocol: "http:", host: "localhost:5173" }, "abc")).toBe(
      "ws://localhost:5173/api/voice/ws?conversation=abc",
    );
  });

  it("explains close codes", () => {
    expect(explainClose(1000, "")).toBeNull();
    expect(explainClose(4401, "")).toMatch(/Sign in again/);
    expect(explainClose(4503, "Voice is missing its sentence data.")).toBe(
      "Voice is missing its sentence data.",
    );
    expect(explainClose(1006, "")).toMatch(/dropped/);
  });
});
