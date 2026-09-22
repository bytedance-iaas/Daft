import { zh } from '../locales/zh';
import type { ErrorCode } from './types';

/** Client-side codes for failures that never reached the Daemon's error body. */
export type ClientErrorCode = 'network' | 'unexpected_response';

/**
 * Every failed call surfaces as an ApiError. `message` is what people see: the Daemon's Chinese
 * `error.message` as is (doc 03 §1), or a Chinese fallback when there was no error body.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: ErrorCode | ClientErrorCode;
  readonly details: Record<string, unknown> | undefined;

  constructor(status: number, code: ErrorCode | ClientErrorCode, message: string, details?: Record<string, unknown>) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
  }

  static network(cause: unknown): ApiError {
    const err = new ApiError(0, 'network', zh.errors.network);
    (err as { cause?: unknown }).cause = cause;
    return err;
  }

  /** Builds an ApiError from a non-2xx response and whatever body it carried. */
  static fromResponse(status: number, body: unknown): ApiError {
    const e = (body as { error?: { code?: unknown; message?: unknown; details?: unknown } } | undefined)?.error;
    if (e && typeof e.code === 'string' && typeof e.message === 'string') {
      const details = e.details && typeof e.details === 'object' ? (e.details as Record<string, unknown>) : undefined;
      return new ApiError(status, e.code as ErrorCode, e.message, details);
    }
    return new ApiError(status, 'unexpected_response', zh.errors.unexpected(status));
  }
}

export function isApiError(err: unknown, code?: ApiError['code']): err is ApiError {
  return err instanceof ApiError && (code === undefined || err.code === code);
}

/** The text to show for any thrown value. */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error && err.message) return err.message;
  return zh.errors.unknown;
}
