import { formatRate } from "./rate.js";

/** Heard BPM is the track base times deck tempo. Unknown base stays null. One decimal. */
export function heardBpm(base, rate) {
  if (typeof base !== "number" || !Number.isFinite(base)) return null;
  if (typeof rate !== "number" || !Number.isFinite(rate)) return null;
  return Math.round(base * rate * 10) / 10;
}

/** One decimal, or an en dash when the base BPM is unknown. */
export function formatBpm(base, rate) {
  const heard = heardBpm(base, rate);
  if (heard == null) return "–";
  return heard.toFixed(1);
}

/** Visible readout. Uses deckState.rate, not a jog or spin rate. */
export function bpmText(deckState) {
  return `${formatBpm(deckState?.bpm, deckState?.rate)} BPM`;
}

/** Slider value text. The heard BPM is added only when a base exists. */
export function tempoValueText(deckState) {
  const rate = formatRate(deckState?.rate ?? 1);
  if (heardBpm(deckState?.bpm, deckState?.rate) == null) return rate;
  return `${rate}、${bpmText(deckState)}`;
}
