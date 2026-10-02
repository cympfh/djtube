/** Keyboard nudge, as a fraction of the fader throw (0 = full A, 1 = full B). */
export const CROSSFADER_STEP = 0.05;

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export function clampCrossfader(position: number): number {
  const clamped = clamp(position, 0, 1);
  return Math.round(clamped * 100) / 100;
}

export function nudgeCrossfader(position: number, delta: number): number {
  return clampCrossfader(position + delta);
}

/** Equal-power curve so the middle of the fader stays loud. */
export function equalPowerGains(position: number): { a: number; b: number } {
  const x = clamp(position, 0, 1) * (Math.PI / 2);
  return { a: Math.cos(x), b: Math.sin(x) };
}

export function crossfaderLabel(position: number): string {
  const x = clampCrossfader(position);
  const gains = equalPowerGains(x);
  const a = Math.round(gains.a * 100);
  const b = Math.round(gains.b * 100);
  const mix = `A${a} / B${b}`;
  if (x <= 0) return `Aいっぱい（${mix}）`;
  if (x >= 1) return `Bいっぱい（${mix}）`;
  if (Math.abs(x - 0.5) <= 0.02) return `中央（${mix}）`;
  return mix;
}
