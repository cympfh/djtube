export const RATE_MIN = 0.5;
export const RATE_MAX = 2;
/** Keyboard nudge. The deck slider and the FLX4 tempo fader are not snapped to this. */
export const RATE_STEP = 0.01;

export function clampRate(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 1;
  return Math.min(RATE_MAX, Math.max(RATE_MIN, numeric));
}

/** MIDI 64 is 1.0, 0 is 0.5, and 127 is 2.0. In-between values stay on that line. */
export function rateFromMidi(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 1;
  const midi = Math.min(127, Math.max(0, numeric));
  if (midi <= 64) return clampRate(RATE_MIN + (midi / 64) * (1 - RATE_MIN));
  return clampRate(1 + ((midi - 64) / 63) * (RATE_MAX - 1));
}

export function formatRate(rate) {
  return `${clampRate(rate).toFixed(2)}×`;
}
