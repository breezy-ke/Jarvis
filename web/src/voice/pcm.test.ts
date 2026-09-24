import { describe, expect, it } from "vitest";

import {
  FRAME_SAMPLES,
  Framer,
  Resampler,
  floatToPcm16,
  pcm16ToFloat,
  rms,
  silenceFrames,
} from "./pcm";

function tone(hz: number, rate: number, seconds: number, amplitude = 0.5): Float32Array {
  const n = Math.round(rate * seconds);
  return Float32Array.from(
    { length: n },
    (_, i) => amplitude * Math.sin((2 * Math.PI * hz * i) / rate),
  );
}

function inChunks(resampler: Resampler, input: Float32Array, sizes: number[]): Float32Array {
  const parts: number[] = [];
  let offset = 0;
  let i = 0;
  while (offset < input.length) {
    const size = sizes[i++ % sizes.length] ?? 128;
    parts.push(...resampler.process(input.subarray(offset, offset + size)));
    offset += size;
  }
  return Float32Array.from(parts);
}

describe("16-bit PCM", () => {
  it("round-trips and clips", () => {
    const pcm = floatToPcm16(Float32Array.from([0, 0.5, -0.5, 1, -1, 2, -2]));
    const view = new DataView(pcm);
    expect(view.getInt16(2, true)).toBe(16384); // little-endian, as the server expects
    expect(view.getInt16(6, true)).toBe(32767);
    expect(view.getInt16(10, true)).toBe(32767); // clipped, not wrapped
    expect(view.getInt16(12, true)).toBe(-32768);
    const back = pcm16ToFloat(pcm);
    expect(back[1]).toBeCloseTo(0.5, 3);
    expect(back[2]).toBeCloseTo(-0.5, 3);
  });

  it("ignores a stray odd byte", () => {
    expect(pcm16ToFloat(new ArrayBuffer(5)).length).toBe(2);
  });
});

describe("Resampler", () => {
  it("gives the same result however the stream is chunked", () => {
    const input = tone(440, 48_000, 0.5);
    const whole = new Resampler(48_000, 16_000).process(input);
    const chunked = inChunks(new Resampler(48_000, 16_000), input, [128, 7, 333, 1, 1024]);
    expect(chunked.length).toBe(whole.length);
    for (let i = 0; i < whole.length; i++) expect(chunked[i]).toBeCloseTo(whole[i] ?? 0, 5);
  });

  it("converts 48 kHz to 16 kHz and 24 kHz to 48 kHz at the right length", () => {
    expect(new Resampler(48_000, 16_000).process(new Float32Array(48_000)).length).toBeCloseTo(
      16_000,
      -1,
    );
    expect(new Resampler(24_000, 48_000).process(new Float32Array(2_400)).length).toBeCloseTo(
      4_800,
      -1,
    );
  });

  it("keeps speech-band sound and removes what would alias", () => {
    const down = (hz: number) => {
      const out = new Resampler(48_000, 16_000).process(tone(hz, 48_000, 1));
      return rms(out.subarray(200)); // skip the filter's warm-up
    };
    expect(down(1_000)).toBeGreaterThan(0.3); // a 0.5 tone keeps its level (rms ≈ 0.35)
    expect(down(12_000)).toBeLessThan(0.02); // above 8 kHz: filtered, not folded back
  });

  it("passes audio through unchanged at the same rate", () => {
    const input = tone(300, 16_000, 0.1);
    expect(new Resampler(16_000, 16_000).process(input)).toEqual(input);
  });
});

describe("Framer", () => {
  it("makes exact 20 ms frames across pushes", () => {
    const framer = new Framer();
    expect(framer.push(new Float32Array(100))).toHaveLength(0);
    const frames = framer.push(new Float32Array(FRAME_SAMPLES * 2 + 50));
    expect(frames).toHaveLength(2);
    expect(frames.every((f) => f.length === FRAME_SAMPLES)).toBe(true);
    expect(framer.push(new Float32Array(170))).toHaveLength(1); // 100 + 50 + 170 = 320
  });
});

describe("silenceFrames", () => {
  it("makes the requested length of silence", () => {
    const frames = silenceFrames(0.8);
    expect(frames).toHaveLength(40);
    expect(frames[0]?.byteLength).toBe(FRAME_SAMPLES * 2);
  });
});
