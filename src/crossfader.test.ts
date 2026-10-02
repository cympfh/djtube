import { describe, expect, it } from 'vitest';
import { crossfaderLabel, equalPowerGains, nudgeCrossfader } from './crossfader';

describe('crossfader', () => {
  it('uses an equal-power curve and stays inside 0..1', () => {
    expect(equalPowerGains(0)).toEqual({ a: 1, b: 0 });
    expect(equalPowerGains(1).a).toBeCloseTo(0);
    expect(equalPowerGains(1).b).toBeCloseTo(1);
    const mid = equalPowerGains(0.5);
    expect(mid.a).toBeCloseTo(Math.SQRT1_2);
    expect(mid.b).toBeCloseTo(Math.SQRT1_2);
    expect(nudgeCrossfader(0, -0.05)).toBe(0);
    expect(nudgeCrossfader(1, 0.05)).toBe(1);
    expect(nudgeCrossfader(0.5, 0.05)).toBe(0.55);
  });

  it('names the center by the resulting volumes', () => {
    expect(crossfaderLabel(0.5)).toBe('中央（A71 / B71）');
    expect(crossfaderLabel(0)).toBe('Aいっぱい（A100 / B0）');
  });
});
