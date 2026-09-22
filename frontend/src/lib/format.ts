// Number, size and token formatting shared by every page. Pure functions, unit tested.
import dayjs from 'dayjs';
import relativeTime from 'dayjs/plugin/relativeTime';
import 'dayjs/locale/zh-cn';
import { zh } from '../locales/zh';

dayjs.extend(relativeTime);
dayjs.locale('zh-cn');

/** 2010000 → "2.01M", 67500 → "67.5K", 886 → "886". Token counts are never turned into money (D12). */
export function compactNumber(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  const abs = Math.abs(n);
  const fmt = (v: number, unit: string) => `${Number(v.toFixed(2)).toString()}${unit}`;
  if (abs >= 1e9) return fmt(n / 1e9, 'B');
  if (abs >= 1e6) return fmt(n / 1e6, 'M');
  if (abs >= 1e4) return fmt(n / 1e3, 'K');
  return n.toLocaleString('en-US');
}

/** 51200 → "51,200" */
export function grouped(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return n.toLocaleString('en-US');
}

/** 0.82 → "82%", 0.945 → "94.5%" */
export function percent(ratio: number | null | undefined, digits = 1): string {
  if (ratio === null || ratio === undefined || Number.isNaN(ratio)) return '—';
  const v = ratio * 100;
  return `${Number(v.toFixed(digits)).toString()}%`;
}

/** 1520331122 → "1.42 GiB" */
export function bytes(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${i === 0 ? v : Number(v.toFixed(2))} ${units[i]}`;
}

/** Local absolute time, e.g. 2026-09-20 13:05:12 */
export function absoluteTime(ms: number | null | undefined): string {
  if (!ms) return '—';
  return dayjs(ms).format('YYYY-MM-DD HH:mm:ss');
}

/** Short local time for dense tables: 09-20 13:05 */
export function shortTime(ms: number | null | undefined): string {
  if (!ms) return '—';
  return dayjs(ms).format('MM-DD HH:mm');
}

/** Relative time in Chinese: 「3 分钟前」 */
export function relativeTimeText(ms: number | null | undefined, now: number = Date.now()): string {
  if (!ms) return '—';
  if (Math.abs(now - ms) < 45_000) return zh.time.justNow;
  return dayjs(ms).from(dayjs(now));
}

/** Total tokens of a usage record: input + output (reasoning is part of output, cache hits of input). */
export function totalTokens(u: { prompt_tokens: number; completion_tokens: number } | null | undefined): number {
  if (!u) return 0;
  return u.prompt_tokens + u.completion_tokens;
}

/** "MMDD" for delivery directory suffixes (v1 suggest_delivery_name). */
export function monthDay(ms: number = Date.now()): string {
  return dayjs(ms).format('MMDD');
}
