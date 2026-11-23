"""Python client for the SnoopScan web scraping API.

Deliberately mirrors the shape of the established clients in this space —
`scrape`, `crawl`, `map`, `extract`, `search`, plus a blocking `crawl_and_wait`
— because the migration promise is that existing code changes a base URL and
keeps working. Method and option names are the API's names.

Written from our own OpenAPI surface, not from any other client's source.

    from snoopscan import SnoopScan

    snoop = SnoopScan(api_key="sk_...")
    page = snoop.scrape("https://example.com")
    print(page.markdown)
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

# The hosted API, api.snoopscan.com — what most `pip install snoopscan` users
# run against. A localhost default would suit someone running this open-core
# engine themselves, but that is a small, technically sophisticated minority,
# and everyone else's very first call would fail with a connection error. A
# self-hoster sets SNOOPSCAN_BASE_URL=http://localhost:8099 — one env var,
# and precisely the audience capable of setting it.
#
# `SNOOP_BASE_URL` is the pre-rename spelling, still honoured so an existing
# deployment does not break. The CLI reads `SNOOPSCAN_BASE_URL`, and the two
# must agree on the default.
DEFAULT_BASE_URL = (
    os.environ.get("SNOOPSCAN_BASE_URL")
    or os.environ.get("SNOOP_BASE_URL")
    or "https://api.snoopscan.com"
)
DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_RETRIES = 2
API_KEY_ENV = "SNOOPSCAN_API_KEY"

# -- retries ----------------------------------------------------------------
# The same rule as every other SnoopScan SDK (Go, PHP, Ruby, Java, .NET,
# Rust, Elixir):
#
#   * 429 is retried on every call: the rate limiter refused before anything
#     ran, so repeating is always safe. A numeric Retry-After is honoured,
#     capped at 60 seconds.
#   * 500, 502, 503 and 504 are retried only on a call that is safe to
#     repeat, and only when the failure is not the engine's verdict: the body
#     was not the API envelope (a gateway answered) or its code is INTERNAL.
#     BLOCKED, TARGET_ERROR, TIMEOUT and the rest are answers; retrying them
#     repeats the same work for the same answer.
#   * crawl, batch_scrape, create_monitor and run_monitor are never retried:
#     each starts a job or creates something, and a gateway error does not
#     say whether the first attempt already did.
#
# Nothing else is retried: other 4xx, transport errors, client timeouts.
_RETRY_AFTER_CAP_S = 60.0
_BACKOFF_BASE_S = 0.5
_BACKOFF_CAP_S = 8.0
_BACKOFF_JITTER = 0.25
_RETRYABLE_SERVER_STATUSES = frozenset({500, 502, 503, 504})

# A screen of 500 names on one registry takes about two minutes at the
# registry's own pace, which is past the default deadline; a screen gets
# this much at least.
_SCREEN_TIMEOUT_S = 300.0

# Module-level so tests can replace them without touching the real clock.
_sleep = time.sleep
_async_sleep = asyncio.sleep


def _resolve_api_key(api_key: str | None) -> str:
    """The key passed wins; otherwise SNOOPSCAN_API_KEY from the environment."""
    key = api_key or os.environ.get(API_KEY_ENV)
    if not key:
        raise ValueError(
            f"No API key: pass api_key or set {API_KEY_ENV}. "
            "Create one at https://snoopscan.com/app/keys"
        )
    return key


def _check_max_retries(max_retries: int) -> int:
    if max_retries < 0:
        raise ValueError("max_retries must be 0 or more (0 turns retries off)")
    return max_retries


def _backoff(attempt: int) -> float:
    """0.5s doubling per attempt, capped at 8s, plus up to 25% jitter."""
    delay = min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * (2.0**attempt))
    return delay + delay * _BACKOFF_JITTER * random.random()  # noqa: S311 - jitter, not crypto


def _retry_after(response: httpx.Response) -> float | None:
    """A numeric Retry-After header, in seconds; None when there is none."""
    value = (response.headers.get("retry-after") or "").strip()
    try:
        seconds = float(value)
    except ValueError:
        return None
    return max(0.0, seconds) if seconds == seconds else None  # NaN is no header


def _retry_wait(error: SnoopScanError, attempt: int, safe: bool) -> float | None:
    """How long to wait before retrying this failure, or None not to retry."""
    if error.status_code == 429:
        if error.retry_after is not None:
            return min(error.retry_after, _RETRY_AFTER_CAP_S)
        return _backoff(attempt)
    if safe and error.status_code in _RETRYABLE_SERVER_STATUSES and error.code == "INTERNAL":
        # A non-envelope body is reported as INTERNAL too, so this one test
        # covers "a gateway answered" and "the engine said INTERNAL".
        return _backoff(attempt)
    return None


# `timeout` (milliseconds) in a request body is the ENGINE's own budget —
# how long it may keep trying tiers server-side. It has nothing to do with
# DEFAULT_TIMEOUT, the SDK's own HTTP client deadline for waiting on THAT
# response — two clocks that happened to agree by coincidence whenever a
# caller asked for exactly 120s. Ask for 120s (or more) with a fixed client
# deadline and the two race: whichever fires first wins, and often it is
# httpx's own ReadTimeout instead of the engine's clean JSON error,
# indistinguishable from a real network failure. So the client deadline is
# widened to the engine's budget plus a margin.
# 10s of margin covers connection setup and response transfer that sit
# outside the engine's own accounting.
_TIMEOUT_MARGIN_S = 10.0


def _http_timeout_for(body: dict[str, Any], client_default: float) -> float:
    engine_timeout_ms = body.get("timeout")
    if not isinstance(engine_timeout_ms, (int, float)):
        return client_default
    return max(client_default, engine_timeout_ms / 1000 + _TIMEOUT_MARGIN_S)


class SnoopScanError(Exception):
    """An error returned by the API.

    Carries the machine-readable code so callers can branch on it —
    `BLOCKED` and `TARGET_ERROR` mean different things and deserve different
    handling.
    """

    def __init__(
        self,
        code: str,
        message: str,
        detail: dict[str, Any] | None = None,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail or {}
        # The HTTP status, and the response's Retry-After in seconds, when
        # the error came from a response that had them.
        self.status_code = status_code
        self.retry_after = retry_after

    @property
    def is_blocked(self) -> bool:
        return self.code == "BLOCKED"

    @property
    def is_target_error(self) -> bool:
        """The site responded, but not with the page — a genuine 404 or 5xx,
        not us being blocked. Retrying will not help."""
        return self.code == "TARGET_ERROR"

    @property
    def is_rate_limited(self) -> bool:
        return self.code == "RATE_LIMITED"


@dataclass
class Cost:
    """What the request actually cost. Failed requests carry no cost."""

    tier: str | None = None
    tiers_attempted: list[str] = field(default_factory=list)
    proxy_used: bool = False
    proxy_type: str | None = None
    proxy_bytes: int = 0
    browser_ms: int = 0
    extraction_path: str | None = None
    cached: bool = False


@dataclass
class Document:
    markdown: str | None = None
    html: str | None = None
    raw_html: str | None = None
    links: list[str] = field(default_factory=list)
    json_: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    cost: Cost = field(default_factory=Cost)
    # The payload as it arrived. Kept because the typed fields are a curated
    # view: anything the API adds later, and anything a caller wants to dump
    # verbatim, is otherwise lost the moment it is parsed.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str | None:
        return self.metadata.get("title")

    @property
    def url(self) -> str | None:
        return self.metadata.get("url")

    @property
    def page_type(self) -> str:
        return self.metadata.get("pageType", "unknown")

    @property
    def word_count(self) -> int:
        return int(self.metadata.get("wordCount", 0))

    @property
    def extraction_confidence(self) -> float:
        """0-1. Treat anything below 0.5 as suspect and cross-check it."""
        return float(self.metadata.get("extractionConfidence", 0.0))

    @property
    def is_suspect(self) -> bool:
        return self.extraction_confidence < 0.5

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> Document:
        return cls(
            markdown=data.get("markdown"),
            html=data.get("html"),
            raw_html=data.get("rawHtml"),
            links=data.get("links") or [],
            json_=data.get("json"),
            metadata=data.get("metadata") or {},
            cost=Cost(
                **{k: v for k, v in (data.get("cost") or {}).items() if k in Cost.__annotations__}
            ),
            raw=data,
        )


@dataclass
class CrawlJob:
    id: str
    status: str
    total: int = 0
    completed: int = 0
    failed: int = 0
    cost: dict[str, Any] = field(default_factory=dict)

    @property
    def finished(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")


class SnoopScan:
    """Synchronous client. See AsyncSnoopScan for the async form.

    `api_key` defaults to the SNOOPSCAN_API_KEY environment variable; a key
    passed here wins. `max_retries` is how many times a failed call may be
    retried after the first attempt (see the retry rule above); 0 turns
    retries off.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        key = _resolve_api_key(api_key)
        self.base_url = base_url.rstrip("/")
        self._default_timeout = timeout
        self.max_retries = _check_max_retries(max_retries)
        self._client = httpx.Client(
            timeout=timeout,
            # No client-wide Content-Type: httpx sets JSON for a JSON body and
            # the multipart boundary for an upload. A fixed JSON header here
            # overrode the boundary, so /v1/parse never saw its file.
            headers={"Authorization": f"Bearer {key}"},
        )

    # -- plumbing ---------------------------------------------------------

    def _request(self, method: str, path: str, *, safe: bool, **kwargs: Any) -> Any:
        """One call, retried when the retry rule allows, then unwrapped."""
        attempt = 0
        while True:
            response = self._client.request(method, f"{self.base_url}{path}", **kwargs)
            try:
                return self._unwrap(response)
            except SnoopScanError as error:
                wait = _retry_wait(error, attempt, safe) if attempt < self.max_retries else None
                if wait is None:
                    raise
            _sleep(wait)
            attempt += 1

    def _post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        safe: bool = True,
        http_timeout: float | None = None,
    ) -> Any:
        request_timeout = _http_timeout_for(body, http_timeout or self._default_timeout)
        return self._request("POST", path, safe=safe, json=body, timeout=request_timeout)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, safe=True, params=params)

    def _delete(self, path: str) -> Any:
        return self._request("DELETE", path, safe=True)

    @staticmethod
    def _unwrap(response: httpx.Response) -> Any:
        retry_after = _retry_after(response)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or "success" not in payload:
            # Never leak an httpx exception for an API-level failure: callers
            # branch on `SnoopScanError.code`, and a raw HTTPStatusError
            # would bypass that entirely. A body that is not the API's
            # envelope means something in front of the API answered — a
            # proxy, a load balancer, an nginx error page — so it is reported
            # as INTERNAL with the status.
            raise SnoopScanError(
                "INTERNAL",
                f"Non-JSON response from the API (HTTP {response.status_code})",
                {"status_code": response.status_code},
                status_code=response.status_code,
                retry_after=retry_after,
            )

        if not payload.get("success", False):
            error = payload.get("error") or {}
            raise SnoopScanError(
                error.get("code", "INTERNAL"),
                error.get("message", "Unknown error"),
                error.get("detail"),
                status_code=response.status_code,
                retry_after=retry_after,
            )
        return payload.get("data")

    # -- endpoints --------------------------------------------------------

    def scrape(self, url: str, **options: Any) -> Document:
        """Fetch and extract one URL."""
        return Document.from_payload(self._post("/v1/scrape", {"url": url, **options}))

    def crawl(self, url: str, **options: Any) -> CrawlJob:
        """Start a crawl. Returns immediately with a job id."""
        data = self._post("/v1/crawl", {"url": url, **options}, safe=False)
        return CrawlJob(id=data["id"], status=data["status"])

    def crawl_status(self, job_id: str) -> CrawlJob:
        data = self._get(f"/v1/crawl/{job_id}")
        return CrawlJob(
            id=data["id"],
            status=data["status"],
            total=data.get("total", 0),
            completed=data.get("completed", 0),
            failed=data.get("failed", 0),
            cost=data.get("cost") or {},
        )

    def crawl_pages(
        self, job_id: str, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[Document], str | None]:
        """One page of results, plus the cursor for the next.

        Results are paginated rather than inlined because a 10,000-page crawl
        must not arrive as a single JSON body.
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = self._get(f"/v1/crawl/{job_id}/pages", params)
        documents = [Document.from_payload(row) for row in data.get("pages", [])]
        next_link = data.get("next")
        next_cursor = next_link.split("cursor=")[-1] if next_link else None
        return documents, next_cursor

    def crawl_errors(self, job_id: str) -> list[dict[str, Any]]:
        return self._get(f"/v1/crawl/{job_id}/errors").get("errors", [])

    def cancel_crawl(self, job_id: str) -> CrawlJob:
        payload = self._delete(f"/v1/crawl/{job_id}")
        return CrawlJob(id=payload["id"], status=payload["status"])

    def crawl_and_wait(
        self,
        url: str,
        poll_interval: float = 3.0,
        max_wait: float = 900.0,
        **options: Any,
    ) -> list[Document]:
        """Start a crawl and block until it finishes, then return every page.

        Convenience only. For anything large, start the crawl and read pages
        as they arrive rather than holding the whole result in memory.
        """
        job = self.crawl(url, **options)
        deadline = time.monotonic() + max_wait

        while time.monotonic() < deadline:
            current = self.crawl_status(job.id)
            if current.finished:
                break
            time.sleep(poll_interval)
        else:
            raise SnoopScanError("TIMEOUT", f"Crawl {job.id} did not finish within {max_wait:.0f}s")

        documents: list[Document] = []
        cursor: str | None = None
        while True:
            page, cursor = self.crawl_pages(job.id, cursor)
            documents.extend(page)
            if not cursor:
                return documents

    def map(self, url: str, **options: Any) -> list[dict[str, Any]]:
        """Discover URLs without fetching page bodies. Fast and cheap."""
        return self._post("/v1/map", {"url": url, **options}).get("links", [])

    def products(self, url: str, **options: Any) -> dict[str, Any]:
        """Every product a Shopify or WooCommerce store publishes, from its own
        catalogue endpoint. Returns {platform, total, products, pages_fetched, cost}."""
        return self._post("/v1/products", {"url": url, **options})

    def posts(self, url: str, **options: Any) -> dict[str, Any]:
        """A site's posts from its API (WordPress, Substack, Squarespace,
        Discourse) or its RSS/Atom feed. Returns {platform, source, posts, cost}."""
        return self._post("/v1/posts", {"url": url, **options})

    def company(self, url: str, **options: Any) -> dict[str, Any]:
        """Everything a company's own site says about itself: firmographics
        (name, phone, address, LinkedIn, headcount, industry) plus, unless
        `contacts=False`, the emails, social links and contact form it
        publishes. Returns {domain, company, people, contacts, pagesRead, cost}."""
        return self._post("/v1/company", {"url": url, **options})

    def brand(self, domain: str, **options: Any) -> dict[str, Any]:
        """A site's brand, read from its own homepage, manifest and stylesheets:
        name, description, slogan, logo, symbol, icon and banner addresses, brand
        colors, fonts and social links. Pass `maxAge` (milliseconds, default 30
        days) to accept a stored profile; `0` reads the site again. Returns
        {name, description, slogan, domain, logo, symbol, icon, banner, logos,
        colors, fonts, socials, meta}."""
        return self._post("/v1/brand", {"domain": domain, **options})

    def domain(self, domain: str, **options: Any) -> dict[str, Any]:
        """What is known about a domain rather than a page: registration,
        DNS records, and who links to it in our own crawl graph. Every part
        is opt-out — pass `registration=False`, `dns=False` or
        `backlinks=False` to skip one. Returns {domain, registration, dns,
        backlinks, cost}."""
        return self._post("/v1/domain", {"domain": domain, **options})

    # --- domain reports: hosted API only ------------------------------------
    # A self-hosted engine has `/v1/domain` but not these three.

    def domain_report(
        self, domain: str, depth: str | None = None, **options: Any
    ) -> dict[str, Any]:
        """A buyer's report on a domain: key findings, concerns, unanswered
        questions and the evidence behind each. `depth` is `quick`, `buyer`
        (the API's default) or `deep`; options: `snapshots`, `verifyLinks`.
        Hosted API only."""
        return self._post("/v1/domain/report", _domain_report_body(domain, depth, options))

    def domain_screen(self, domains: list[str], **options: Any) -> dict[str, Any]:
        """Availability for up to 500 names in one call, from each registry's
        RDAP record and DNS. Option: `dns` (default true). Returns {counts,
        checked, results, cost}. Hosted API only."""
        return self._post(
            "/v1/domain/screen",
            {"domains": list(domains), **options},
            http_timeout=max(self._default_timeout, _SCREEN_TIMEOUT_S),
        )

    def domain_snapshots(
        self, domain: str, years: list[int] | None = None, **options: Any
    ) -> dict[str, Any]:
        """Read the archived snapshots a `buyer` report marked pending.
        `years` limits it to those years (from the report's
        `evidence.history.pending`); option: `snapshots`. Hosted API only."""
        return self._post(
            "/v1/domain/history/snapshots", _domain_snapshots_body(domain, years, options)
        )

    # --- monitors: watch pages for changes on a schedule ------------------

    def create_monitor(self, name: str, urls: list[str] | str, **options: Any) -> dict[str, Any]:
        """intervalMinutes (>= 5, default 60), goal, webhook. Returns the monitor."""
        body: dict[str, Any] = {"name": name, **options}
        body["urls" if isinstance(urls, list) else "url"] = urls
        return self._post("/v1/monitor", body, safe=False)

    def monitors(self) -> list[dict[str, Any]]:
        return self._get("/v1/monitor").get("monitors", [])

    def monitor(self, monitor_id: str) -> dict[str, Any]:
        return self._get(f"/v1/monitor/{monitor_id}")

    def delete_monitor(self, monitor_id: str) -> None:
        self._delete(f"/v1/monitor/{monitor_id}")

    def run_monitor(self, monitor_id: str) -> dict[str, Any]:
        """A check right now; returns it."""
        return self._post(f"/v1/monitor/{monitor_id}/run", {}, safe=False)

    def monitor_checks(self, monitor_id: str, limit: int = 20) -> list[dict[str, Any]]:
        out = self._get(f"/v1/monitor/{monitor_id}/checks", params={"limit": limit})
        return out.get("checks", [])

    def batch_scrape(self, urls: list[str], **options: Any) -> CrawlJob:
        data = self._post("/v1/batch/scrape", {"urls": urls, **options}, safe=False)
        return CrawlJob(id=data["id"], status=data["status"])

    def extract(
        self,
        urls: list[str],
        schema: dict[str, Any] | None = None,
        prompt: str | None = None,
        *,
        template: str | None = None,
        **options: Any,
    ) -> list[dict[str, Any]]:
        """Schema-constrained extraction.

        Give either your own `schema` or a named `template` — `product`,
        `article`, `jobPosting` and the rest, which `templates()` lists. A
        template is a curated schema whose fields are the ones sites already
        publish in their own markup, so they usually come back without a model
        being called at all.

        Output is validated before it is returned, so a row with an `error`
        means the page genuinely lacked the fields — not that the request
        should be retried.
        """
        if (schema is None) == (template is None):
            raise ValueError(
                "give either `schema` or `template`, not both and not neither: "
                "a template IS a schema, and silently picking one would return "
                "fields you did not ask for"
            )
        body: dict[str, Any] = {"urls": urls, **options}
        if schema is not None:
            body["schema"] = schema
        if template is not None:
            body["template"] = template
        if prompt:
            body["prompt"] = prompt
        return self._post("/v1/extract", body)

    def templates(self) -> list[dict[str, Any]]:
        """The named templates this deployment offers, with their fields."""
        answer = self._get("/v1/templates")
        return list(answer.get("templates", []))

    def search(self, query: str, **options: Any) -> dict[str, Any]:
        return self._post("/v1/search", {"query": query, **options})

    def parse(
        self,
        *,
        url: str | None = None,
        path: str | os.PathLike[str] | None = None,
        content: str | bytes | None = None,
        filename: str | None = None,
    ) -> dict[str, Any]:
        """Turn a document into markdown: PDF, DOCX, HTML, TXT or Markdown.

        Exactly one of:
          * `path`: a file on disk, uploaded as it is (binary-safe, so a PDF
            survives the trip).
          * `content`: a document already in memory; `filename` says what
            kind it is (default `document.txt` for text, `document.pdf` is
            yours to name for bytes).
          * `url`: a document on the web. The engine fetches it with every
            tier it has and reads PDFs, HTML and text, then returns the same
            shape as an upload.

        `/v1/parse` takes a multipart file and nothing else, so a file is
        sent as its raw bytes and a URL goes through scrape instead.
        """
        kind = _parse_input(url, path, content)
        if kind == "url":
            assert url is not None
            return _parse_from_scrape(url, self._post("/v1/scrape", _parse_scrape_body(url)))
        name, data = _parse_upload(path, content, filename)
        return self._request(
            "POST",
            "/v1/parse",
            safe=True,
            files={"file": (name, data)},
            timeout=self._default_timeout,
        )

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SnoopScan:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class AsyncSnoopScan:
    """Async client. Same surface as the synchronous one."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        key = _resolve_api_key(api_key)
        self.base_url = base_url.rstrip("/")
        self._default_timeout = timeout
        self.max_retries = _check_max_retries(max_retries)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            # No client-wide Content-Type: httpx sets JSON for a JSON body and
            # the multipart boundary for an upload. A fixed JSON header here
            # overrode the boundary, so /v1/parse never saw its file.
            headers={"Authorization": f"Bearer {key}"},
        )

    async def _request(self, method: str, path: str, *, safe: bool, **kwargs: Any) -> Any:
        """As `SnoopScan._request`: the same retry rule, awaited."""
        attempt = 0
        while True:
            response = await self._client.request(method, f"{self.base_url}{path}", **kwargs)
            try:
                return SnoopScan._unwrap(response)
            except SnoopScanError as error:
                wait = _retry_wait(error, attempt, safe) if attempt < self.max_retries else None
                if wait is None:
                    raise
            await _async_sleep(wait)
            attempt += 1

    async def _post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        safe: bool = True,
        http_timeout: float | None = None,
    ) -> Any:
        request_timeout = _http_timeout_for(body, http_timeout or self._default_timeout)
        return await self._request("POST", path, safe=safe, json=body, timeout=request_timeout)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, safe=True, params=params)

    async def _delete(self, path: str) -> Any:
        return await self._request("DELETE", path, safe=True)

    async def scrape(self, url: str, **options: Any) -> Document:
        return Document.from_payload(await self._post("/v1/scrape", {"url": url, **options}))

    async def crawl(self, url: str, **options: Any) -> CrawlJob:
        data = await self._post("/v1/crawl", {"url": url, **options}, safe=False)
        return CrawlJob(id=data["id"], status=data["status"])

    async def crawl_status(self, job_id: str) -> CrawlJob:
        data = await self._get(f"/v1/crawl/{job_id}")
        return CrawlJob(
            id=data["id"],
            status=data["status"],
            total=data.get("total", 0),
            completed=data.get("completed", 0),
            failed=data.get("failed", 0),
            cost=data.get("cost") or {},
        )

    async def crawl_pages(
        self, job_id: str, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[Document], str | None]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = await self._get(f"/v1/crawl/{job_id}/pages", params)
        documents = [Document.from_payload(row) for row in data.get("pages", [])]
        next_link = data.get("next")
        next_cursor = next_link.split("cursor=")[-1] if next_link else None
        return documents, next_cursor

    async def crawl_errors(self, job_id: str) -> list[dict[str, Any]]:
        return (await self._get(f"/v1/crawl/{job_id}/errors")).get("errors", [])

    async def cancel_crawl(self, job_id: str) -> CrawlJob:
        payload = await self._delete(f"/v1/crawl/{job_id}")
        return CrawlJob(id=payload["id"], status=payload["status"])

    async def crawl_and_wait(
        self,
        url: str,
        poll_interval: float = 3.0,
        max_wait: float = 900.0,
        **options: Any,
    ) -> list[Document]:
        """Start a crawl and wait until it finishes, then return every page.

        Convenience only. For anything large, start the crawl and read pages
        as they arrive rather than holding the whole result in memory.
        """
        job = await self.crawl(url, **options)
        deadline = time.monotonic() + max_wait

        while time.monotonic() < deadline:
            current = await self.crawl_status(job.id)
            if current.finished:
                break
            await asyncio.sleep(poll_interval)
        else:
            raise SnoopScanError("TIMEOUT", f"Crawl {job.id} did not finish within {max_wait:.0f}s")

        documents: list[Document] = []
        cursor: str | None = None
        while True:
            page, cursor = await self.crawl_pages(job.id, cursor)
            documents.extend(page)
            if not cursor:
                return documents

    async def map(self, url: str, **options: Any) -> list[dict[str, Any]]:
        return (await self._post("/v1/map", {"url": url, **options})).get("links", [])

    async def products(self, url: str, **options: Any) -> dict[str, Any]:
        return await self._post("/v1/products", {"url": url, **options})

    async def posts(self, url: str, **options: Any) -> dict[str, Any]:
        return await self._post("/v1/posts", {"url": url, **options})

    async def company(self, url: str, **options: Any) -> dict[str, Any]:
        return await self._post("/v1/company", {"url": url, **options})

    async def brand(self, domain: str, **options: Any) -> dict[str, Any]:
        return await self._post("/v1/brand", {"domain": domain, **options})

    async def domain(self, domain: str, **options: Any) -> dict[str, Any]:
        return await self._post("/v1/domain", {"domain": domain, **options})

    async def domain_report(
        self, domain: str, depth: str | None = None, **options: Any
    ) -> dict[str, Any]:
        """As `SnoopScan.domain_report`. Hosted API only."""
        return await self._post("/v1/domain/report", _domain_report_body(domain, depth, options))

    async def domain_screen(self, domains: list[str], **options: Any) -> dict[str, Any]:
        """As `SnoopScan.domain_screen`. Hosted API only."""
        return await self._post(
            "/v1/domain/screen",
            {"domains": list(domains), **options},
            http_timeout=max(self._default_timeout, _SCREEN_TIMEOUT_S),
        )

    async def domain_snapshots(
        self, domain: str, years: list[int] | None = None, **options: Any
    ) -> dict[str, Any]:
        """As `SnoopScan.domain_snapshots`. Hosted API only."""
        return await self._post(
            "/v1/domain/history/snapshots", _domain_snapshots_body(domain, years, options)
        )

    async def batch_scrape(self, urls: list[str], **options: Any) -> CrawlJob:
        data = await self._post("/v1/batch/scrape", {"urls": urls, **options}, safe=False)
        return CrawlJob(id=data["id"], status=data["status"])

    async def search(self, query: str, **options: Any) -> dict[str, Any]:
        return await self._post("/v1/search", {"query": query, **options})

    async def parse(
        self,
        *,
        url: str | None = None,
        path: str | os.PathLike[str] | None = None,
        content: str | bytes | None = None,
        filename: str | None = None,
    ) -> dict[str, Any]:
        """As `SnoopScan.parse`."""
        kind = _parse_input(url, path, content)
        if kind == "url":
            assert url is not None
            return _parse_from_scrape(url, await self._post("/v1/scrape", _parse_scrape_body(url)))
        name, data = _parse_upload(path, content, filename)
        return await self._request(
            "POST",
            "/v1/parse",
            safe=True,
            files={"file": (name, data)},
            timeout=self._default_timeout,
        )

    async def create_monitor(
        self, name: str, urls: list[str] | str, **options: Any
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, **options}
        body["urls" if isinstance(urls, list) else "url"] = urls
        return await self._post("/v1/monitor", body, safe=False)

    async def monitors(self) -> list[dict[str, Any]]:
        return (await self._get("/v1/monitor")).get("monitors", [])

    async def monitor(self, monitor_id: str) -> dict[str, Any]:
        return await self._get(f"/v1/monitor/{monitor_id}")

    async def delete_monitor(self, monitor_id: str) -> None:
        await self._delete(f"/v1/monitor/{monitor_id}")

    async def run_monitor(self, monitor_id: str) -> dict[str, Any]:
        return await self._post(f"/v1/monitor/{monitor_id}/run", {}, safe=False)

    async def monitor_checks(self, monitor_id: str, limit: int = 20) -> list[dict[str, Any]]:
        out = await self._get(f"/v1/monitor/{monitor_id}/checks", params={"limit": limit})
        return out.get("checks", [])

    async def extract(
        self,
        urls: list[str],
        schema: dict[str, Any] | None = None,
        prompt: str | None = None,
        *,
        template: str | None = None,
        **options: Any,
    ) -> list[dict[str, Any]]:
        """Your own `schema` or a named `template`. See SnoopScan.extract."""
        if (schema is None) == (template is None):
            raise ValueError(
                "give either `schema` or `template`, not both and not neither: "
                "a template IS a schema, and silently picking one would return "
                "fields you did not ask for"
            )
        body: dict[str, Any] = {"urls": urls, **options}
        if schema is not None:
            body["schema"] = schema
        if template is not None:
            body["template"] = template
        if prompt:
            body["prompt"] = prompt
        return await self._post("/v1/extract", body)

    async def templates(self) -> list[dict[str, Any]]:
        """The named templates this deployment offers, with their fields."""
        answer = await self._get("/v1/templates")
        return list(answer.get("templates", []))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsyncSnoopScan:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


