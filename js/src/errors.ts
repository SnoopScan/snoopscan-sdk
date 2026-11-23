/**
 * An error returned by the API. Carries the machine-readable code so callers
 * can branch on it — BLOCKED and TARGET_ERROR mean different things and
 * deserve different handling.
 */
export class SnoopScanError extends Error {
  code: string;
  detail: Record<string, unknown>;
  /** The HTTP status, when the error came from a response. */
  status?: number;
  /** Seconds from the response's Retry-After header, when it had one. */
  retryAfter?: number;

  constructor(
    code: string,
    message: string,
    detail?: Record<string, unknown> | null,
    extra: { status?: number; retryAfter?: number } = {},
  ) {
    super(`${code}: ${message}`);
    this.name = 'SnoopScanError';
    this.code = code;
    this.detail = detail ?? {};
    if (extra.status !== undefined) this.status = extra.status;
    if (extra.retryAfter !== undefined) this.retryAfter = extra.retryAfter;
  }

  get isBlocked(): boolean {
    return this.code === 'BLOCKED';
  }

  /** The site responded, but not with the page — a genuine 404 or 5xx, not
   * us being blocked. Retrying will not help. */
  get isTargetError(): boolean {
    return this.code === 'TARGET_ERROR';
  }

  get isRateLimited(): boolean {
    return this.code === 'RATE_LIMITED';
  }
}
