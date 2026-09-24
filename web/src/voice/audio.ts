/**
 * The browser side of voice: the microphone in, Jarvis's voice out.
 *
 * One AudioContext at the device's own rate does both. The capture worklet
 * converts the microphone to 16 kHz frames; replies arrive at 24 kHz and are
 * converted up here before the playback worklet queues them.
 */
import captureUrl from "./capture.worklet.ts?worker&url";
import { OUTPUT_RATE, Resampler, pcm16ToFloat } from "./pcm";
import playbackUrl from "./playback.worklet.ts?worker&url";

/** What the voice client needs from an audio device (a fake one in tests). */
export interface AudioIO {
  /** Open the microphone and call `onFrame` with each 20 ms frame of 16 kHz PCM. */
  start(onFrame: (pcm: ArrayBuffer, level: number) => void): Promise<void>;
  /** Queue a chunk of Jarvis's voice (16-bit PCM at 24 kHz). */
  play(pcm: ArrayBuffer): void;
  /** Drop everything queued (Jarvis was interrupted). */
  flush(): void;
  /** True while Jarvis's voice is coming out of the speaker. */
  readonly playing: boolean;
  onPlayback?: (playing: boolean, level: number) => void;
  stop(): Promise<void>;
}

export class MicrophoneError extends Error {}

function microphoneProblem(error: unknown): string {
  const name = error instanceof DOMException ? error.name : "";
  if (name === "NotAllowedError" || name === "SecurityError")
    return "Microphone access is blocked. Allow it for this site in your browser settings, then try again.";
  if (name === "NotFoundError" || name === "OverconstrainedError")
    return "No microphone was found. Plug one in or check your system sound settings.";
  if (name === "NotReadableError")
    return "The microphone is busy in another app. Close it there and try again.";
  return "The microphone couldn't start.";
}

export class BrowserAudio implements AudioIO {
  playing = false;
  onPlayback?: (playing: boolean, level: number) => void;
  private context: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private playback: AudioWorkletNode | null = null;
  private upsampler: Resampler | null = null;
  private oddByte: Uint8Array | null = null;

  /**
   * Create the audio context now, inside the tap that starts talking. Safari on
   * iPhone only lets sound play from a context created during a tap.
   */
  prime(): void {
    if (this.context) return;
    this.context = new AudioContext({ latencyHint: "interactive" });
    void this.context.resume().catch(() => undefined);
  }

  async start(onFrame: (pcm: ArrayBuffer, level: number) => void): Promise<void> {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new MicrophoneError(
        "This browser can only use the microphone over HTTPS. Open Jarvis at its Tailscale address.",
      );
    }
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
    } catch (error) {
      throw new MicrophoneError(microphoneProblem(error));
    }
    const context = this.context ?? new AudioContext({ latencyHint: "interactive" });
    this.context = context;
    await context.audioWorklet.addModule(captureUrl);
    await context.audioWorklet.addModule(playbackUrl);

    const capture = new AudioWorkletNode(context, "jarvis-capture", {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [1],
    });
    capture.port.onmessage = (event: MessageEvent<{ pcm: ArrayBuffer; level: number }>) =>
      onFrame(event.data.pcm, event.data.level);
    // Connected to the speakers through a muted gain so the browser keeps it running.
    const silent = context.createGain();
    silent.gain.value = 0;
    context.createMediaStreamSource(this.stream).connect(capture);
    capture.connect(silent).connect(context.destination);

    this.playback = new AudioWorkletNode(context, "jarvis-playback", {
      numberOfInputs: 0,
      numberOfOutputs: 1,
      outputChannelCount: [1],
    });
    this.playback.port.onmessage = (event: MessageEvent<{ playing: boolean; level: number }>) => {
      this.playing = event.data.playing;
      this.onPlayback?.(event.data.playing, event.data.level);
    };
    this.playback.connect(context.destination);
    this.upsampler = new Resampler(OUTPUT_RATE, context.sampleRate);
    if (context.state === "suspended") await context.resume();
  }

  play(pcm: ArrayBuffer): void {
    if (!this.playback || !this.upsampler) return;
    let bytes = new Uint8Array(pcm);
    if (this.oddByte) {
      const joined = new Uint8Array(bytes.length + 1);
      joined.set(this.oddByte);
      joined.set(bytes, 1);
      bytes = joined;
      this.oddByte = null;
    }
    if (bytes.length % 2) {
      this.oddByte = bytes.slice(-1);
      bytes = bytes.slice(0, -1);
    }
    const samples = this.upsampler.process(pcm16ToFloat(bytes.slice().buffer));
    this.playback.port.postMessage({ type: "audio", samples }, [samples.buffer]);
  }

  flush(): void {
    this.playback?.port.postMessage({ type: "flush" });
    if (this.context) this.upsampler = new Resampler(OUTPUT_RATE, this.context.sampleRate);
    this.oddByte = null;
    this.playing = false;
  }

  async stop(): Promise<void> {
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
    this.playback = null;
    this.playing = false;
    const context = this.context;
    this.context = null;
    if (context && context.state !== "closed") await context.close();
  }
}
