/**
 * AudioWorklet: the microphone, as 20 ms frames of 16 kHz 16-bit PCM for Jarvis.
 * Runs on the audio thread; each frame goes to the page with its loudness.
 */
import { Framer, INPUT_RATE, Resampler, floatToPcm16, rms } from "./pcm";

declare const sampleRate: number;
declare function registerProcessor(name: string, processor: unknown): void;
declare class AudioWorkletProcessor {
  readonly port: MessagePort;
}

class CaptureProcessor extends AudioWorkletProcessor {
  private readonly resampler = new Resampler(sampleRate, INPUT_RATE);
  private readonly framer = new Framer();

  process(inputs: Float32Array[][]): boolean {
    const channel = inputs[0]?.[0];
    if (channel?.length) {
      for (const frame of this.framer.push(this.resampler.process(channel))) {
        const pcm = floatToPcm16(frame);
        this.port.postMessage({ pcm, level: rms(frame) }, [pcm]);
      }
    }
    return true;
  }
}

registerProcessor("jarvis-capture", CaptureProcessor);
