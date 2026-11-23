# snoopscan

JavaScript/TypeScript client for SnoopScan, the web scraping API for AI agents.

**Using SnoopScan from an AI chat?** You do not need this package. Paste
`Read and follow https://snoopscan.com/agent-onboarding/SKILL.md` into Claude Code,
Codex, Cursor or any agent, and it sets everything up. You only sign in and
press Approve in your browser.

MIT licensed. The server is AGPL-3.0; a client library must not be, or every
application that imports it inherits the copyleft.

This package is for your own code. Add it to your project:

```bash
npm install snoopscan
```

Needs Node 18+. Your key goes in the project's environment as
`SNOOPSCAN_API_KEY`: create one at https://snoopscan.com/app/keys, or ask
your coding agent to "get me a SnoopScan key and put it in .env" (it runs
`npx snoopscan login`, which opens your browser to sign in and saves the key).

```ts
import { SnoopScan } from 'snoopscan';

const snoop = new SnoopScan(); // the key comes from SNOOPSCAN_API_KEY

const page = await snoop.scrape('https://example.com/');
console.log(page.markdown);

for (const link of await snoop.map('https://example.com/', { limit: 100 })) {
  console.log(link);
}

const pages = await snoop.crawlAndWait('https://example.com/', { limit: 50 });
console.log(pages.length, 'pages');
```

Works in Node 18+ and in the browser — the client is built on native `fetch`
with zero runtime dependencies. `require()` and `import` both work; types
ship in the package, no `@types/snoopscan` to install separately.

## Templates

Structured fields without writing a schema. A template names a kind of page —
`product`, `article`, `jobPosting`, `localBusiness`, `event`, `recipe` — and its
fields are the schema.org names sites already publish, so they usually come
straight from the page's own markup, with no model call.

```ts
const rows = await snoop.extract(['https://shop.example/product/123'], null, {
  template: 'product',
});
console.log(rows[0].data); // { name, price, currency, ... }

for (const t of await snoop.templates()) {
  console.log(t.name, t.fields); // what this deployment offers
}
```

Give a `schema` **or** a `template`, never both — a template *is* a schema.
Passing both, or neither, throws before the request is sent.

## Polling a crawl yourself

`crawlAndWait` does this for you. When you want control over the wait:

```ts
let job = await snoop.crawl('https://example.com/', { limit: 500 });
while (!(job = await snoop.crawlStatus(job.id)).finished) {  // completed, failed or cancelled
  await new Promise((r) => setTimeout(r, 5000));
}
```

`isCrawlFinished(job)` is exported too, for a job you only hold the status of.

## Platforms

A site that publishes its data as JSON is asked, not crawled:

```ts
const catalog = await snoop.products('https://store.example.com'); // Shopify, WooCommerce, Squarespace, Magento
for (const p of catalog.products as Record<string, unknown>[]) {
  console.log(p.title, p.price, p.currency, p.available);
}

const posts = await snoop.posts('https://blog.example.com'); // WordPress, Substack, Squarespace, Discourse; else the feed
console.log(posts.source, (posts.posts as unknown[]).length);
```

Every page's `metadata.platform` says what built it, and a `scrape()` of a
Shopify, WooCommerce or Amazon product page carries `product` beside the
markdown. If a store hides its price from a plain request, pass `tier: 'browser'`.

## Company & domain

Two lookups that answer questions a single page can't:

```ts
const lead = await snoop.company('acme.com'); // firmographics + contacts, from the site itself
const company = lead.company as Record<string, unknown>;
console.log(company.name, company.headcount);

const info = await snoop.domain('acme.com'); // registration, DNS, backlinks — not a page fetch
console.log((info.registration as Record<string, unknown>).registrar);
```

`company()` takes `{ contacts: false }` to skip contact discovery and return
only firmographics. `domain()`'s three lookups are each opt-out —
`registration: false`, `dns: false`, `backlinks: false` — since a caller
asking about a domain usually wants all of it, not a form to fill in.

## Brand

A site's name, logos, colors, fonts and social links, read from its own pages:

```ts
const brand = await snoop.brand('stripe.com');
console.log(brand.name, brand.logo, brand.icon);
for (const color of brand.colors as Array<Record<string, unknown>>) {
  console.log(color.hex, color.type, color.source);
}
```

A profile read within 30 days is served from storage; pass `{ maxAge: 0 }`
(milliseconds) to read the site again. It costs 5 credits when the site was read.

## Domain report, screen and snapshots

**Hosted API only.** A self-hosted engine has `/v1/domain` but not these.

```ts
const report = await snoop.domainReport('example.com', { depth: 'buyer' }); // quick, buyer (default) or deep
const screen = await snoop.domainScreen(['example.com', 'example-shop.io'], { dns: true }); // up to 500 names
const more = await snoop.domainSnapshots('example.com', { years: [2014, 2019] });
```

