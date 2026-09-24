/**
 * Audio plumbing for the voice socket: sample-rate conversion and 16-bit PCM.
 *
 * Jarvis hears 16 kHz and speaks at 24 kHz (16-bit little-endian mono), while
 * browsers run audio at the device rate (usually 44.1 or 48 kHz). Everything
 * here is plain math with no browser APIs, so it runs in AudioWorklets and tests.
 */

export const INPUT_RATE = 16_000;
export const OUTPUT_RATE = 24_000;
export const FRAME_SAMPLES = 320; // 20 ms at 16 kHz

/** Float samples in [-1, 1] to 16-bit little-endian PCM bytes. */
export function floatToPcm16(samples: Float32Array): ArrayBuffer {
  const buffer = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buffer);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i] ?? 0));
    view.setInt16(i * 2, s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7fff), true);
  }
  return buffer;
}

/** 16-bit little-endian PCM bytes to float samples. An odd trailing byte is ignored. */
export function pcm16ToFloat(buffer: ArrayBuffer): Float32Array {
  const view = new DataView(buffer);
  const out = new Float32Array(Math.floor(buffer.byteLength / 2));
  for (let i = 0; i < out.length; i++) out[i] = view.getInt16(i * 2, true) / 0x8000;
  return out;
}

/** Root-mean-square loudness of a block, 0 (silence) to about 1. */
export function rms(samples: Float32Array): number {
  if (!samples.length) return 0;
  let sum = 0;
  for (const s of samples) sum += s * s;
  return Math.sqrt(sum / samples.length);
}

/** A windowed-sinc low-pass filter. `cutoff` is a fraction of the sample rate (0-0.5). */
export function lowPass(taps: number, cutoff: number): Float32Array {
  const h = new Float32Array(taps);
  const middle = (taps - 1) / 2;
  let total = 0;
  for (let k = 0; k < taps; k++) {
    const x = k - middle;
    const sinc = x === 0 ? 2 * cutoff : Math.sin(2 * Math.PI * cutoff * x) / (Math.PI * x);
    const hamming = 0.54 - 0.46 * Math.cos((2 * Math.PI * k) / (taps - 1));
    h[k] = sinc * hamming;
    total += sinc * hamming;
  }
  for (let k = 0; k < taps; k++) h[k] = (h[k] ?? 0) / total;
  return h;
}

/**
 * Streaming sample-rate conversion (linear interpolation, with an anti-aliasing
 * filter when going down). Feed it chunks of any size; the output is the same as
 * converting the whole stream at once.
 */
export class Resampler {
  private readonly step: number;
  private readonly filter: Float32Array | null;
  private history: Float32Array;
  private position = 1; // where the next output sample falls, relative to [last, ...chunk]
  private last = 0;

  constructor(
    readonly from: number,
    readonly to: number,
  ) {
    this.step = from / to;
    this.filter = from > to ? lowPass(31, (0.5 * to * 0.9) / from) : null;
    this.history = new Float32Array(this.filter ? this.filter.length - 1 : 0);
  }

  process(input: Float32Array): Float32Array {
    if (this.from === this.to) return input.slice();
    const x = this.filter ? this.smooth(input) : input;
    const out: number[] = [];
    while (this.position < x.length) {
      const i = Math.floor(this.position);
      const fraction = this.position - i;
      const a = i === 0 ? this.last : (x[i - 1] ?? 0);
      const b = x[i] ?? 0;
      out.push(a + (b - a) * fraction);
      this.position += this.step;
    }
    this.position -= x.length;
    if (x.length) this.last = x[x.length - 1] ?? 0;
    return Float32Array.from(out);
  }

  private smooth(input: Float32Array): Float32Array {
    const h = this.filter as Float32Array;
    const n = h.length - 1;
    const joined = new Float32Array(n + input.length);
    joined.set(this.history);
    joined.set(input, n);
    const out = new Float32Array(input.length);
    for (let i = 0; i < input.length; i++) {
      let acc = 0;
      for (let k = 0; k <= n; k++) acc += (h[k] ?? 0) * (joined[i + n - k] ?? 0);
      out[i] = acc;
    }
    this.history = joined.slice(joined.length - n);
    return out;
  }
}

/** Collects samples into fixed-size frames (20 ms for the voice socket). */
export class Framer {
  private buffer: Float32Array;
  private filled = 0;

  constructor(readonly size: number = FRAME_SAMPLES) {
    this.buffer = new Float32Array(size);
  }

  push(samples: Float32Array): Float32Array[] {
    const frames: Float32Array[] = [];
    let offset = 0;
    while (offset < samples.length) {
      const take = Math.min(this.size - this.filled, samples.length - offset);
      this.buffer.set(samples.subarray(offset, offset + take), this.filled);
      this.filled += take;
      offset += take;
      if (this.filled === this.size) {
        frames.push(this.buffer);
        this.buffer = new Float32Array(this.size);
        this.filled = 0;
      }
    }
    return frames;
  }
}

/** `seconds` of silence as 16 kHz PCM frames (to end a hold-to-talk turn). */
export function silenceFrames(seconds: number): ArrayBuffer[] {
  const count = Math.round((seconds * INPUT_RATE) / FRAME_SAMPLES);
  return Array.from({ length: count }, () => new ArrayBuffer(FRAME_SAMPLES * 2));
}
