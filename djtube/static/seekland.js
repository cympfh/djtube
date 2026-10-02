const LEAVE_EPSILON = 1e-3;

// True once playback has left the pre-jog time toward the command.
// Arriving on the command counts, including one 0.05s platter tick.
// A report still at the pre-jog time, or one that moved the other way, does not.
export function commandedSeekLanded(from, at, reported) {
  if (!Number.isFinite(from) || !Number.isFinite(at) || !Number.isFinite(reported)) return false;
  const span = at - from;
  if (span === 0) return false;
  const traveled = reported - from;
  const toward = span > 0 ? traveled : -traveled;
  const reached = span > 0 ? reported >= at - LEAVE_EPSILON : reported <= at + LEAVE_EPSILON;
  if (reached && toward > 0) return true;
  return toward > LEAVE_EPSILON;
}
