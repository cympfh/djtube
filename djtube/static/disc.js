/** Degrees the ring turns per second of media time. playbackRate speeds that clock. */
export const DISC_DEGREES_PER_SECOND = 180;

export function discRotationDegrees(mediaTime) {
  const time = Number(mediaTime);
  if (!Number.isFinite(time) || time <= 0) return 0;
  return (time * DISC_DEGREES_PER_SECOND) % 360;
}
