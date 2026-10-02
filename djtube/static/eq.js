export const EQ_BANDS = ["high", "mid", "low"];
export const EQ_CUT_DB = -36;
export const EQ_BOOST_DB = 12;
export const EQ_STEP = 0.1;

export const EQ_FILTERS = {
  high: { type: "highshelf", frequency: 10000, Q: 0.7 },
  mid: { type: "peaking", frequency: 1000, Q: 0.9 },
  low: { type: "lowshelf", frequency: 100, Q: 0.7 },
};

export function clampEqUnit(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 0.5;
  const clamped = Math.min(1, Math.max(0, numeric));
  return Math.round(clamped * 1000) / 1000;
}

/** 0 is full cut, 0.5 is 0 dB, 1 is full boost. */
export function eqGainDb(unit) {
  const x = clampEqUnit(unit);
  if (x >= 0.5) return ((x - 0.5) / 0.5) * EQ_BOOST_DB;
  return ((0.5 - x) / 0.5) * EQ_CUT_DB;
}

/** MIDI 64 is 0 dB, 0 is full cut, and 127 is full boost. */
export function eqUnitFromMidi(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 0.5;
  const midi = Math.min(127, Math.max(0, numeric));
  if (midi <= 64) return clampEqUnit((midi / 64) * 0.5);
  return clampEqUnit(0.5 + ((midi - 64) / 63) * 0.5);
}

export function formatEqDb(db) {
  const rounded = Math.round(Number(db) * 10) / 10;
  if (!Number.isFinite(rounded)) return "0.0 dB";
  if (rounded > 0) return `+${rounded.toFixed(1)} dB`;
  return `${rounded.toFixed(1)} dB`;
}

export function connectEqGraph(source, context) {
  const nodes = {};
  let previous = source;
  for (const band of EQ_BANDS) {
    const spec = EQ_FILTERS[band];
    const filter = context.createBiquadFilter();
    filter.type = spec.type;
    filter.frequency.value = spec.frequency;
    filter.Q.value = spec.Q;
    filter.gain.value = 0;
    previous.connect(filter);
    previous = filter;
    nodes[band] = filter;
  }
  previous.connect(context.destination);
  return nodes;
}
