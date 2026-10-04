// What a platter spin should sound like. The deck tempo is a separate setting.

/** Wall time assumed for the first tick, before a gap between ticks is known. */
export const JOG_FIRST_TICK_SECONDS = 0.02;
/** No further platter ticks for this long means the hand has left the disc. */
export const JOG_RELEASE_MS = 80;
/** Media elements stop following a rate outside this range. */
export const SPIN_RATE_MIN = 0.0625;
export const SPIN_RATE_MAX = 16;

export function clampSpinRate(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric <= 0) return null;
  return Math.min(SPIN_RATE_MAX, Math.max(SPIN_RATE_MIN, numeric));
}

export function jogWallSeconds(elapsedSeconds) {
  const wall = Number(elapsedSeconds);
  if (Number.isFinite(wall) && wall > 0) return wall;
  return JOG_FIRST_TICK_SECONDS;
}

/** Chirp played while the platter moves backward. Faster spin, faster and higher chirp. */
export function scratchFromSpeed(speed) {
  const rate = Number(speed);
  if (!Number.isFinite(rate) || rate <= 0) return null;
  return {
    rate,
    chirpHz: 220 + rate * 80,
  };
}

/**
 * Backward: the scratch, not the track.
 * Forward: the track at media-seconds per wall-second, so a faster spin is heard faster.
 */
export function jogSpinPlan(deltaSeconds, elapsedSeconds) {
  const delta = Number(deltaSeconds);
  if (!Number.isFinite(delta) || delta === 0) return null;
  const speed = Math.abs(delta) / jogWallSeconds(elapsedSeconds);
  if (delta < 0) {
    return {
      direction: "backward",
      hear: "scratch",
      trackRate: null,
      scratch: scratchFromSpeed(speed),
    };
  }
  return {
    direction: "forward",
    hear: "track",
    trackRate: speed,
    scratch: null,
  };
}

/** Hand off the platter: the deck's own tempo, no scratch. */
export function jogReleaseHear(deckRate) {
  const rate = Number(deckRate);
  return {
    direction: "stop",
    hear: "deck",
    trackRate: Number.isFinite(rate) ? rate : 1,
    scratch: null,
  };
}
