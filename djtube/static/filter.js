// One filter per deck. Center is a true bypass. Left of center is a low-pass
// whose cutoff falls toward the left. Right of center is a high-pass whose
// cutoff rises toward the right.

export const FILTER_STEP = 0.05;
export const FILTER_CENTER = 0.5;
/** Full counterclockwise: bass remains, highs are gone. */
export const FILTER_LPF_MIN_HZ = 80;
/** Just left of center: low-pass is still almost open. */
export const FILTER_LPF_OPEN_HZ = 16000;
/** Just right of center: high-pass is still almost open. */
export const FILTER_HPF_OPEN_HZ = 40;
/** Full clockwise: bass is gone, highs remain. */
export const FILTER_HPF_MAX_HZ = 10000;
/** Butterworth. No resonant peak. */
export const FILTER_Q = Math.SQRT1_2;

export function clampFilterUnit(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return FILTER_CENTER;
  const clamped = Math.min(1, Math.max(0, numeric));
  return Math.round(clamped * 1000) / 1000;
}

/** MIDI 64 is center. 0 is full low-pass. 127 is full high-pass. */
export function filterUnitFromMidi(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return FILTER_CENTER;
  const midi = Math.min(127, Math.max(0, numeric));
  if (midi <= 64) return clampFilterUnit((midi / 64) * 0.5);
  return clampFilterUnit(0.5 + ((midi - 64) / 63) * 0.5);
}

function logFrequency(minHz, maxHz, amount) {
  const t = Math.min(1, Math.max(0, amount));
  if (t === 0) return minHz;
  if (t === 1) return maxHz;
  const min = Math.log(minHz);
  const max = Math.log(maxHz);
  return Math.exp(min + (max - min) * t);
}

export function filterSpec(unit) {
  const x = clampFilterUnit(unit);
  if (x === FILTER_CENTER) return { bypass: true };
  if (x < FILTER_CENTER) {
    return {
      bypass: false,
      type: "lowpass",
      frequency: logFrequency(FILTER_LPF_MIN_HZ, FILTER_LPF_OPEN_HZ, x / FILTER_CENTER),
      Q: FILTER_Q,
    };
  }
  return {
    bypass: false,
    type: "highpass",
    frequency: logFrequency(FILTER_HPF_OPEN_HZ, FILTER_HPF_MAX_HZ, (x - FILTER_CENTER) / FILTER_CENTER),
    Q: FILTER_Q,
  };
}

function formatHz(hz) {
  if (hz >= 1000) return `${(hz / 1000).toFixed(1)} kHz`;
  return `${Math.round(hz)} Hz`;
}

/** Center reads as no filter. Either side names the filter and its cutoff. */
export function formatFilter(unit) {
  const spec = filterSpec(unit);
  if (spec.bypass) return "なし";
  const name = spec.type === "lowpass" ? "ローパス" : "ハイパス";
  return `${name} ${formatHz(spec.frequency)}`;
}

/**
 * A peaking biquad at 0 dB is unity: numerator and denominator match, so the
 * signal is unchanged. Center uses that, rather than a low-pass or high-pass
 * left at a cutoff.
 */
export function applyFilter(node, unit) {
  const spec = filterSpec(unit);
  if (spec.bypass) {
    node.type = "peaking";
    node.frequency.value = 1000;
    node.Q.value = 1;
    node.gain.value = 0;
    return spec;
  }
  node.type = spec.type;
  node.frequency.value = spec.frequency;
  node.Q.value = spec.Q;
  node.gain.value = 0;
  return spec;
}

/**
 * Connect the deck filter after the EQ tail and on to the speakers.
 * `tail` must not already be connected. This is the only path out of the deck.
 */
export function connectDeckFilter(tail, context) {
  const filter = context.createBiquadFilter();
  applyFilter(filter, FILTER_CENTER);
  tail.connect(filter);
  filter.connect(context.destination);
  return filter;
}
