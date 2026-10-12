/**
 * One beat-sync adjustment. While a deck is locked, the caller repeats the
 * rate. Phase is applied when sync starts and when the leader jumps, not on
 * every tick. Nothing here starts, stops, or seeks.
 *
 * `beatSnapPlan` is the separate play-start nudge. It does not change rate.
 * The caller seeks only when the nearest beats are already close.
 *
 * The follower rate makes its heard BPM half, the same, or double the master
 * heard tempo. Closeness is |ln(currentHeard / targetHeard)| against the
 * pre-sync heard BPM. The caller stores that factor for the whole lock.
 * If the rate for that factor is outside 0.5–2, there is no rate: do not
 * clamp, and do not substitute a farther octave.
 *
 * Phase is wall-clock seconds since the last beat:
 *   mod(currentTime - beatOffset, 60 / bpm) / rate
 * The shorter beat grid is the pulse. A target before 0 or past the file
 * moves by that period instead of being clamped to the end.
 */

const RATE_MIN = 0.5;
const RATE_MAX = 2;
const HEARD_FACTORS = [0.5, 1, 2];

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

function syncFactor(value) {
  return value === 0.5 || value === 1 || value === 2;
}

/**
 * Half, same, or double of the master heard tempo.
 * Minimizes |ln(currentHeard / targetHeard)|. An equal distance keeps 1×.
 * Null when a BPM or rate is unusable. Does not apply the 0.5–2 rate limit.
 */
export function beatSyncFactor(ownBpm, otherBpm, otherRate, ownRate) {
  if (!finiteBpm(ownBpm) || !finiteBpm(otherBpm) || !positiveRate(otherRate) || !positiveRate(ownRate)) {
    return null;
  }
  const masterHeard = otherBpm * otherRate;
  const currentHeard = ownBpm * ownRate;
  if (!Number.isFinite(masterHeard) || !Number.isFinite(currentHeard) || !(masterHeard > 0)) return null;

  let best = null;
  for (const factor of HEARD_FACTORS) {
    const dist = Math.abs(Math.log(currentHeard / (masterHeard * factor)));
    const bias = Math.abs(Math.log(factor));
    if (
      !best
      || dist < best.dist - 1e-9
      || (Math.abs(dist - best.dist) <= 1e-9 && bias < best.bias - 1e-9)
    ) {
      best = { factor, dist, bias };
    }
  }
  return best ? best.factor : null;
}

function rateForFactor(ownBpm, otherBpm, otherRate, factor) {
  if (!finiteBpm(ownBpm) || !finiteBpm(otherBpm) || !positiveRate(otherRate) || !syncFactor(factor)) return null;
  const rate = (otherBpm * otherRate * factor) / ownBpm;
  if (!Number.isFinite(rate) || rate < RATE_MIN - 1e-9 || rate > RATE_MAX + 1e-9) return null;
  return rate;
}

/**
 * Follower playback rate for one octave of the master heard tempo.
 * Without `factor`, the octave is the log-closest of {0.5, 1, 2} to the
 * follower's current heard BPM (`ownBpm * ownRate`). With `factor`, that
 * octave is used as-is. Null when the rate is outside 0.5–2, or inputs are
 * unusable. Never clamped, and a farther octave is not substituted.
 */
