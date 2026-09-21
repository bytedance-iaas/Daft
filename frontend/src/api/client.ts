// A thin typed client over the C4 contract: openapi-fetch gives path/params/body typing from the
// generated schema; unwrap() turns every non-2xx response into an ApiError carrying the one
// Error body (doc 03 §1).
import createClient, { type Client } from 'openapi-fetch';
import { apiBaseUrl } from '../base';
import { ApiError } from './errors';
import type { paths } from './schema';

let cached: { url: string; client: Client<paths> } | null = null;

/** The client for the current mount prefix (recreated if the prefix changes, e.g. in tests). */
export function api(): Client<paths> {
  const url = apiBaseUrl();
  if (!cached || cached.url !== url) {
    cached = {
      url,
      client: createClient<paths>({
        baseUrl: url,
        // Resolve fetch at call time so test doubles and MSW interceptors are always honoured.
        fetch: (input: Request) => globalThis.fetch(input),
        headers: { Accept: 'application/json' },
      }),
    };
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
