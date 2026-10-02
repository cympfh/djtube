export function formatTime(seconds) {
  if (seconds == null || !Number.isFinite(Number(seconds)) || Number(seconds) < 0) return "–:––";
  const total = Math.floor(Number(seconds));
  const secs = total % 60;
  const mins = Math.floor(total / 60);
  if (mins >= 60) {
    const hours = Math.floor(mins / 60);
    const rest = mins % 60;
    return `${hours}:${String(rest).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }
  return `${mins}:${String(secs).padStart(2, "0")}`;
}
