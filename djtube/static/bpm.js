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