# -- parse ----------------------------------------------------------------
# Shared by both clients, so the two cannot disagree about what parse sends.

_DOCUMENT_ONLY_BY_UPLOAD = (".docx",)


def _parse_input(url: Any, path: Any, content: Any) -> str:
    given = [
        name
        for name, value in (("url", url), ("path", path), ("content", content))
        if value is not None and value != ""
    ]
    if len(given) != 1:
        raise ValueError("parse takes exactly one of url, path or content")
    if given[0] == "url" and str(url).lower().split("?", 1)[0].endswith(_DOCUMENT_ONLY_BY_UPLOAD):
        raise ValueError(
            "A Word document is read from a file, not a URL: download it and pass path=."
        )
    return given[0]


def _parse_upload(path: Any, content: Any, filename: str | None) -> tuple[str, bytes]:
    if path is not None:
        with open(path, "rb") as handle:
            return filename or os.path.basename(os.fspath(path)), handle.read()
    if isinstance(content, str):
        return filename or "document.txt", content.encode("utf-8")
    return filename or "document", bytes(content)


def _parse_scrape_body(url: str) -> dict[str, Any]:
    return {"url": url, "formats": ["markdown"], "parsers": ["pdf"]}


def _parse_from_scrape(url: str, data: dict[str, Any]) -> dict[str, Any]:
    """A scrape answer in the shape `/v1/parse` returns, so callers need not care which ran."""
    meta = data.get("metadata") or {}
    return {
        "markdown": data.get("markdown") or "",
        "url": meta.get("sourceURL") or meta.get("url") or url,
        "kind": "url",
        "metadata": meta,
        "cost": data.get("cost"),
    }


# -- domain reports --------------------------------------------------------
# Shared by both clients.


def _domain_report_body(domain: str, depth: str | None, options: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"domain": domain, **options}
    if depth is not None:
        body["depth"] = depth
    return body


def _domain_snapshots_body(
    domain: str, years: list[int] | None, options: dict[str, Any]
) -> dict[str, Any]:
    body: dict[str, Any] = {"domain": domain, **options}
    if years is not None:
        body["years"] = list(years)
    return body
