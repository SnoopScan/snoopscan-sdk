/**
 * JavaScript/TypeScript client for the SnoopScan web scraping API.
 *
 * Deliberately mirrors the shape of the established clients in this space —
 * `scrape`, `crawl`, `map`, `extract`, `search`, plus a blocking
 * `crawlAndWait` — because the migration promise is that existing code
 * changes a base URL and keeps working. Method and option names are the
 * API's names; only the method NAME casing follows JS convention
 * (`crawlStatus`, not `crawl_status` — the Python client does the reverse
 * for the same reason, following ITS language's convention).
 *
 * Written from our own OpenAPI surface, not from any other client's source.
 *
 *   import { SnoopScan } from 'snoopscan';
 *
 *   const snoop = new SnoopScan({ apiKey: 'sk_...' });
 *   const page = await snoop.scrape('https://example.com');
 *   console.log(page.markdown);
 */

import { SnoopScanError } from './errors.js';
import {
  Cost,
  CrawlJob,
  Document,
  Options,
  crawlJobFromPayload,
  isCrawlFinished,
} from './types.js';

// The hosted API, api.snoopscan.com — see sdk/python/snoopscan/client.py's
// DEFAULT_BASE_URL for the full reasoning. A default of localhost would fail
// every hosted user's first call; a self-hoster of this open-core engine sets
// SNOOPSCAN_BASE_URL=http://localhost:8099 instead — one env var, for exactly
// the audience capable of setting it.
// `process.env` is Node/Bun/Deno-only, so this is guarded for the browser
// build, where a base URL must be passed explicitly.
function envBaseUrl(): string | undefined {
  if (typeof process === 'undefined' || !process.env) return undefined;
  return process.env.SNOOPSCAN_BASE_URL || process.env.SNOOP_BASE_URL;
}

export const DEFAULT_BASE_URL = envBaseUrl() || 'https://api.snoopscan.com';
const DEFAULT_TIMEOUT_MS = 120_000;

// `timeout` (ms) in a request body is the ENGINE's own budget — how long it
// may keep trying tiers server-side. It has nothing to do with
// DEFAULT_TIMEOUT_MS, this client's own abort deadline for waiting on THAT
// response. With a single deadline, asking for 120s (or more) makes the two
// race: whichever fires first wins, sometimes surfacing this client's own
// abort instead of the engine's clean JSON error. The Python SDK does the
// same. 10s of margin covers connection setup and response transfer that sit
// outside the engine's own accounting.
const TIMEOUT_MARGIN_MS = 10_000;

function httpTimeoutFor(body: Record<string, unknown> | undefined, clientDefault: number): number {
  const engineTimeout = body?.timeout;
  if (typeof engineTimeout !== 'number') return clientDefault;
  return Math.max(clientDefault, engineTimeout + TIMEOUT_MARGIN_MS);
}

export const DEFAULT_MAX_RETRIES = 2;
const API_KEY_ENV = 'SNOOPSCAN_API_KEY';

// -- retries -----------------------------------------------------------------
// The same rule as every other SnoopScan SDK (Python, Go, PHP, Ruby, Java,
// .NET, Rust, Elixir):
//
//   - 429 is retried on every call: the rate limiter refused before anything
//     ran, so repeating is always safe. A numeric Retry-After is honoured,
//     capped at 60 seconds.
//   - 500, 502, 503 and 504 are retried only on a call that is safe to
//     repeat, and only when the failure is not the engine's verdict: the body
//     was not the API envelope (a gateway answered) or its code is INTERNAL.
//     BLOCKED, TARGET_ERROR, TIMEOUT and the rest are answers; retrying them
//     repeats the same work for the same answer.
//   - crawl, batchScrape, createMonitor and runMonitor are never retried:
//     each starts a job or creates something, and a gateway error does not
//     say whether the first attempt already did.
//
// Nothing else is retried: other 4xx, network errors, this client's timeouts.
const RETRY_AFTER_CAP_MS = 60_000;
const BACKOFF_BASE_MS = 500;
const BACKOFF_CAP_MS = 8_000;
const BACKOFF_JITTER = 0.25;
const RETRYABLE_SERVER_STATUSES = new Set([500, 502, 503, 504]);

