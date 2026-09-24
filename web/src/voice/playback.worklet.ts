/**
 * AudioWorklet: plays Jarvis's voice from a queue that can be emptied instantly
 * (when you talk over Jarvis). Tells the page when playback starts and stops.
 */
import { rms } from "./pcm";

declare function registerProcessor(name: string, processor: unknown): void;
declare class AudioWorkletProcessor {
  readonly port: MessagePort;
}

type Message = { type: "audio"; samples: Float32Array } | { type: "flush" };

class PlaybackProcessor extends AudioWorkletProcessor {
  private queue: Float32Array[] = [];
  private current: Float32Array | null = null;
  private offset = 0;
  private wasPlaying = false;
  private blocks = 0;

  constructor() {
    super();
    this.port.onmessage = (event: MessageEvent<Message>) => {
      if (event.data.type === "audio") {
        this.queue.push(event.data.samples);
      } else {
        this.queue = [];
        this.current = null;
        this.offset = 0;
      }
    };
  }

  process(_inputs: Float32Array[][], outputs: Float32Array[][]): boolean {
    const out = outputs[0]?.[0];
    if (!out) return true;
    let written = 0;
    while (written < out.length) {
      if (!this.current || this.offset >= this.current.length) {
        this.current = this.queue.shift() ?? null;
        this.offset = 0;
        if (!this.current) break;
      }
      const n = Math.min(out.length - written, this.current.length - this.offset);
      out.set(this.current.subarray(this.offset, this.offset + n), written);
      written += n;
      this.offset += n;
    }
    out.fill(0, written);
    const playing = written > 0;
    this.blocks += 1;
    if (playing !== this.wasPlaying || (playing && this.blocks % 8 === 0)) {
      this.port.postMessage({ playing, level: rms(out) });
      this.wasPlaying = playing;
    }
    return true;
  }
}

registerProcessor("jarvis-playback", PlaybackProcessor);
