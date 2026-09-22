// A thin typed client over the C4 contract: openapi-fetch gives path/params/body typing from the
// generated schema; unwrap() turns every non-2xx response into an ApiError carrying the one
// Error body (doc 03 §1).
import createClient, { type Client } from 'openapi-fetch';
import { apiBaseUrl } from '../base';
import { ApiError } from './errors';
import type { paths } from './schema';

let cached: { url: string; client: Client<paths> } | null = null;

const READS = new Set(['GET', 'HEAD', 'OPTIONS']);

/**
 * Every write says `Content-Type: application/json`, even without a body (openapi-fetch only sets
 * it when there is one). C4 1.2 requires it and the Daemon refuses writes that do not; 1.1 allows it.
 */
export function withJsonContentType(request: Request): Request {
  if (READS.has(request.method.toUpperCase()) || request.headers.has('Content-Type')) return request;
  const headers = new Headers(request.headers);
  headers.set('Content-Type', 'application/json');
  return new Request(request, { headers });
}

/** The client for the current mount prefix (recreated if the prefix changes, e.g. in tests). */
export function api(): Client<paths> {
  const url = apiBaseUrl();
  if (!cached || cached.url !== url) {
    const client = createClient<paths>({
      baseUrl: url,
      // Resolve fetch at call time so test doubles and MSW interceptors are always honoured.
      fetch: (input: Request) => globalThis.fetch(input),
      headers: { Accept: 'application/json' },
    });
    client.use({ onRequest: ({ request }) => withJsonContentType(request) });
    cached = { url, client };
  }
  return cached.client;
}

interface FetchResult<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/** Awaits an openapi-fetch call and returns its data, or throws ApiError. */
export async function unwrap<T>(call: Promise<FetchResult<T>>): Promise<T> {
  let result: FetchResult<T>;
  try {
    result = await call;
  } catch (cause) {
    if (cause instanceof ApiError) throw cause;
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
    throw ApiError.network(cause);
  }
  if (!result.response.ok) throw ApiError.fromResponse(result.response.status, result.error);
  return result.data as T;
}

/** A fresh Idempotency-Key (doc 03 §8) for one user action. */
export function idempotencyKey(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c && typeof c.randomUUID === 'function') return `ui-${c.randomUUID()}`;
  return `ui-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}