// A screen of 500 names on one registry takes about two minutes at the
// registry's own pace, past the default deadline; a screen gets this at least.
const SCREEN_TIMEOUT_MS = 300_000;

/** Test seams: replaced in tests so a retry does not really wait. Not exported from the package. */
export const internals = {
  sleep: (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms)),
  random: (): number => Math.random(),
};

/** 500ms doubling per attempt, capped at 8s, plus up to 25% jitter. */
export function backoffMs(attempt: number): number {
  const delay = Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** attempt);
  return delay + delay * BACKOFF_JITTER * internals.random();
}

function retryWaitMs(error: SnoopScanError, attempt: number, safe: boolean): number | null {
  if (error.status === 429) {
    if (error.retryAfter !== undefined) return Math.min(error.retryAfter * 1000, RETRY_AFTER_CAP_MS);
    return backoffMs(attempt);
  }
  // A non-envelope body is reported as INTERNAL too, so this one test covers
  // "a gateway answered" and "the engine said INTERNAL".
  if (
    safe &&
    error.status !== undefined &&
    RETRYABLE_SERVER_STATUSES.has(error.status) &&
    error.code === 'INTERNAL'
  ) {
    return backoffMs(attempt);
  }
  return null;
}

function retryAfterSeconds(response: Response): number | undefined {
  const value = response.headers.get('retry-after')?.trim();
  if (!value) return undefined;
  const seconds = Number(value);
  return Number.isFinite(seconds) ? Math.max(0, seconds) : undefined;
}

function envApiKey(): string | undefined {
  if (typeof process === 'undefined' || !process.env) return undefined;
  return process.env[API_KEY_ENV] || undefined;
}

export interface SnoopScanOptions {
  /** Defaults to the SNOOPSCAN_API_KEY environment variable; a key passed here wins. */
  apiKey?: string;
  baseUrl?: string;
  /** Milliseconds. */
  timeout?: number;
  /** Retries after the first attempt, under the retry rule. 0 turns them off. Default 2. */
  maxRetries?: number;
  /** Bring your own fetch — Node <18, a proxying fetch, a test double. */
  fetch?: typeof fetch;
}

interface RequestSpec {
  body?: Record<string, unknown>;
  params?: Record<string, string | number>;
  form?: FormData;
  /** Safe to repeat after a 5xx: false for the calls that start or create something. */
  safe?: boolean;
  /** This client's deadline for this call, when it differs from the default. */
  timeoutMs?: number;
}

export class SnoopScan {
  private baseUrl: string;
  private apiKey: string;
  private timeoutMs: number;
  private maxRetries: number;
  private fetchImpl: typeof fetch;

