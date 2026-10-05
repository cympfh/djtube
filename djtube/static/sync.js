import { clampRate } from "./rate.js";

/**
 * One beat-sync adjustment. While a deck is locked, the caller repeats this
 * so the follower keeps the leader's tempo and phase. Nothing here starts,
 * stops, or seeks.
 *
 * Rate is (otherBpm * otherRate) / ownBpm, times 0.5, 1, or 2 — whichever lands
 * closest to 1. Half and double tempos are the same groove: 67.9 against 135.8
 * stays near 1× instead of jumping to 2×. The heard numbers do not have to match.
 *
 * Phase is wall-clock seconds since the last beat:
 *   mod(currentTime - beatOffset, 60 / bpm) / rate
 * The shorter beat grid is the pulse. The follower seeks by at most half of that.
 */

const RATE_FACTORS = [1, 0.5, 2];

function finiteBpm(value) {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

function finiteTime(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function positiveRate(value) {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

function mod(value, period) {
  if (!(period > 0) || !Number.isFinite(value)) return 0;
  const wrapped = value % period;
  return wrapped < 0 ? wrapped + period : wrapped;
}

/** Signed shortest move from current to target, in the same units as period. */
function circularDelta(current, target, period) {
  let delta = target - current;
  if (!(period > 0)) return 0;
  const half = period / 2;
  if (delta > half) delta -= period;
  else if (delta <= -half) delta += period;
  return delta;
}

function nearestFactor(ratio) {
  let best = 1;
  let bestDist = Infinity;
  for (const factor of RATE_FACTORS) {
    const dist = Math.abs(ratio - factor);
    if (dist < bestDist - 1e-9) {
      best = factor;
      bestDist = dist;
    }
  }
  return best;
}

/**
 * Follower playback rate that shares the leader's groove.
 * Returns null when either BPM or the leader rate is unusable.
 * The follower's current rate is not an input: the octave closest to 1× wins.
 */
export function beatSyncRate(ownBpm, otherBpm, otherRate) {
  if (!finiteBpm(ownBpm) || !finiteBpm(otherBpm) || !positiveRate(otherRate)) return null;
  const raw = (otherBpm * otherRate) / ownBpm;
  if (!Number.isFinite(raw) || !(raw > 0)) return null;
  let best = raw;
  let bestDist = Math.abs(raw - 1);
  for (const factor of [0.5, 2]) {
    const candidate = raw * factor;
    const dist = Math.abs(candidate - 1);
    if (dist < bestDist - 1e-9) {
      best = candidate;
      bestDist = dist;
    }
  }
  return clampRate(best);
}

/** Wall-clock seconds since the last beat at this file position and tempo. */
function wallSinceBeat(time, beatOffset, bpm, rate) {
  return mod(time - beatOffset, 60 / bpm) / rate;
}

function gridTargets(origin, pulse, period) {
  if (!(pulse > 0) || !(period > 0)) return [];
  const base = mod(origin, pulse);
  const steps = Math.max(1, Math.round(period / pulse));
  const targets = [];
  for (let step = 0; step < steps + 1 && step < 8; step += 1) {
    targets.push(mod(base + step * pulse, period));
  }
  return targets;
}

function nearestWallDelta(ownWall, otherWall, ownHeard, otherHeard, factor) {
  if (factor === 2) return circularDelta(ownWall, mod(otherWall, ownHeard), ownHeard);
  if (factor === 0.5) {
    let best = null;
    for (const target of gridTargets(otherWall, otherHeard, ownHeard)) {
      const delta = circularDelta(ownWall, target, ownHeard);
      if (best == null || Math.abs(delta) < Math.abs(best) - 1e-12) best = delta;
    }
    return best ?? 0;
  }
  const scaled = otherHeard > 0 ? otherWall * (ownHeard / otherHeard) : otherWall;
  return circularDelta(ownWall, mod(scaled, ownHeard), ownHeard);
}

function usableDeck(deck) {
  if (!deck || deck.measuring) return false;
  return finiteBpm(deck.bpm) && finiteTime(deck.beatOffset) && finiteTime(deck.time) && positiveRate(deck.rate);
}

/**
 * Rate and file time for the follower. Null when either side has no beat grid
 * (missing BPM, missing beatOffset, or still measuring). Does not read clocks.
 */
export function beatSyncPlan(own, other) {
  if (!usableDeck(own) || !usableDeck(other)) return null;
  const rate = beatSyncRate(own.bpm, other.bpm, other.rate);
  if (rate == null) return null;
  const ownHeard = 60 / (own.bpm * rate);
  const otherHeard = 60 / (other.bpm * other.rate);
  if (!(ownHeard > 0) || !(otherHeard > 0)) return null;
  const heardRatio = (own.bpm * rate) / (other.bpm * other.rate);
  if (!Number.isFinite(heardRatio) || !(heardRatio > 0)) return null;
  const factor = nearestFactor(heardRatio);
  const ownWall = wallSinceBeat(own.time, own.beatOffset, own.bpm, rate);
  const otherWall = wallSinceBeat(other.time, other.beatOffset, other.bpm, other.rate);
  const deltaWall = nearestWallDelta(ownWall, otherWall, ownHeard, otherHeard, factor);
  if (!Number.isFinite(deltaWall)) return null;
  const time = own.time + deltaWall * rate;
  if (!Number.isFinite(time)) return null;
  return { rate, time };
}
