import { describe, expect, it } from 'vitest';
import { formatClock, formatDuration } from './format';

describe('formatClock', () => {
  it('formats minutes and hours', () => {
    expect(formatClock(0)).toBe('0:00');
    expect(formatClock(65)).toBe('1:05');
    expect(formatClock(3661)).toBe('1:01:01');
    expect(formatClock(Number.NaN)).toBe('--:--');
  });

  it('marks livestreams', () => {
    expect(formatDuration(-1)).toBe('ライブ');
    expect(formatDuration(null)).toBe('');
    expect(formatDuration(90)).toBe('1:30');
  });
});
