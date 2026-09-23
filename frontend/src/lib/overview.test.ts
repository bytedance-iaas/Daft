import { describe, expect, it } from 'vitest';
import { ACTIVE_PAGER_PX, ACTIVE_ROW_PX, activePageSize, readOverviewPeriod, writeOverviewPeriod } from './overview';
import { writePrefs } from './prefs';

describe('the overview period (a UI preference)', () => {
  it('starts at 7 days and remembers the last choice', () => {
    expect(readOverviewPeriod()).toBe(7);
    writeOverviewPeriod(90);
    expect(readOverviewPeriod()).toBe(90);
    writeOverviewPeriod(365);
    expect(readOverviewPeriod()).toBe(365);
  });

  it('ignores what is not one of the four periods', () => {
    writePrefs({ overviewDays: 14 });
    expect(readOverviewPeriod()).toBe(7);
  });
});

describe('the running list pages to fit its height', () => {
  it('shows everything when it fits', () => {
    expect(activePageSize(3 * ACTIVE_ROW_PX, 3)).toBe(3);
    expect(activePageSize(1000, 1)).toBe(1);
  });

  it('keeps room for the pager when it does not', () => {
    expect(activePageSize(200, 4)).toBe(3); // 4 × 56 > 200; (200 − 32) / 56 = 3
    expect(activePageSize(3 * ACTIVE_ROW_PX + ACTIVE_PAGER_PX - 1, 10)).toBe(2);
    expect(activePageSize(1000, 20)).toBe(17);
  });

  it('shows at least one row, even without room or tasks', () => {
    expect(activePageSize(0, 0)).toBe(1);
    expect(activePageSize(0, 5)).toBe(1);
    expect(activePageSize(40, 2)).toBe(1);
  });
});
