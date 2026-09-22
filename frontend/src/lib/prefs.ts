// UI preferences only (D17): page sizes, last used names for pre-selection, collapsed panels.
// Nothing here is business state; losing it only changes a default.

const PREFIX = 'curator.ui.';

export interface Prefs {
  /** Rows per page, keyed by table. */
  pageSize: Record<string, number>;
  /** Last access key picked for a dataset (doc 07 §2.1: several keys → pick the last used). */
  lastCredential?: string;
  /** Last access key used for a delivery directory. */
  lastOutputCredential?: string;
  /** Last VLM backend and model. */
  lastBackend?: string;
  lastModel?: string;
  /** Parent of the last delivery directory (「上次用过的交付根目录」). */
  lastDeliveryRoot?: string;
  /** Last region picked. */
  lastRegion?: string;
  /** Log tab: follow latest. */
  followLogs?: boolean;
}

function storage(): Storage | null {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null;
  } catch {
    return null;
  }
}

export function readPrefs(): Prefs {
  const s = storage();
  const fallback: Prefs = { pageSize: {} };
  if (!s) return fallback;
  try {
    const raw = s.getItem(`${PREFIX}prefs`);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw) as Partial<Prefs>;
    return { ...fallback, ...parsed, pageSize: { ...(parsed.pageSize ?? {}) } };
  } catch {
    return fallback;
  }
}

export function writePrefs(patch: Partial<Prefs>): void {
  const s = storage();
  if (!s) return;
  const next = { ...readPrefs(), ...patch };
  try {
    s.setItem(`${PREFIX}prefs`, JSON.stringify(next));
  } catch {
    // Quota or privacy mode: preferences are optional.
  }
}

export const PAGE_SIZES = [10, 20, 50, 100] as const;
export type PageSize = (typeof PAGE_SIZES)[number];

export function readPageSize(table: string, fallback: PageSize = 20): PageSize {
  const v = readPrefs().pageSize[table];
  return (PAGE_SIZES as readonly number[]).includes(v ?? -1) ? (v as PageSize) : fallback;
}

export function writePageSize(table: string, size: number): void {
  const prefs = readPrefs();
  writePrefs({ pageSize: { ...prefs.pageSize, [table]: size } });
}
