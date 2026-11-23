# snoopscan

Python client for SnoopScan, the web scraping API for AI agents.

**Using SnoopScan from an AI chat?** You do not need this package. Paste
`Read and follow https://snoopscan.com/agent-onboarding/SKILL.md` into Claude Code,
Codex, Cursor or any agent, and it sets everything up. You only sign in and
press Approve in your browser.

MIT licensed. The server is AGPL-3.0; a client library must not be, or every
application that imports it inherits the copyleft.

This package is for your own code. Add it to your project:

```bash
pip install snoopscan
```

Needs Python 3.10+. Your key goes in the project's environment as
`SNOOPSCAN_API_KEY`: create one at https://snoopscan.com/app/keys, or ask
your coding agent to "get me a SnoopScan key and put it in .env" (it runs
`snoopscan login`, which opens your browser to sign in and saves the key).

```python
from snoopscan import SnoopScan

snoop = SnoopScan()  # the key comes from SNOOPSCAN_API_KEY

page = snoop.scrape("https://example.com/")
print(page.markdown)

for link in snoop.map("https://example.com/", limit=100):
    print(link["url"])

pages = snoop.crawl_and_wait("https://example.com/", limit=50)
print(len(pages), "pages")
```

## Templates

Structured fields without writing a schema. A template names a kind of page —
`product`, `article`, `jobPosting`, `localBusiness`, `event`, `recipe` — and its
fields are the schema.org names sites already publish, so they usually come
straight from the page's own markup, with no model call.

```python
rows = snoop.extract(["https://shop.example/product/123"], template="product")
print(rows[0]["data"])  # {"name": ..., "price": ..., "currency": ..., ...}

for t in snoop.templates():  # what this deployment offers
    print(t["name"], t["fields"])
```

Give a `schema` **or** a `template`, never both — a template *is* a schema.
Passing both, or neither, raises before the request is sent.

## Polling a crawl yourself

`crawl_and_wait` does this for you. When you want control over the wait:

```python
import time

job = snoop.crawl("https://example.com/", limit=500)
while not (job := snoop.crawl_status(job.id)).finished:  # completed, failed or cancelled
    time.sleep(5)
```

## Platforms

A site that publishes its data as JSON is asked, not crawled:

```python
catalog = snoop.products(
    "https://store.example.com"
)  # Shopify, WooCommerce, Squarespace, Magento
for p in catalog["products"]:
    print(p["title"], p["price"], p["currency"], p["available"])

posts = snoop.posts(
    "https://blog.example.com"
)  # WordPress, Substack, Squarespace, Discourse; else the feed
print(posts["source"], len(posts["posts"]))
```

Every page's `metadata.platform` says what built it, and a `scrape()` of a
Shopify, WooCommerce or Amazon product page carries `product` beside the
markdown. If a store hides its price from a plain request, pass `tier="browser"`.

## Monitors

```python
m = snoop.create_monitor(
    "Pricing",
    ["https://example.com/pricing"],
    intervalMinutes=60,
    webhook="https://hooks.example.com/snoop",
)
check = snoop.run_monitor(m["id"])  # a check now: {"counts": {...}, "pages": [...]}
snoop.monitor_checks(m["id"])  # recent checks
snoop.delete_monitor(m["id"])
```

Each page in a check is `same`, `changed` (with a git diff), `new` or `error`.
The webhook `monitor.check.completed` fires only when a check has something to
say.

## Company & domain

Two lookups that answer questions a single page can't:

```python
lead = snoop.company("acme.com")  # firmographics + contacts, from the site itself
print(lead["company"]["name"], lead["company"]["headcount"])
for email in lead["contacts"]["emails"]:
    print(email["email"], email["role"], email["onDomain"])

info = snoop.domain("acme.com")  # registration, DNS, backlinks — not a page fetch
print(info["registration"]["registrar"], info["dns"]["mx"])
print(info["backlinks"]["referringDomains"], "domains link here, per our own crawl graph")
```

