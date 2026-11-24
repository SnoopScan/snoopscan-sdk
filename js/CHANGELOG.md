# Changelog

The Python and JavaScript SDKs are released together and carry one version.

## 0.7.0 - 2026-11-24

- `tech`: the technologies one page runs (CMS, shop platform, theme, analytics, CDN,
  firewall, server), read from its HTML, response headers and cookie names. Takes
  `maxAge` in milliseconds (default `0`, a fresh read). Costs 1 credit, plus what a
  proxy or browser costs over a plain page.
- `brand`: a site's name, logos, colors, fonts and social links, read from its own
  homepage. Takes `maxAge` in milliseconds (default 30 days; `0` reads the site
  again). Costs 5 credits when the site was read.

## 0.6.0 - 2026-09-27 (not released on its own; shipped in 0.7.0)

Brings this SDK level with the Go, PHP, Ruby, Java, .NET, Rust and Elixir SDKs.

- Automatic retries, the same rule as every other SnoopScan SDK. A 429 is
  retried on every call, honouring a numeric `Retry-After` up to 60 seconds. A
  500, 502, 503 or 504 is retried only on a call that is safe to repeat, and
  only when a gateway answered or the API said `INTERNAL`. `crawl`,
  `batchScrape`, `createMonitor` and `runMonitor` are never retried on a 5xx,
  and engine answers such as `BLOCKED` or `TARGET_ERROR` are never retried.
  Backoff starts at 0.5 seconds and doubles to at most 8, with jitter.
  Configure with `maxRetries` (default 2; 0 turns them off).
- The API key comes from `SNOOPSCAN_API_KEY` when none is passed, so `new
  SnoopScan()` works on its own. A key passed still wins. With neither, the
  constructor fails with a message saying so. An empty key counts as none.
- Domain tools on the client (hosted API only): `domainReport`, `domainScreen`
  and `domainSnapshots`. A screen takes up to 500 names and gets a 300-second
  deadline at least.
- `SnoopScanError` now carries `status` and `retryAfter`;
  `DEFAULT_MAX_RETRIES` is exported.
- A JSON body that is not the API's envelope is reported as `INTERNAL` with
  its HTTP status, like a non-JSON one.
- README: documents the default base URL, `https://api.snoopscan.com`, puts
  the chat-first setup line under the title, and covers retries, the
  environment key and the domain tools.

## Earlier releases

- 0.5.2 (2026-09-25): the CLI prints the formats asked for and fails loudly on
  an empty answer.
- 0.5.1 (2026-09-23): parse uploads a file, and a URL goes through scrape.
- 0.5.0 (2026-09-22): `doctor`, update notices, `python -m snoopscan`.
- 0.4.0 (2026-09-22): `snoopscan login`.
