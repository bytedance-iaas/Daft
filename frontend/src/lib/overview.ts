// The overview page (07 §4.3): the period it shows and how its list of running tasks pages.
import { readPrefs, writePrefs } from './prefs';

/** The periods `GET /overview?days=` takes (C4 1.10.0): 近 7 天, 近 1 月, 近 3 月 and 近 1 年. */
export const OVERVIEW_PERIODS = [7, 30, 90, 365] as const;
export type OverviewPeriod = (typeof OVERVIEW_PERIODS)[number];

export function isOverviewPeriod(value: unknown): value is OverviewPeriod {
  return (OVERVIEW_PERIODS as readonly unknown[]).includes(value);
}

/** The period this browser picked last (a UI preference, D17); 7 days until one is picked. */
export function readOverviewPeriod(): OverviewPeriod {
  const days = readPrefs().overviewDays;
  return isOverviewPeriod(days) ? days : 7;
}

export function writeOverviewPeriod(days: OverviewPeriod): void {
  writePrefs({ overviewDays: days });
}

/** Heights in px that styles.css gives a running-task row and the pager under the list («概览»). */
export const ACTIVE_ROW_PX = 56;
export const ACTIVE_PAGER_PX = 32;

/**
 * How many running tasks one page of the 运行情况 card shows, so the list fits in `height` px
 * (what the period card next to it leaves): all of them when they fit, otherwise as many as fit
 * above the pager - at least one.
 */
export function activePageSize(height: number, count: number): number {
  if (count * ACTIVE_ROW_PX <= height) return Math.max(1, count);
  return Math.max(1, Math.floor((height - ACTIVE_PAGER_PX) / ACTIVE_ROW_PX));
}
