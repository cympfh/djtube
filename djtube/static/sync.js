/**
 * One beat-sync adjustment. While a deck is locked, the caller repeats the
 * rate. Phase is applied when sync starts and when the leader jumps, not on
 * every tick. Nothing here starts, stops, or seeks.
 *
 * The follower rate is the speed whose heard BPM is half, the same, or double
 * the master heard tempo — whichever is closest to the follower's current
 * heard BPM (own BPM × current rate, before this call overwrites it). If that
 * rate is outside 0.5–2, there is no rate: do not clamp, and do not substitute
 * a farther octave.
 *
 * Phase is wall-clock seconds since the last beat:
 *   mod(currentTime - beatOffset, 60 / bpm) / rate
 * The shorter beat grid is the pulse. A target before 0 or past the file
 * moves by that period instead of being clamped to the end.
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

const HEARD_FACTORS = [0.5, 1, 2];

/**
 * Follower playback rate that shares the leader's groove at half, the same,
 * or double the master heard tempo.
 *
 * `ownRate` is the follower tempo already on the deck, before sync overwrites
 * it. Heard BPM is file BPM × that rate. The chosen target minimizes
 * |currentHeard − masterHeard × {0.5, 1, 2}|. An equal distance keeps the
 * factor closer to 1× (same, then half). If that rate is outside 0.5–2, the
 * result is null: a farther octave is not substituted, and the rate is not
 * clamped. Null also when a BPM or rate is unusable.
 */
export function beatSyncRate(ownBpm, otherBpm, otherRate, ownRate) {
  if (!finiteBpm(ownBpm) || !finiteBpm(otherBpm) || !positiveRate(otherRate) || !positiveRate(ownRate)) {
    return null;
  }
  const masterHeard = otherBpm * otherRate;
  const currentHeard = ownBpm * ownRate;
  if (!Number.isFinite(masterHeard) || !Number.isFinite(currentHeard) || !(masterHeard > 0)) return null;

  let best = null;
  for (const factor of HEARD_FACTORS) {
    const targetHeard = masterHeard * factor;
    const dist = Math.abs(currentHeard - targetHeard);
    const bias = Math.abs(factor - 1);
    if (
      !best
      || dist < best.dist - 1e-9
      || (Math.abs(dist - best.dist) <= 1e-9 && bias < best.bias - 1e-9)
    ) {
      best = { factor, dist, bias };
    }
  }
  if (!best) return null;
  const rate = (masterHeard * best.factor) / ownBpm;
  if (!Number.isFinite(rate) || rate < RATE_MIN - 1e-9 || rate > RATE_MAX + 1e-9) return null;
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

/**
 * Slide a file time onto the same beat inside the track.
 * Negative targets step forward by `period`. Targets past `duration` step back.
 * `period` is the shorter beat in follower file seconds when tempos are half or double.
 */
export function placeSyncTime(time, period, duration = Number.POSITIVE_INFINITY) {
  if (!Number.isFinite(time) || !(period > 0)) return time;
  let next = time;
  let steps = 0;
  while (next < -1e-9 && steps < 10000) {
    next += period;
    steps += 1;
  }
  const end = Number(duration);
  if (Number.isFinite(end) && end > 0) {
    steps = 0;
    while (next > end + 1e-9 && next - period >= -1e-9 && steps < 10000) {
      next -= period;
      steps += 1;
    }
  }
  return next;
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
  const rate = beatSyncRate(own.bpm, other.bpm, other.rate, own.rate);
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
  const period = Math.min(ownHeard, otherHeard) * rate;
  const time = placeSyncTime(own.time + deltaWall * rate, period);
  if (!Number.isFinite(time) || !(period > 0)) return null;
  return { rate, time, period };
}
