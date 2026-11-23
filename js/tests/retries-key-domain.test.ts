/**
 * Retries, the key from the environment, and the domain report tools.
 *
 * The same rule as the Python, Go, PHP, Ruby, Java, .NET, Rust and Elixir
 * SDKs. No network: every request goes to a scripted fetch, and the sleeps
 * are recorded rather than slept.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DEFAULT_MAX_RETRIES, SnoopScan, SnoopScanError } from '../src/index.js';
import { backoffMs, internals } from '../src/client.js';

const BASE = 'https://api.example.test';

function ok(data: unknown = {}): Response {
  return new Response(JSON.stringify({ success: true, data }), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

function fail(status: number, code: string, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify({ success: false, error: { code, message: code.toLowerCase() } }), {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  });
}

function gateway(status: number): Response {
  return new Response('<html>bad gateway</html>', { status, headers: { 'content-type': 'text/html' } });
}

interface Sent {
  url: string;
  init: RequestInit;
}

function script(...responses: Response[]): { fetch: typeof fetch; sent: Sent[]; bodies: () => unknown[] } {
  const sent: Sent[] = [];
  const queue = [...responses];
  const fetchImpl = (async (url: string, init: RequestInit) => {
    sent.push({ url, init });
    const next = queue.shift();
    if (!next) throw new Error('no scripted response left');
    return next;
  }) as unknown as typeof fetch;
  return { fetch: fetchImpl, sent, bodies: () => sent.map((s) => JSON.parse(s.init.body as string)) };
}

let sleeps: number[];
const realSleep = internals.sleep;

beforeEach(() => {
  sleeps = [];
  internals.sleep = async (ms: number) => {
    sleeps.push(ms);
  };
});

afterEach(() => {
  internals.sleep = realSleep;
  vi.unstubAllEnvs();
});

function client(fetchImpl: typeof fetch, extra: { maxRetries?: number; timeout?: number } = {}): SnoopScan {
  return new SnoopScan({ apiKey: 'sk_test', baseUrl: BASE, fetch: fetchImpl, ...extra });
}

describe('retries', () => {
  it('retries a 429 after its Retry-After', async () => {
    const s = script(fail(429, 'RATE_LIMITED', { 'Retry-After': '3' }), ok({ markdown: 'hi' }));
    expect((await client(s.fetch).scrape('https://a.test/')).markdown).toBe('hi');
    expect(s.sent).toHaveLength(2);
    expect(sleeps).toEqual([3000]);
  });

  it('caps a Retry-After at 60 seconds', async () => {
    const s = script(fail(429, 'RATE_LIMITED', { 'Retry-After': '600' }), ok());
    await client(s.fetch).search('q');
    expect(sleeps).toEqual([60_000]);
  });

  it('retries a 429 even on a call that starts a job: nothing ran', async () => {
    const s = script(fail(429, 'RATE_LIMITED'), ok({ id: 'job_1', status: 'scraping' }));
    expect((await client(s.fetch).crawl('https://a.test/')).id).toBe('job_1');
    expect(s.sent).toHaveLength(2);
    expect(sleeps[0]).toBeGreaterThanOrEqual(500);
    expect(sleeps[0]).toBeLessThanOrEqual(625);
  });

  it.each([500, 502, 503, 504])('retries a gateway %i on a safe call', async (status) => {
    const s = script(gateway(status), ok({ markdown: 'hi' }));
    expect((await client(s.fetch).scrape('https://a.test/')).markdown).toBe('hi');
    expect(s.sent).toHaveLength(2);
  });

  it('retries the engine saying INTERNAL', async () => {
    const s = script(fail(500, 'INTERNAL'), ok({ links: [] }));
    expect(await client(s.fetch).map('https://a.test/')).toEqual([]);
    expect(s.sent).toHaveLength(2);
  });

  it.each(['BLOCKED', 'TARGET_ERROR', 'TIMEOUT', 'FETCH_FAILED'])('never retries the engine verdict %s', async (code) => {
    const s = script(fail(502, code), ok());
    await expect(client(s.fetch).scrape('https://a.test/')).rejects.toMatchObject({ code, status: 502 });
    expect(s.sent).toHaveLength(1);
    expect(sleeps).toEqual([]);
  });

  const unsafe: [string, (c: SnoopScan) => Promise<unknown>][] = [
    ['crawl', (c) => c.crawl('https://a.test/')],
    ['batchScrape', (c) => c.batchScrape(['https://a.test/'])],
    ['createMonitor', (c) => c.createMonitor('m', ['https://a.test/'])],
    ['runMonitor', (c) => c.runMonitor('mon_1')],
  ];
  it.each(unsafe)('never retries %s on a 5xx', async (_name, call) => {
    const s = script(gateway(502), ok({ id: 'x', status: 'scraping' }));
    await expect(call(client(s.fetch))).rejects.toMatchObject({ code: 'INTERNAL' });
    expect(s.sent).toHaveLength(1);
  });

  it('does not retry other 4xx', async () => {
    const s = script(fail(400, 'INVALID_REQUEST'), ok());
    await expect(client(s.fetch).scrape('https://a.test/')).rejects.toBeInstanceOf(SnoopScanError);
    expect(s.sent).toHaveLength(1);
  });

  it('does not retry a network failure', async () => {
    let calls = 0;
    const failing = (async () => {
      calls++;
      throw new TypeError('fetch failed');
    }) as unknown as typeof fetch;
    await expect(client(failing).scrape('https://a.test/')).rejects.toMatchObject({ code: 'NETWORK_ERROR' });
    expect(calls).toBe(1);
  });

  it('stops after two retries, with a doubling backoff', async () => {
    const s = script(gateway(503), gateway(503), gateway(503), ok());
    await expect(client(s.fetch).scrape('https://a.test/')).rejects.toMatchObject({ status: 503 });
    expect(s.sent).toHaveLength(3);
    expect(DEFAULT_MAX_RETRIES).toBe(2);
    expect(sleeps[0]).toBeGreaterThanOrEqual(500);
    expect(sleeps[0]).toBeLessThanOrEqual(625);
    expect(sleeps[1]).toBeGreaterThanOrEqual(1000);
    expect(sleeps[1]).toBeLessThanOrEqual(1250);
  });

  it('caps the backoff at eight seconds, jitter on top', () => {
    for (let attempt = 0; attempt < 10; attempt++) expect(backoffMs(attempt)).toBeLessThanOrEqual(10_000);
    expect(backoffMs(9)).toBeGreaterThanOrEqual(8_000);
  });

  it('takes maxRetries', async () => {
    const s = script(gateway(503), gateway(503), gateway(503), gateway(503), ok({ markdown: 'x' }));
    expect((await client(s.fetch, { maxRetries: 4 }).scrape('https://a.test/')).markdown).toBe('x');
    expect(s.sent).toHaveLength(5);
  });

  it('turns retries off with maxRetries: 0', async () => {
    const s = script(fail(429, 'RATE_LIMITED', { 'Retry-After': '1' }), ok());
    await expect(client(s.fetch, { maxRetries: 0 }).scrape('https://a.test/')).rejects.toMatchObject({
      retryAfter: 1,
      status: 429,
    });
    expect(s.sent).toHaveLength(1);
    expect(sleeps).toEqual([]);
  });

  it('refuses a negative maxRetries', () => {
    expect(() => new SnoopScan({ apiKey: 'k', maxRetries: -1 })).toThrow(/maxRetries/);
  });

  it('retries GETs, DELETEs and uploads, re-sending the file', async () => {
    const s = script(gateway(502), ok({ monitors: [] }), gateway(502), ok({}), gateway(502), ok({ markdown: 'doc' }));
    const c = client(s.fetch);
    expect(await c.monitors()).toEqual([]);
    await c.deleteMonitor('mon_1');
    expect((await c.parse({ content: 'hello', filename: 'a.txt' })).markdown).toBe('doc');
    expect(s.sent).toHaveLength(6);
    const file = (s.sent[5]!.init.body as FormData).get('file') as File;
    expect(await file.text()).toBe('hello');
  });
});

describe('the API key', () => {
  const header = (sent: Sent[]) => (sent[0]!.init.headers as Record<string, string>).Authorization;

  it('comes from SNOOPSCAN_API_KEY when none is passed', async () => {
    vi.stubEnv('SNOOPSCAN_API_KEY', 'sk_env');
    const s = script(ok());
    await new SnoopScan({ baseUrl: BASE, fetch: s.fetch }).search('q');
    expect(header(s.sent)).toBe('Bearer sk_env');
  });

  it('works with no options at all', () => {
    vi.stubEnv('SNOOPSCAN_API_KEY', 'sk_env');
    expect(() => new SnoopScan()).not.toThrow();
  });

  it('a key passed wins over the environment', async () => {
    vi.stubEnv('SNOOPSCAN_API_KEY', 'sk_env');
    const s = script(ok());
    await new SnoopScan({ apiKey: 'sk_mine', baseUrl: BASE, fetch: s.fetch }).search('q');
    expect(header(s.sent)).toBe('Bearer sk_mine');
  });

  it('no key anywhere is a clear error', () => {
    vi.stubEnv('SNOOPSCAN_API_KEY', '');
    expect(() => new SnoopScan()).toThrow(/SNOOPSCAN_API_KEY/);
  });
});

describe('domain tools', () => {
  it('domainReport sends the domain and depth', async () => {
    const s = script(ok({ domain: 'example.com' }), ok({}));
    const c = client(s.fetch);
    expect((await c.domainReport('example.com', { depth: 'quick' })).domain).toBe('example.com');
    await c.domainReport('example.com', { snapshots: 10, verifyLinks: 5 });
    expect(s.sent[0]!.url).toBe(`${BASE}/v1/domain/report`);
    expect(s.bodies()).toEqual([
      { domain: 'example.com', depth: 'quick' },
      { domain: 'example.com', snapshots: 10, verifyLinks: 5 },
    ]);
  });

  it('domainScreen sends up to 500 names and outlasts a short client deadline', async () => {
    // A 500-name screen takes about two minutes; the call gets at least 300s
    // whatever the client default, so a 20ms default must not abort an 80ms answer.
    const names = Array.from({ length: 500 }, (_, i) => `name${i}.com`);
    const sent: Sent[] = [];
    const slow = (async (url: string, init: RequestInit) => {
      sent.push({ url, init });
      await new Promise((resolve) => setTimeout(resolve, 80));
      if (init.signal?.aborted) throw Object.assign(new Error('aborted'), { name: 'AbortError' });
      return ok({ counts: {}, checked: 500, results: [] });
    }) as unknown as typeof fetch;
    const out = await client(slow, { timeout: 20 }).domainScreen(names, { dns: false });
    expect(out.checked).toBe(500);
    expect(sent[0]!.url).toBe(`${BASE}/v1/domain/screen`);
    expect(JSON.parse(sent[0]!.init.body as string)).toEqual({ domains: names, dns: false });
  });

  it('domainSnapshots sends the years', async () => {
    const s = script(ok({ snapshots: [] }), ok({}));
    const c = client(s.fetch);
    await c.domainSnapshots('example.com', { years: [2014, 2019], snapshots: 6 });
    await c.domainSnapshots('example.com');
    expect(s.sent[0]!.url).toBe(`${BASE}/v1/domain/history/snapshots`);
    expect(s.bodies()).toEqual([
      { domain: 'example.com', years: [2014, 2019], snapshots: 6 },
      { domain: 'example.com' },
    ]);
  });

  it('the domain tools are safe to retry', async () => {
    const s = script(gateway(502), ok(), gateway(503), ok(), gateway(504), ok());
    const c = client(s.fetch);
    await c.domainReport('a.com');
    await c.domainScreen(['a.com']);
    await c.domainSnapshots('a.com');
    expect(s.sent).toHaveLength(6);
  });
});

describe('brand', () => {
  it('sends the domain and the max age', async () => {
    const s = script(ok({ name: 'Stripe' }), ok({}));
    const c = client(s.fetch);
    expect((await c.brand('stripe.com')).name).toBe('Stripe');
    await c.brand('stripe.com', { maxAge: 0 });
    expect(s.sent[0]!.url).toBe(`${BASE}/v1/brand`);
    expect(s.bodies()).toEqual([{ domain: 'stripe.com' }, { domain: 'stripe.com', maxAge: 0 }]);
  });
});
