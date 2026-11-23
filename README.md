<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.png">
    <img src="assets/logo-light.png" alt="SnoopScan" width="260">
  </picture>
</p>

<h1 align="center">SnoopScan SDK for Python and JavaScript</h1>

<p align="center">
  Scrape, crawl, map, search and extract the web from your own code. Clean markdown or schema-validated JSON, with retries, typed errors and the cost of every call.
</p>

<p align="center">
  <a href="https://pypi.org/project/snoopscan/"><img src="https://img.shields.io/pypi/v/snoopscan?label=PyPI" alt="PyPI"></a>
  <a href="https://www.npmjs.com/package/snoopscan"><img src="https://img.shields.io/npm/v/snoopscan?label=npm" alt="npm"></a>
  <a href="https://github.com/SnoopScan/snoopscan-sdk/actions/workflows/ci.yml"><img src="https://github.com/SnoopScan/snoopscan-sdk/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT license"></a>
</p>

[SnoopScan](https://snoopscan.com) is a web scraping API for AI agents and the apps behind them. This repository holds the two official client libraries. Both have the same methods, the same error codes and the same retry rules.

| | Install | Needs | Docs |
| --- | --- | --- | --- |
| **Python** | `pip install snoopscan` | Python 3.10+ | [python/README.md](python/README.md) |
| **JavaScript / TypeScript** | `npm install snoopscan` | Node 18+ or a browser | [js/README.md](js/README.md) |

Using SnoopScan from an AI chat rather than from code? You don't need an SDK. Use the [MCP server](https://github.com/SnoopScan/snoopscan-mcp).

## Python

```python
from snoopscan import SnoopScan

snoop = SnoopScan()  # reads SNOOPSCAN_API_KEY

page = snoop.scrape("https://example.com/")
print(page.markdown)

rows = snoop.extract(["https://shop.example/product/123"], template="product")
print(rows[0]["data"])  # name, price, currency, availability

for link in snoop.map("https://docs.example.com/", limit=100):
    print(link["url"])

pages = snoop.crawl_and_wait("https://docs.example.com/", limit=50)
print(len(pages), "pages")
```

An async client, `AsyncSnoopScan`, is included.

## JavaScript and TypeScript

```ts
import { SnoopScan } from 'snoopscan';

const snoop = new SnoopScan(); // reads SNOOPSCAN_API_KEY

const page = await snoop.scrape('https://example.com/');
console.log(page.markdown);

const pages = await snoop.crawlAndWait('https://docs.example.com/', { limit: 50 });
console.log(pages.length, 'pages');
```

No runtime dependencies. It works with `import` and `require`, and the types ship in the package.

## What you can call

- `scrape`, `parse`: one page or document as markdown, HTML or links
- `map`, `crawl`: a site's URLs, or a background crawl you poll or wait on
- `search`: web search, with each result's page if you ask
- `extract`: fields to your JSON schema, or to a template such as `product` or `article`
- `products`, `posts`: a store's catalog or a blog's posts from the platform's own feed
- `create_monitor` / `run_monitor`: watch pages and get a webhook when they change
- `company`, `domain`, `brand`: what a site says about itself, and a domain's age, owner and history

Each reply carries a `cost` object, and failed pages aren't billed. A free account gets credits every month with no card. Get a key at [snoopscan.com/app/keys](https://snoopscan.com/app/keys) and set it as `SNOOPSCAN_API_KEY`.

## Other languages

Go, PHP, Ruby, Java, .NET, Rust and Elixir clients live in [SnoopScan/snoopscan-core](https://github.com/SnoopScan/snoopscan-core/tree/main/sdk). Or call the [REST API](https://snoopscan.com/docs) directly.

## Development

```bash
# Python
cd python && pip install -e . pytest pytest-httpx && pytest

# JavaScript
cd js && npm ci && npm test && npm run build
```

## License

MIT. The SnoopScan engine is AGPL-3.0, and these libraries are MIT on purpose, so importing one never puts copyleft on your application.
