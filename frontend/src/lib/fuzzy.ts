// Typing a few letters to find something in a long list (07 §7: a backend can list a hundred
// models). Case-insensitive, and the letters only have to appear in order, so «d20pro» finds
// «doubao-seed-2-0-pro-260215».

/** Whether `query`'s characters appear in `text`, in order (spaces ignored). */
export function fuzzyMatch(text: string, query: string): boolean {
  const q = query.trim().toLowerCase().replace(/\s+/g, '');
  if (!q) return true;
  const t = text.toLowerCase();
  let i = 0;
  for (const c of t) {
    if (c === q[i]) i += 1;
    if (i === q.length) return true;
  }
  return false;
}

/** The matching items, the ones that contain the query as written first. */
export function fuzzyFilter<T>(items: readonly T[], query: string, text: (item: T) => string): T[] {
  const q = query.trim().toLowerCase();
  if (!q) return [...items];
  const hits = items.filter((i) => fuzzyMatch(text(i), q));
  const contains = (i: T) => text(i).toLowerCase().includes(q.replace(/\s+/g, ''));
  return [...hits.filter(contains), ...hits.filter((i) => !contains(i))];
}
