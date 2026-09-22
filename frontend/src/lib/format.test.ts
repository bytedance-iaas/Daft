import { describe, expect, it } from 'vitest';
import { bytes, compactNumber, grouped, percent, relativeTimeText, totalTokens } from './format';

describe('format', () => {
  it('compacts token counts like the mockups (no money, D12)', () => {
    expect(compactNumber(2010000)).toBe('2.01M');
    expect(compactNumber(67500)).toBe('67.5K');
    expect(compactNumber(931800)).toBe('931.8K');
    expect(compactNumber(886)).toBe('886');
    expect(compactNumber(5000)).toBe('5,000');
    expect(compactNumber(null)).toBe('—');
  });

  it('formats percentages, groups and sizes', () => {
    expect(percent(0.82)).toBe('82%');
    expect(percent(0.945)).toBe('94.5%');
    expect(percent(null)).toBe('—');
    expect(grouped(51200)).toBe('51,200');
    expect(bytes(1520331122)).toBe('1.42 GiB');
    expect(bytes(512)).toBe('512 B');
  });

  it('relative time is Chinese', () => {
    const now = Date.UTC(2026, 8, 21, 12, 0, 0);
    expect(relativeTimeText(now - 10_000, now)).toBe('刚刚');
    expect(relativeTimeText(now - 3 * 60_000, now)).toBe('3 分钟前');
  });

  it('total tokens = input + output', () => {
    expect(totalTokens({ prompt_tokens: 2010000, completion_tokens: 67500 })).toBe(2077500);
    expect(totalTokens(null)).toBe(0);
  });
});
