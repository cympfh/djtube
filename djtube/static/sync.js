/**
 * One beat-sync adjustment. While a deck is locked, the caller repeats the
 * rate. Phase is applied when sync starts and when the leader jumps, not on
 * every tick. Nothing here starts, stops, or seeks.
 *
 * Rate is (otherBpm * otherRate) / ownBpm, folded by half and double until it
 * is as close to 1 as those octaves allow. 67.9 against 135.8 stays at 1×.
 * If it is still outside 0.5–2 after folding, there is no rate: do not clamp.
 *
 * Phase is wall-clock seconds since the last beat:
 *   mod(currentTime - beatOffset, 60 / bpm) / rate
 * The shorter beat grid is the pulse. The follower seeks by at most half of that.
 */

const RATE_MIN = 0.5;
const RATE_MAX = 2;
const MAX_FOLDS = 8;

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
  let bestDist = Math.abs(ratio - 1);
  for (let octave = -MAX_FOLDS; octave <= MAX_FOLDS; octave += 1) {
    const factor = 2 ** octave;
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
 * Returns null when either BPM or the leader rate is unusable, or when half
 * and double still leave the rate outside 0.5–2. The follower's current rate
 * is not an input: the octave closest to 1× wins. Never clamped.
 */
export function beatSyncRate(ownBpm, otherBpm, otherRate) {
  if (!finiteBpm(ownBpm) || !finiteBpm(otherBpm) || !positiveRate(otherRate)) return null;
  let rate = (otherBpm * otherRate) / ownBpm;
  if (!Number.isFinite(rate) || !(rate > 0)) return null;
  for (let fold = 0; fold < MAX_FOLDS; fold += 1) {
    const half = rate * 0.5;
    const doubled = rate * 2;
    const dist = Math.abs(rate - 1);
    const halfDist = Math.abs(half - 1);
    const doubleDist = Math.abs(doubled - 1);
    if (halfDist + 1e-9 < dist && halfDist <= doubleDist + 1e-9) rate = half;
    else if (doubleDist + 1e-9 < dist) rate = doubled;
    else break;
  }
  if (rate < RATE_MIN - 1e-9 || rate > RATE_MAX + 1e-9) return null;
  return rate;
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
  for (let step = 0; step < steps + 1 && step < 64; step += 1) {
    targets.push(mod(base + step * pulse, period));
  }
  return targets;
}

function nearestWallDelta(ownWall, otherWall, ownHeard, otherHeard, factor) {
  if (Math.abs(factor - 1) < 1e-6) {
    const scaled = otherHeard > 0 ? otherWall * (ownHeard / otherHeard) : otherWall;
    return circularDelta(ownWall, mod(scaled, ownHeard), ownHeard);
  }
  if (factor > 1) return circularDelta(ownWall, mod(otherWall, ownHeard), ownHeard);
  if (factor < 1) {
    let best = null;
    for (const target of gridTargets(otherWall, otherHeard, ownHeard)) {
      const delta = circularDelta(ownWall, target, ownHeard);
      if (best == null || Math.abs(delta) < Math.abs(best) - 1e-12) best = delta;
    }
    return best ?? 0;
  }
  return 0;
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
