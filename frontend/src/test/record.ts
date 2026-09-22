// Records the REST calls the UI makes (listeners are removed after each test in setup.ts).
import { server } from '../mocks/server';

export interface SeenRequest {
  method: string;
  /** Path below /api/v1, e.g. /tasks/t1/retry */
  path: string;
  query: URLSearchParams;
  body: unknown;
  headers: Record<string, string>;
}

export function recordRequests(): SeenRequest[] {
  const seen: SeenRequest[] = [];
  server.events.on('request:start', async ({ request }) => {
    const url = new URL(request.url);
    if (!url.pathname.includes('/api/v1/')) return;
    const text = request.method === 'GET' ? '' : await request.clone().text();
    seen.push({
      method: request.method,
      path: url.pathname.replace(/.*\/api\/v1/, ''),
      query: url.searchParams,
      body: text ? JSON.parse(text) : null,
      headers: Object.fromEntries(request.headers.entries()),
    });
  });
  return seen;
}