  constructor(options: SnoopScanOptions = {}) {
    const apiKey = options.apiKey || envApiKey();
    if (!apiKey) {
      throw new Error(
        `No API key: pass apiKey or set ${API_KEY_ENV}. Create one at https://snoopscan.com/app/keys`,
      );
    }
    const maxRetries = options.maxRetries ?? DEFAULT_MAX_RETRIES;
    if (!Number.isInteger(maxRetries) || maxRetries < 0) {
      throw new Error('maxRetries must be a whole number, 0 or more (0 turns retries off)');
    }
    this.apiKey = apiKey;
    this.maxRetries = maxRetries;
    this.baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/+$/, '');
    this.timeoutMs = options.timeout ?? DEFAULT_TIMEOUT_MS;
    this.fetchImpl = options.fetch ?? globalThis.fetch;
    if (!this.fetchImpl) {
      throw new Error(
        'No fetch implementation available. Pass one via `fetch` in the constructor options ' +
          '(e.g. on Node < 18, or a custom test double).',
      );
    }
  }

  // -- plumbing -----------------------------------------------------------

  /** One call, retried when the retry rule allows, then unwrapped. */
  private async request(
    method: 'GET' | 'POST' | 'DELETE',
    path: string,
    spec: RequestSpec = {},
  ): Promise<unknown> {
    const safe = spec.safe ?? true;
    for (let attempt = 0; ; attempt++) {
      try {
        return await SnoopScan.unwrap(await this.send(method, path, spec));
      } catch (error) {
        if (!(error instanceof SnoopScanError) || attempt >= this.maxRetries) throw error;
        const wait = retryWaitMs(error, attempt, safe);
        if (wait === null) throw error;
        await internals.sleep(wait);
      }
    }
  }

  /** One HTTP exchange. Network failures and this client's timeout become SnoopScanError. */
  private async send(method: 'GET' | 'POST' | 'DELETE', path: string, spec: RequestSpec): Promise<Response> {
    let url = `${this.baseUrl}${path}`;
    if (spec.params) {
      const qs = new URLSearchParams(
        Object.entries(spec.params).map(([k, v]) => [k, String(v)]),
      ).toString();
      if (qs) url += `?${qs}`;
    }

    const requestTimeoutMs = httpTimeoutFor(spec.body, spec.timeoutMs ?? this.timeoutMs);
    // No Content-Type for a form: fetch writes the multipart boundary.
    const headers: Record<string, string> = { Authorization: `Bearer ${this.apiKey}` };
    let payload: BodyInit | undefined;
    if (spec.form !== undefined) {
      payload = spec.form;
    } else if (spec.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      payload = JSON.stringify(spec.body);
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), requestTimeoutMs);
    try {
      return await this.fetchImpl(url, { method, headers, body: payload, signal: controller.signal });
    } catch (cause) {
      if (cause instanceof Error && cause.name === 'AbortError') {
        throw new SnoopScanError('TIMEOUT', `Request to ${path} timed out after ${requestTimeoutMs}ms`);
      }
      throw new SnoopScanError('NETWORK_ERROR', `Could not reach ${this.baseUrl}: ${(cause as Error).message}`);
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * Never leak a raw fetch/HTTP failure for an API-level failure: callers
   * branch on `SnoopScanError.code`, and an uncaught non-2xx would bypass
   * that entirely. A non-JSON body means something in front of the API
   * answered — a proxy, a load balancer, an nginx error page — so it is
   * reported as INTERNAL with the status.
   */
  static async unwrap(response: Response): Promise<unknown> {
    const extra = { status: response.status, retryAfter: retryAfterSeconds(response) };
    let payload: { success?: boolean; data?: unknown; error?: Record<string, unknown> } | null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    if (payload === null || typeof payload !== 'object' || !('success' in payload)) {
      throw new SnoopScanError(
        'INTERNAL',
        `Non-JSON response from the API (HTTP ${response.status})`,
        { status_code: response.status },
        extra,
      );
    }

    if (!payload.success) {
      const error = payload.error ?? {};
      throw new SnoopScanError(
        (error.code as string) ?? 'INTERNAL',
        (error.message as string) ?? 'Unknown error',
        (error.detail as Record<string, unknown>) ?? null,
        extra,
      );
    }
    return payload.data;
  }

  private post(
    path: string,
    body: Record<string, unknown>,
    options: { safe?: boolean; timeoutMs?: number } = {},
  ): Promise<any> {
    return this.request('POST', path, { body, ...options }) as Promise<any>;
  }

  private get(path: string, params?: Record<string, string | number>): Promise<any> {
    return this.request('GET', path, params ? { params } : {}) as Promise<any>;
  }

  private del(path: string): Promise<any> {
    return this.request('DELETE', path) as Promise<any>;
  }

  // -- pages ----------------------------------------------------------------

  /** Fetch and extract one URL. */
  async scrape(url: string, options: Options = {}): Promise<Document> {
    const data = await this.post('/v1/scrape', { url, ...options });
    return new Document(data);
  }

  /** Start a crawl. Returns immediately with a job id. */
  async crawl(url: string, options: Options = {}): Promise<CrawlJob> {
    const data = await this.post('/v1/crawl', { url, ...options }, { safe: false });
    return crawlJobFromPayload(data);
  }

  async crawlStatus(jobId: string): Promise<CrawlJob> {
    const data = await this.get(`/v1/crawl/${jobId}`);
    return crawlJobFromPayload(data);
  }

  /**
   * One page of results, plus the cursor for the next. Results are
   * paginated rather than inlined because a 10,000-page crawl must not
   * arrive as a single JSON body.
   */
  async crawlPages(
    jobId: string,
    cursor?: string,
    limit = 50,
  ): Promise<{ documents: Document[]; nextCursor: string | null }> {
    const params: Record<string, string | number> = { limit };
    if (cursor) params.cursor = cursor;
    const data = await this.get(`/v1/crawl/${jobId}/pages`, params);
    const documents = ((data.pages as Record<string, unknown>[]) ?? []).map((row) => new Document(row));
    const nextLink = data.next as string | undefined;
    const nextCursor = nextLink ? nextLink.split('cursor=').pop() ?? null : null;
    return { documents, nextCursor };
  }

  async crawlErrors(jobId: string): Promise<Record<string, unknown>[]> {
    const data = await this.get(`/v1/crawl/${jobId}/errors`);
    return data.errors ?? [];
  }

  async cancelCrawl(jobId: string): Promise<CrawlJob> {
    const data = await this.del(`/v1/crawl/${jobId}`);
    return crawlJobFromPayload(data);
  }

  /**
   * Start a crawl and wait until it finishes, then return every page.
   *
   * Convenience only. For anything large, start the crawl and read pages as
   * they arrive rather than holding the whole result in memory.
   */
  async crawlAndWait(
    url: string,
    options: Options & { pollIntervalMs?: number; maxWaitMs?: number } = {},
  ): Promise<Document[]> {
    const { pollIntervalMs = 3_000, maxWaitMs = 900_000, ...crawlOptions } = options;
    const job = await this.crawl(url, crawlOptions);
    const deadline = Date.now() + maxWaitMs;

    let current = job;
    while (Date.now() < deadline) {
      current = await this.crawlStatus(job.id);
      if (isCrawlFinished(current)) break;
      await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));
    }
    if (!isCrawlFinished(current)) {
      throw new SnoopScanError('TIMEOUT', `Crawl ${job.id} did not finish within ${maxWaitMs}ms`);
    }

    const documents: Document[] = [];
    let cursor: string | null | undefined;
    for (;;) {
      const page = await this.crawlPages(job.id, cursor ?? undefined);
      documents.push(...page.documents);
      cursor = page.nextCursor;
      if (!cursor) return documents;
    }
  }

  /** Discover URLs without fetching page bodies. Fast and cheap. */
  async map(url: string, options: Options = {}): Promise<string[]> {
    const data = await this.post('/v1/map', { url, ...options });
    return data.links ?? [];
  }

  /** Every product a Shopify or WooCommerce store publishes, from its own
   * catalogue endpoint. Returns {platform, total, products, pagesFetched, cost}. */
  async products(url: string, options: Options = {}): Promise<Record<string, unknown>> {
    return this.post('/v1/products', { url, ...options });
  }

  /** A site's posts from its API (WordPress, Substack, Squarespace,
   * Discourse) or its RSS/Atom feed. Returns {platform, source, posts, cost}. */
  async posts(url: string, options: Options = {}): Promise<Record<string, unknown>> {
    return this.post('/v1/posts', { url, ...options });
  }

  /** Everything a company's own site says about itself: firmographics
   * (name, phone, address, LinkedIn, headcount, industry) plus, unless
   * `contacts: false`, the emails, social links and contact form it
   * publishes. Returns {domain, company, people, contacts, pagesRead, cost}. */
  async company(url: string, options: Options = {}): Promise<Record<string, unknown>> {
    return this.post('/v1/company', { url, ...options });
  }

  /** A site's brand, read from its own homepage, manifest and stylesheets:
   * name, description, slogan, logo, symbol, icon and banner addresses, brand
   * colors, fonts and social links. Pass `maxAge` (milliseconds, default 30
   * days) to accept a stored profile; `0` reads the site again. Returns
   * {name, description, slogan, domain, logo, symbol, icon, banner, logos,
   * colors, fonts, socials, meta}. */
  async brand(domain: string, options: Options = {}): Promise<Record<string, unknown>> {
    return this.post('/v1/brand', { domain, ...options });
  }

  /** What is known about a domain rather than a page: registration, DNS
   * records, and who links to it in our own crawl graph. Every part is
   * opt-out — pass `registration: false`, `dns: false` or `backlinks:
   * false` to skip one. Returns {domain, registration, dns, backlinks, cost}. */
  async domain(domainName: string, options: Options = {}): Promise<Record<string, unknown>> {
    return this.post('/v1/domain', { domain: domainName, ...options });
  }

  // -- domain reports: hosted API only (a self-hosted engine has /v1/domain, not these) --

  /** A buyer's report on a domain: key findings, concerns, unanswered
   * questions and the evidence behind each. `depth` is `quick`, `buyer`
   * (the API's default) or `deep`; also `snapshots`, `verifyLinks`. */
  async domainReport(
    domainName: string,
    options: Options & { depth?: 'quick' | 'buyer' | 'deep' } = {},
  ): Promise<Record<string, unknown>> {
    return this.post('/v1/domain/report', { domain: domainName, ...options });
  }

  /** Availability for up to 500 names in one call, from each registry's
   * RDAP record and DNS. Option: `dns` (default true). Returns {counts,
   * checked, results, cost}. */
  async domainScreen(domains: string[], options: Options & { dns?: boolean } = {}): Promise<Record<string, unknown>> {
    return this.post(
      '/v1/domain/screen',
      { domains: [...domains], ...options },
      { timeoutMs: Math.max(this.timeoutMs, SCREEN_TIMEOUT_MS) },
    );
  }

  /** Read the archived snapshots a `buyer` report marked pending. `years`
   * limits it to those years (the report's `evidence.history.pending`). */
  async domainSnapshots(
    domainName: string,
    options: Options & { years?: number[]; snapshots?: number } = {},
  ): Promise<Record<string, unknown>> {
    return this.post('/v1/domain/history/snapshots', { domain: domainName, ...options });
  }

  // -- monitors: watch pages for changes on a schedule ---------------------

  /** intervalMinutes (>= 5, default 60), goal, webhook. Returns the monitor. */
  async createMonitor(
    name: string,
    urls: string[] | string,
    options: Options = {},
  ): Promise<Record<string, unknown>> {
    const body: Record<string, unknown> = { name, ...options };
    body[Array.isArray(urls) ? 'urls' : 'url'] = urls;
    return this.post('/v1/monitor', body, { safe: false });
  }

  async monitors(): Promise<Record<string, unknown>[]> {
    const data = await this.get('/v1/monitor');
    return data.monitors ?? [];
  }

  async monitor(monitorId: string): Promise<Record<string, unknown>> {
    return this.get(`/v1/monitor/${monitorId}`);
  }

  async deleteMonitor(monitorId: string): Promise<void> {
    await this.del(`/v1/monitor/${monitorId}`);
  }

  /** A check right now; returns it. */
  async runMonitor(monitorId: string): Promise<Record<string, unknown>> {
    return this.post(`/v1/monitor/${monitorId}/run`, {}, { safe: false });
  }

  async monitorChecks(monitorId: string, limit = 20): Promise<Record<string, unknown>[]> {
    const data = await this.get(`/v1/monitor/${monitorId}/checks`, { limit });
    return data.checks ?? [];
  }

  // -- bulk / structured ----------------------------------------------------

  async batchScrape(urls: string[], options: Options = {}): Promise<CrawlJob> {
    const data = await this.post('/v1/batch/scrape', { urls, ...options }, { safe: false });
    return crawlJobFromPayload(data);
  }

  /**
   * Schema-constrained extraction. Output is validated against the schema
   * before it is returned, so a row with an `error` means the page
   * genuinely lacked the fields — not that the request should be retried.
   */
  async extract(
    urls: string[],
    schema: Record<string, unknown> | null,
    options: Options & { prompt?: string; template?: string } = {},
  ): Promise<Record<string, unknown>[]> {
    const { prompt, template, ...rest } = options;
    if ((schema == null) === (template == null)) {
      throw new Error(
        'give either `schema` or `template`, not both and not neither: a template ' +
          'IS a schema, and silently picking one would return fields you did not ask for',
      );
    }
    const body: Record<string, unknown> = { urls, ...rest };
    if (schema != null) body.schema = schema;
    if (template != null) body.template = template;
    if (prompt) body.prompt = prompt;
    return this.post('/v1/extract', body);
  }

  /** The named templates this deployment offers, with their fields. */
  async templates(): Promise<Record<string, unknown>[]> {
    const answer = await this.get('/v1/templates');
    return answer.templates ?? [];
  }

  async search(query: string, options: Options = {}): Promise<Record<string, unknown>> {
    return this.post('/v1/search', { query, ...options });
  }

  /**
   * Turn a document into markdown: PDF, DOCX, HTML, TXT or Markdown.
   *
   * Exactly one of:
   *   - `content`: the document itself (a string, bytes or a Blob), sent as a
   *     multipart upload; `filename` says what kind it is.
   *   - `url`: a document on the web. The engine fetches it with every tier
   *     it has and reads PDFs, HTML and text, answering in the same shape.
   *
   * `/v1/parse` takes one uploaded file, never a JSON body.
   */
  async parse(input: {
    url?: string;
    content?: string | Uint8Array | ArrayBuffer | Blob;
    filename?: string;
  }): Promise<Record<string, unknown>> {
    const given = [input.url, input.content].filter((v) => v !== undefined && v !== '');
    if (given.length !== 1) {
      throw new SnoopScanError('INVALID_REQUEST', 'parse takes exactly one of url or content');
    }
    if (input.url !== undefined) {
      if ((input.url.toLowerCase().split('?')[0] ?? '').endsWith('.docx')) {
        throw new SnoopScanError(
          'INVALID_REQUEST',
          'A Word document is read from a file, not a URL: download it and pass its content.',
        );
      }
      const data = (await this.post('/v1/scrape', {
        url: input.url,
        formats: ['markdown'],
        parsers: ['pdf'],
      })) as Record<string, unknown>;
      const meta = (data.metadata as Record<string, unknown>) ?? {};
      return {
        markdown: (data.markdown as string) ?? '',
        url: (meta.sourceURL as string) ?? (meta.url as string) ?? input.url,
        kind: 'url',
        metadata: meta,
        cost: data.cost,
      };
    }
    const content = input.content as string | Uint8Array | ArrayBuffer | Blob;
    const name = input.filename ?? (typeof content === 'string' ? 'document.txt' : 'document');
    const blob =
      content instanceof Blob
        ? content
        : new Blob([typeof content === 'string' ? content : (content as BlobPart)]);
    const form = new FormData();
    form.append('file', blob, name);
    return (await this.upload('/v1/parse', form)) as Record<string, unknown>;
  }

  /** A multipart POST. No Content-Type of our own: fetch writes the boundary. */
  private upload(path: string, form: FormData): Promise<unknown> {
    return this.request('POST', path, { form });
  }
}

export type { Cost, CrawlJob, Options };
export { Document, SnoopScanError };
