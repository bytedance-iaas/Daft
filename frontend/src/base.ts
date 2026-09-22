// The mount prefix (doc 07 §2.3). Everything that becomes a URL is built from here:
// the router basename, the REST prefix and the SSE endpoint. Never assume the root path.

declare global {
  interface Window {
    __CURATOR_BASE__?: string;
  }
}

/** Normalizes a prefix: '' or '/curation' (leading slash, no trailing slash). */
export function normalizeBase(raw: unknown): string {
  if (typeof raw !== 'string') return '';
  const trimmed = raw.trim().replace(/\/+$/, '');
  if (!trimmed) return '';
  return trimmed.startsWith('/') ? trimmed : `/${trimmed}`;
}

/** The prefix injected by the Daemon into index.html; '' when served at the root. */
export function getBase(): string {
  return normalizeBase(typeof window === 'undefined' ? '' : window.__CURATOR_BASE__);
}

/** React Router basename ('/' at the root). */
export function routerBasename(): string {
  return getBase() || '/';
}

function origin(): string {
  return typeof window === 'undefined' ? 'http://localhost' : window.location.origin;
}

/** Absolute REST prefix, e.g. https://host/curation/api/v1 */
export function apiBaseUrl(): string {
  return `${origin()}${getBase()}/api/v1`;
}

/** SSE endpoint for one task, e.g. /curation/events/tasks/task_1 */
export function taskEventsUrl(taskId: string): string {
  return `${getBase()}/events/tasks/${encodeURIComponent(taskId)}`;
}

/** A front-end route as an absolute path under the prefix (for links opened outside the router). */
export function appPath(route: string): string {
  const r = route.startsWith('/') ? route : `/${route}`;
  return `${getBase()}${r}`;
}