A `buyer` report answers at once and marks archived snapshots it has not read
yet as pending (`evidence.history.pending`); `domainSnapshots` reads them.
`domainReport` also takes `snapshots` and `verifyLinks`. A screen of 500
names takes about two minutes, so it gets a 300-second deadline at least.

## Monitors

```ts
const m = await snoop.createMonitor('Pricing', ['https://example.com/pricing'], {
  intervalMinutes: 60,
  webhook: 'https://hooks.example.com/snoop',
});
const check = await snoop.runMonitor(m.id as string); // a check now: {counts, pages}
await snoop.monitorChecks(m.id as string); // recent checks
await snoop.deleteMonitor(m.id as string);
```

Each page in a check is `same`, `changed` (with a git diff), `new` or
`error`. The webhook `monitor.check.completed` fires only when a check has
something to say.

## Configuration

```ts
const snoop = new SnoopScan({
  apiKey: 'sk_...',                    // default: SNOOPSCAN_API_KEY
  baseUrl: 'http://localhost:8099',    // default: SNOOPSCAN_BASE_URL, else https://api.snoopscan.com
  timeout: 60_000,                     // milliseconds; default 120000
  maxRetries: 2,                       // default 2; 0 turns retries off
  fetch: myFetch,                      // default: the global fetch
});
```

**Key.** With no `apiKey`, the client reads `SNOOPSCAN_API_KEY` from the
environment (Node, Bun and Deno; a browser has none, so pass it). A key you
pass wins. With neither, the constructor throws saying so.

**Base URL.** The hosted API, `https://api.snoopscan.com`, is the default. A
self-hosted engine usually runs at `http://localhost:8099`: set
`SNOOPSCAN_BASE_URL` or pass `baseUrl`. Trailing slashes are stripped.

**Timeout.** The client's own deadline for one request. A request's
`timeout` option is a different clock: the engine's own budget, in
milliseconds. When you pass one, that request's deadline widens to the
engine's budget plus 10 seconds, so the client never gives up first.

**Retries.** A rate-limited request (HTTP 429) is retried on every call,
since the limiter refused before anything ran; a numeric `Retry-After` is
honoured, up to 60 seconds. A 500, 502, 503 or 504 is retried only on a call
that is safe to repeat (every GET and DELETE, and the POSTs that only read),
and only when a gateway answered instead of the API, or the API said
`INTERNAL`. `crawl`, `batchScrape`, `createMonitor` and `runMonitor` are never retried on a 5xx: a repeat could start a
second crawl or a duplicate monitor. Engine answers such as `BLOCKED` or
`TARGET_ERROR` are not retried either, since the same work would give the
same answer. Up to 2 retries by default; waits back off from 0.5 seconds,
doubling to at most 8, with a little jitter. When retries run out you get
the last error. Pass `maxRetries: 0` to turn retries off.

## Errors

Every failure throws `SnoopScanError` carrying the API's machine-readable
code, so callers can branch on what actually happened:

```ts
import { SnoopScanError } from 'snoopscan';

try {
  const page = await snoop.scrape(url);
} catch (err) {
  if (err instanceof SnoopScanError) {
    if (err.isBlocked) {
      // BLOCKED — the target refused us; retrying as-is will not help
    } else if (err.code === 'FETCH_FAILED') {
      // the target could not be reached; retrying may help
    } else if (err.code === 'INVALID_REQUEST') {
      // our request was wrong; fix it, do not retry
    }
  }
}
```

`err.status` is the HTTP status and `err.retryAfter` the response's
`Retry-After` in seconds, when it had them.

## Cost

Every response carries a `cost` object: how the page was fetched, and whether
it came from cache. Failed requests carry no cost.

## CLI

Installing the package also installs a `snoopscan` command — the same verbs
as the API and the Python CLI. It is what an AI agent runs when it works with
SnoopScan from the chat, so you rarely type these yourself:

```bash
snoopscan login                    # browser sign-in; saves the key
snoopscan scrape https://example.com
snoopscan crawl https://example.com --wait --limit 50
snoopscan company acme.com
```

Run `snoopscan --help` for the full command list, or `snoopscan <command>
--help` for one command. `-o`, `--json`, `--pretty` and `-q` work on every
command. One caveat if you also use the Python SDK's
CLI: both ship a binary literally named `snoopscan`, so installing both
globally means whichever comes later on `PATH` wins; they share the same
config file (`~/.config/snoopscan/config.toml`) so at least there's nothing
to reconfigure either way.

## Development

From a checkout of the engine repo:

```bash
cd sdk/js
npm install
npm run build
npm test              # unit tests; set SNOOPSCAN_TEST_API_KEY to also run
                       # the live integration tests against a real key
```
