function clampGain(value) {
  if (!Number.isFinite(value)) return 0;
  return Math.min(1, Math.max(0, value));
}

/** Equal-power crossfade. 0 is deck A only, 1 is deck B only. */
export function deckGains(position) {
  const x = Math.min(1, Math.max(0, Number(position) || 0));
  const angle = x * (Math.PI / 2);
  return {
    a: clampGain(Math.cos(angle)),
    b: clampGain(Math.sin(angle)),
  };
}
