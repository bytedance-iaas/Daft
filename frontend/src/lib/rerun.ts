// 「可视化」 (requester item 22, D48): the ReRun web viewer of the same deployment shows a dataset.
// The console is mounted under a prefix ('/curation' in production) and the viewer sits one level
// above it ('/' then). The viewer takes `?url=<value>`, the value being the URL-encoded
// `tos://bucket/prefix/name/`, with `?region=<region>` appended to the tos URL itself (ReRun parses
// `tos://…/?region=cn-beijing`); the dataset name is the last path segment. Credentials come from
// the viewer's own deployment configuration, never from here.
import { getBase, normalizeBase } from '../base';

/** The path one level above a mount prefix: '/curation' → '', '/a/b' → '/a', '' → ''. */
export function parentBase(base: string): string {
  const b = normalizeBase(base);
  return b.slice(0, Math.max(0, b.lastIndexOf('/')));
}

/** Only datasets on TOS can be opened; a locally mounted one cannot. */
export function canVisualize(d: { uri: string }): boolean {
  return /^tos:\/\/[^/]+\/./.test(d.uri.trim());
}

/** The viewer URL for a dataset, or null when it cannot be visualized (not on TOS). */
export function rerunViewerUrl(
  d: { uri: string; region?: string | null },
  base: string = getBase(),
  origin: string = typeof window === 'undefined' ? '' : window.location.origin,
): string | null {
  if (!canVisualize(d)) return null;
  const target = d.uri.trim().replace(/\/?$/, '/') + (d.region ? `?region=${d.region}` : '');
  return `${origin}${parentBase(base)}/?url=${encodeURIComponent(target)}`;
}