export function beatSyncRate(ownBpm, otherBpm, otherRate, ownRate, factor) {
  const chosen = syncFactor(factor) ? factor : beatSyncFactor(ownBpm, otherBpm, otherRate, ownRate);
  if (chosen == null) return null;
  return rateForFactor(ownBpm, otherBpm, otherRate, chosen);
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
 * `factor` is the octave chosen when sync locked (0.5, 1, or 2). Omit it only
 * to choose from the follower's current rate.
 */
export function beatSyncPlan(own, other, factor) {
  if (!usableDeck(own) || !usableDeck(other)) return null;
  const chosen = syncFactor(factor) ? factor : beatSyncFactor(own.bpm, other.bpm, other.rate, own.rate);
  const rate = rateForFactor(own.bpm, other.bpm, other.rate, chosen);
  if (rate == null) return null;
  const ownHeard = 60 / (own.bpm * rate);
  const otherHeard = 60 / (other.bpm * other.rate);
  if (!(ownHeard > 0) || !(otherHeard > 0)) return null;
  const ownWall = wallSinceBeat(own.time, own.beatOffset, own.bpm, rate);
  const otherWall = wallSinceBeat(other.time, other.beatOffset, other.bpm, other.rate);
  const deltaWall = nearestWallDelta(ownWall, otherWall, ownHeard, otherHeard, chosen);
  if (!Number.isFinite(deltaWall)) return null;
  const period = Math.min(ownHeard, otherHeard) * rate;
  const time = placeSyncTime(own.time + deltaWall * rate, period);
  if (!Number.isFinite(time) || !(period > 0)) return null;
  return { rate, time, period, factor: chosen };
}

/**
 * Play-start snap window, in wall-clock seconds.
 * 80 ms is enough to close a flamming kick and too small to abandon a cue
 * the DJ placed off the beat. 20% of the shorter heard beat keeps a fast
 * grid from jumping by a large fraction of its own pulse (above ~150 BPM,
 * 20% is under 80 ms). A half-beat is 50% of that pulse, so it never snaps.
 */
export const BEAT_SNAP_MAX_SECONDS = 0.08;
export const BEAT_SNAP_MAX_FRACTION = 0.2;

function heardBeat(bpm, rate) {
  const heard = 60 / (bpm * rate);
  return heard > 0 && Number.isFinite(heard) ? heard : 0;
}

function beatSnapLimit(ownHeard, otherHeard) {
  const pulse = Math.min(ownHeard, otherHeard);
  if (!(pulse > 0)) return 0;
  return Math.min(BEAT_SNAP_MAX_SECONDS, BEAT_SNAP_MAX_FRACTION * pulse);
}

/** Beat instants near now. Negative is in the past. `wall` is seconds since the last beat. */
function beatInstants(wall, heard) {
  const instants = [];
  if (!(heard > 0) || !Number.isFinite(wall)) return instants;
  const reach = heard + 1;
  const first = Math.floor((-reach + wall) / heard) - 1;
  const last = Math.ceil((reach + wall) / heard) + 1;
  const stop = Math.min(last, first + 64);
  for (let k = first; k <= stop; k += 1) instants.push(k * heard - wall);
  return instants;
}

/** Beat instant nearest to now. Negative is in the past. Exact half keeps the previous beat. */
function nearestBeatToNow(wall, heard) {
  if (!(heard > 0) || !Number.isFinite(wall)) return null;
  const previous = -wall;
  const next = heard - wall;
  if (Math.abs(next) < Math.abs(previous) - 1e-12) return next;
  return previous;
}

/**
 * Wall-clock seconds to seek `own` so the beat of `own` nearest to now lands
 * on the nearest beat of `other`. Positive seeks forward.
 * A closer pair that sits far from the playhead is not the drop: 120 against
 * 128 can put two future beats 9 ms apart while the kicks at now are 40 ms
 * apart. Half and double tempos still meet on the nearer grid point.
 */
function nearestBeatShift(ownWall, ownHeard, otherWall, otherHeard) {
  const ownAt = nearestBeatToNow(ownWall, ownHeard);
  if (ownAt == null) return null;
  let best = null;
  for (const otherAt of beatInstants(otherWall, otherHeard)) {
    const delta = ownAt - otherAt;
    if (!Number.isFinite(delta)) continue;
    if (best == null || Math.abs(delta) < Math.abs(best) - 1e-12) best = delta;
  }
  return best;
}

/**
 * File time for the deck that is starting, or null when it should stay.
 * Null when either grid is missing or still measuring, the nearest beat is
 * outside the snap window, the decks are already together, or the corrected
 * time would leave the file. Unlike sync, an out-of-range target does not
 * step by a whole beat. `other` is the deck already playing.
 */
export function beatSnapPlan(own, other, duration = Number.POSITIVE_INFINITY) {
  if (!usableDeck(own) || !usableDeck(other)) return null;
  const ownHeard = heardBeat(own.bpm, own.rate);
  const otherHeard = heardBeat(other.bpm, other.rate);
  const limit = beatSnapLimit(ownHeard, otherHeard);
  if (!(limit > 0)) return null;
  const ownWall = wallSinceBeat(own.time, own.beatOffset, own.bpm, own.rate);
  const otherWall = wallSinceBeat(other.time, other.beatOffset, other.bpm, other.rate);
  const deltaWall = nearestBeatShift(ownWall, ownHeard, otherWall, otherHeard);
  // 1 ns keeps an error that lands on the limit from falling outside via rounding.
  if (deltaWall == null || Math.abs(deltaWall) > limit + 1e-9) return null;
  if (Math.abs(deltaWall) < 1e-4) return null;
  const shifted = own.time + deltaWall * own.rate;
  if (!Number.isFinite(shifted) || shifted < -1e-6) return null;
  const time = shifted < 0 ? 0 : shifted;
  const end = Number(duration);
  if (Number.isFinite(end) && end > 0 && time > end + 1e-9) return null;
  return { time, deltaWall };
}
