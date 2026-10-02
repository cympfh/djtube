export function formatClock(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '--:--';
  const total = Math.floor(seconds);
  const secs = total % 60;
  const minsTotal = Math.floor(total / 60);
  const ss = String(secs).padStart(2, '0');
  if (minsTotal >= 60) {
    const hours = Math.floor(minsTotal / 60);
    const mins = minsTotal % 60;
    return `${hours}:${String(mins).padStart(2, '0')}:${ss}`;
  }
  return `${minsTotal}:${ss}`;
}

export function formatDuration(seconds: number | null): string {
  if (seconds == null) return '';
  if (seconds < 0) return 'ライブ';
  return formatClock(seconds);
}
