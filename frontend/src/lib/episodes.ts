// Episode selection helpers. The expression is validated and expanded by the server (07 §3:
// 「表达式由后端校验，前端不自己解析」); this display parser only keeps the preview grid and the
// 「已选 N / M 条」 counter in step with the text box, and gives up on anything but N and A-B.

export interface DisplayParse {
  ok: boolean;
  indices: Set<number>;
}

const TERM = /^\s*(\d+)\s*(?:-\s*(\d+)\s*)?$/;

export function parseForDisplay(expr: string, total?: number | null): DisplayParse {
  const indices = new Set<number>();
  const text = expr.trim();
  if (!text) return { ok: true, indices };
  for (const part of text.split(',')) {
    const m = TERM.exec(part);
    if (!m) return { ok: false, indices: new Set() };
    const a = Number(m[1]);
    const b = m[2] === undefined ? a : Number(m[2]);
    if (b < a) return { ok: false, indices: new Set() };
    const hi = total ? Math.min(b, total - 1) : b;
    if (hi - a > 200_000) return { ok: false, indices: new Set() };
    for (let i = a; i <= hi; i += 1) indices.add(i);
  }
  return { ok: true, indices };
}

/** [3, 10, 11, 12, 34] → "3,10-12,34" */
export function toExpr(indices: Iterable<number>): string {
  const sorted = [...new Set(indices)].sort((x, y) => x - y);
  const parts: string[] = [];
  let i = 0;
  while (i < sorted.length) {
    let j = i;
    while (j + 1 < sorted.length && sorted[j + 1] === sorted[j] + 1) j += 1;
    parts.push(j > i ? `${sorted[i]}-${sorted[j]}` : String(sorted[i]));
    i = j + 1;
  }
  return parts.join(',');
}

/** Toggles one index in an expression; returns null when the expression is not display-parsable. */
export function toggleInExpr(expr: string, index: number): string | null {
  const p = parseForDisplay(expr);
  if (!p.ok) return null;
  if (p.indices.has(index)) p.indices.delete(index);
  else p.indices.add(index);
  return toExpr(p.indices);
}

export type EpisodeMode = 'all' | 'head' | 'explicit';

/** How many episodes a selection covers, for the counter and the footer; null = unknown. */
export function selectionCount(mode: EpisodeMode, headN: number | undefined, expr: string, total: number | null | undefined): number | null {
  if (mode === 'all') return total ?? null;
  if (mode === 'head') return headN ? (total ? Math.min(headN, total) : headN) : 0;
  const p = parseForDisplay(expr, total);
  return p.ok ? p.indices.size : null;
}
