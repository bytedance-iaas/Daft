import { describe, expect, it } from 'vitest';
import { checkedOf, countsOf, sectionDigest, verdictItems } from './sectionStats';

describe('a section\'s counts with skipped episodes (D94, design doc 28 §3)', () => {
  const counts = { total: 46, pass: 30, fail: 2, abstain: 4, scored: 0, error: 2, skipped: 8 };

  it('the episodes checked leave out those skipped without judging', () => {
    const c = countsOf({ counts })!;
    expect(c.skipped).toBe(8);
    expect(checkedOf(c)).toBe(38);
    expect(checkedOf(countsOf({ counts: { total: 5, pass: 5 } })!)).toBe(5);   // a report without the key
  });

  it('the digest and the verdict chart name them apart', () => {
    expect(sectionDigest({ counts })).toBe('判废 2 条，转人工 4 条，出错 2 条，跳过 8 条');
    expect(sectionDigest({ counts: { total: 10, pass: 7, skipped: 3 } })).toBe('7 条全部通过，跳过 3 条');
    expect(sectionDigest({ counts: { total: 3, skipped: 3 } })).toBe('跳过 3 条');
    expect(verdictItems(countsOf({ counts })!).map((i) => i.name)).toContain('跳过');
  });
});
