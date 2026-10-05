/** Degrees the ring turns per second of media time. playbackRate speeds that clock. */
export const DISC_DEGREES_PER_SECOND = 180;

export function discRotationDegrees(mediaTime) {
  const time = Number(mediaTime);
  if (!Number.isFinite(time) || time <= 0) return 0;
  return (time * DISC_DEGREES_PER_SECOND) % 360;
}

/** The ring is up once the deck can play, and while a press still holds one that was playing. */
export function discVisible(deckState) {
  if (!deckState?.id) return false;
  if (deckState.status === "ready") return true;
  if (deckState.playing) return true;
  return !!(deckState.discHeld && deckState.discWasPlaying);
}

/** The ring turns only while that deck is playing. A ready deck that is stopped stays still. */
export function discSpinning(deckState) {
  return !!(deckState?.id && deckState.playing);
}
