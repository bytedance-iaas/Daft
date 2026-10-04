// The plumbing every mock handler shares: the API prefix, error bodies, request-body validation,
// Idempotency-Key replay and the two paginations (page numbers, opaque cursors).
import { HttpResponse } from 'msw';
import { db } from './db';

type Validator = (operationId: string, body: unknown) => void;
let requestValidator: Validator | null = null;

/** Tests install a validator that checks request bodies against the contract. */
export function setRequestValidator(v: Validator | null): void {
  requestValidator = v;
}

export const API = '*/api/v1';

export function err(status: number, code: string, message: string, details?: Record<string, unknown>) {
  return HttpResponse.json({ error: details ? { code, message, details } : { code, message } }, { status });
}

export async function body<T>(request: Request, operationId: string): Promise<T> {
  const text = await request.text();
  const parsed = text ? (JSON.parse(text) as T) : ({} as T);
  requestValidator?.(operationId, parsed);
  return parsed;
}

/** Idempotency-Key (doc 03 §8): the same key returns the first response. */
export async function idempotent(request: Request, run: () => Promise<Response> | Response): Promise<Response> {
  const key = request.headers.get('Idempotency-Key');
  if (key && db.idempotency.has(key)) {
    const first = db.idempotency.get(key)!;
    return HttpResponse.json(first.body as never, { status: first.status });
  }
  const res = await run();
  if (key) {
    const clone = res.clone();
    const text = await clone.text();
    db.idempotency.set(key, { status: res.status, body: text ? JSON.parse(text) : null });
  }
  return res;
}

export function page<T>(items: T[], url: URL): { items: T[]; page: number; page_size: number; total: number } {
  const size = Number(url.searchParams.get('page_size') ?? 20);
  const pageNo = Math.max(1, Number(url.searchParams.get('page') ?? 1));
  const start = (pageNo - 1) * size;
  return { items: items.slice(start, start + size), page: pageNo, page_size: size, total: items.length };
}

export function encodeCursor(obj: unknown): string {
  return btoa(unescape(encodeURIComponent(JSON.stringify(obj))));
}

export function decodeCursor<T>(c: string | null): T | null {
  if (!c) return null;
  try {
    return JSON.parse(decodeURIComponent(escape(atob(c)))) as T;
  } catch {
    return null;
  }
}

export function cursorPage<T>(items: T[], url: URL, defLimit = 50): { items: T[]; next_cursor: string | null; has_more: boolean } {
  const limit = Math.min(Number(url.searchParams.get('limit') ?? defLimit), 500);
  const offset = decodeCursor<{ o: number }>(url.searchParams.get('cursor'))?.o ?? 0;
  const slice = items.slice(offset, offset + limit);
  const more = offset + limit < items.length;
  return { items: slice, next_cursor: more ? encodeCursor({ o: offset + limit }) : null, has_more: more };
}