`company()` takes `contacts=False` to skip contact discovery and return only
firmographics. `domain()`'s three lookups are each opt-out —
`registration=False`, `dns=False`, `backlinks=False` — since a caller asking
about a domain usually wants all of it, not a form to fill in.

## Brand

A site's name, logos, colors, fonts and social links, read from its own pages:

```python
brand = snoop.brand("stripe.com")
print(brand["name"], brand["logo"], brand["icon"])
for color in brand["colors"]:
    print(color["hex"], color["type"], color["source"])
```

A profile read within 30 days is served from storage; pass `maxAge=0` (milliseconds)
to read the site again. It costs 5 credits when the site was read.

## Domain report, screen and snapshots

**Hosted API only.** A self-hosted engine has `/v1/domain` but not these.

```python
report = snoop.domain_report("example.com", depth="buyer")  # quick, buyer (default) or deep
screen = snoop.domain_screen(["example.com", "example-shop.io"], dns=True)  # up to 500 names
more = snoop.domain_snapshots("example.com", years=[2014, 2019])
```

A `buyer` report answers at once and marks archived snapshots it has not read
yet as pending (`evidence.history.pending`); `domain_snapshots` reads them.
`domain_report` also takes `snapshots` and `verifyLinks`. A screen of 500
names takes about two minutes, so it gets a 300-second deadline at least.
`AsyncSnoopScan` has the same three methods.

## Configuration

```python
snoop = SnoopScan(
    api_key="sk_...",                     # default: SNOOPSCAN_API_KEY
    base_url="http://localhost:8099",     # default: SNOOPSCAN_BASE_URL, else https://api.snoopscan.com
    timeout=60.0,                         # seconds; default 120
    max_retries=2,                        # default 2; 0 turns retries off
)
```

**Key.** With no `api_key`, the client reads `SNOOPSCAN_API_KEY` from the
environment. A key you pass wins. With neither, the constructor raises
`ValueError` saying so.

**Base URL.** The hosted API, `https://api.snoopscan.com`, is the default. A
self-hosted engine usually runs at `http://localhost:8099`: set
`SNOOPSCAN_BASE_URL` or pass `base_url`. Trailing slashes are stripped.

**Timeout.** The client's own deadline for one request. A request's
`timeout` option is a different clock: the engine's own budget, in
milliseconds. When you pass one, that request's deadline widens to the
engine's budget plus 10 seconds, so the client never gives up first.

**Retries.** A rate-limited request (HTTP 429) is retried on every call,
since the limiter refused before anything ran; a numeric `Retry-After` is
honoured, up to 60 seconds. A 500, 502, 503 or 504 is retried only on a call
that is safe to repeat (every GET and DELETE, and the POSTs that only read),
and only when a gateway answered instead of the API, or the API said
`INTERNAL`. `crawl`, `batch_scrape`, `create_monitor` and `run_monitor` are never retried on a 5xx: a repeat could start a
second crawl or a duplicate monitor. Engine answers such as `BLOCKED` or
`TARGET_ERROR` are not retried either, since the same work would give the
same answer. Up to 2 retries by default; waits back off from 0.5 seconds,
doubling to at most 8, with a little jitter. When retries run out you get
the last error. Set `max_retries=0` to turn retries off.

## Errors

Every failure raises `SnoopScanError` carrying the API's machine-readable
code, so callers can branch on what actually happened:

```python
from snoopscan import SnoopScanError

try:
    page = snoop.scrape(url)
except SnoopScanError as exc:
    if exc.is_blocked:
        ...  # BLOCKED — the target refused us; retrying as-is will not help
    elif exc.code == "FETCH_FAILED":
        ...  # the target could not be reached; retrying may help
    elif exc.code == "INVALID_REQUEST":
        ...  # our request was wrong; fix it, do not retry
```

`exc.status_code` is the HTTP status and `exc.retry_after` the response's
`Retry-After` in seconds, when it had them.

## Cost

Every response carries a `cost` object: how the page was fetched, and whether
it came from cache. Failed requests carry no cost.

## Development

From a checkout of the engine repo:

```bash
uv pip install -e sdk/python --python .venv/bin/python
```
