"""Retries, the key from the environment, and the domain report tools.

The same rule as the Go, PHP, Ruby, Java, .NET, Rust and Elixir SDKs. No
network: every request goes to an httpx.MockTransport, and the sleeps are
recorded rather than slept.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import snoopscan.client as client_module
from snoopscan import AsyncSnoopScan, SnoopScan, SnoopScanError

BASE = "https://api.example.test"


def ok(data: Any = None) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": data if data is not None else {}})


def fail(status: int, code: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json={"success": False, "error": {"code": code, "message": code.lower()}},
        headers=headers,
    )


def gateway(status: int) -> httpx.Response:
    return httpx.Response(status, text="<html>bad gateway</html>")


class Script:
    """Answers each request with the next response, recording the request."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waited: list[float] = []
    monkeypatch.setattr(client_module, "_sleep", waited.append)

    async def async_sleep(seconds: float) -> None:
        waited.append(seconds)

    monkeypatch.setattr(client_module, "_async_sleep", async_sleep)
    return waited


def make(script: Script, **kwargs: Any) -> SnoopScan:
    client = SnoopScan(api_key="sk_test", base_url=BASE, **kwargs)
    client._client = httpx.Client(
        transport=httpx.MockTransport(script), headers=client._client.headers
    )
    return client


def make_async(script: Script, **kwargs: Any) -> AsyncSnoopScan:
    client = AsyncSnoopScan(api_key="sk_test", base_url=BASE, **kwargs)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(script), headers=client._client.headers
    )
    return client


# -- retries -----------------------------------------------------------------


def test_a_429_is_retried_after_its_retry_after(sleeps: list[float]) -> None:
    script = Script(fail(429, "RATE_LIMITED", {"Retry-After": "3"}), ok({"markdown": "hi"}))
    page = make(script).scrape("https://a.test/")
    assert page.markdown == "hi"
    assert len(script.requests) == 2
    assert sleeps == [3.0]


def test_a_retry_after_is_capped_at_60_seconds(sleeps: list[float]) -> None:
    script = Script(fail(429, "RATE_LIMITED", {"Retry-After": "600"}), ok())
    make(script).search("q")
    assert sleeps == [60.0]


def test_a_429_is_retried_even_on_a_call_that_starts_a_job(sleeps: list[float]) -> None:
    """The rate limiter refused before anything ran, so nothing was started."""
    script = Script(fail(429, "RATE_LIMITED"), ok({"id": "job_1", "status": "scraping"}))
    job = make(script).crawl("https://a.test/")
    assert job.id == "job_1"
    assert len(script.requests) == 2
    assert 0.5 <= sleeps[0] <= 0.625


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_gateway_failure_is_retried_on_a_safe_call(status: int, sleeps: list[float]) -> None:
    script = Script(gateway(status), ok({"markdown": "hi"}))
    assert make(script).scrape("https://a.test/").markdown == "hi"
    assert len(script.requests) == 2


def test_the_engine_saying_internal_is_retried(sleeps: list[float]) -> None:
    script = Script(fail(500, "INTERNAL"), ok({"links": []}))
    assert make(script).map("https://a.test/") == []
    assert len(script.requests) == 2


@pytest.mark.parametrize("code", ["BLOCKED", "TARGET_ERROR", "TIMEOUT", "FETCH_FAILED"])
def test_an_engine_verdict_is_never_retried(code: str, sleeps: list[float]) -> None:
    script = Script(fail(502, code), ok())
    with pytest.raises(SnoopScanError) as exc:
        make(script).scrape("https://a.test/")
    assert exc.value.code == code
    assert exc.value.status_code == 502
    assert len(script.requests) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.crawl("https://a.test/"),
        lambda c: c.batch_scrape(["https://a.test/"]),
        lambda c: c.create_monitor("m", ["https://a.test/"]),
        lambda c: c.run_monitor("mon_1"),
    ],
    ids=["crawl", "batch_scrape", "create_monitor", "run_monitor"],
)
def test_a_call_that_starts_or_creates_something_is_never_retried_on_a_5xx(
    call: Any, sleeps: list[float]
) -> None:
    script = Script(gateway(502), ok({"id": "x", "status": "scraping"}))
    with pytest.raises(SnoopScanError) as exc:
        call(make(script))
    assert exc.value.code == "INTERNAL"
    assert len(script.requests) == 1


def test_other_4xx_are_not_retried(sleeps: list[float]) -> None:
    script = Script(fail(400, "INVALID_REQUEST"), ok())
    with pytest.raises(SnoopScanError):
        make(script).scrape("https://a.test/")
    assert len(script.requests) == 1


def test_retries_stop_after_two_with_a_doubling_backoff(sleeps: list[float]) -> None:
    script = Script(gateway(503), gateway(503), gateway(503), ok())
    with pytest.raises(SnoopScanError) as exc:
        make(script).scrape("https://a.test/")
    assert exc.value.status_code == 503
    assert len(script.requests) == 3
    assert 0.5 <= sleeps[0] <= 0.625
    assert 1.0 <= sleeps[1] <= 1.25


def test_the_backoff_is_capped_at_eight_seconds() -> None:
    for attempt in range(10):
        assert client_module._backoff(attempt) <= 8.0 * 1.25
    assert client_module._backoff(9) >= 8.0


def test_max_retries_is_configurable(sleeps: list[float]) -> None:
    script = Script(gateway(503), gateway(503), gateway(503), gateway(503), ok({"markdown": "x"}))
    assert make(script, max_retries=4).scrape("https://a.test/").markdown == "x"
    assert len(script.requests) == 5


def test_max_retries_zero_turns_retries_off(sleeps: list[float]) -> None:
    script = Script(fail(429, "RATE_LIMITED", {"Retry-After": "1"}), ok())
    with pytest.raises(SnoopScanError) as exc:
        make(script, max_retries=0).scrape("https://a.test/")
    assert exc.value.retry_after == 1.0
    assert len(script.requests) == 1
    assert sleeps == []


def test_a_negative_max_retries_is_refused() -> None:
    with pytest.raises(ValueError, match="max_retries"):
        SnoopScan(api_key="k", max_retries=-1)


def test_gets_deletes_and_uploads_are_retried(sleeps: list[float]) -> None:
    script = Script(
        gateway(502),
        ok({"monitors": []}),
        gateway(502),
        ok({}),
        gateway(502),
        ok({"markdown": "doc"}),
    )
    client = make(script)
    assert client.monitors() == []
    client.delete_monitor("mon_1")
    assert client.parse(content="hello")["markdown"] == "doc"
    assert len(script.requests) == 6
    assert script.requests[5].headers["content-type"].startswith("multipart/form-data")


def test_the_async_client_follows_the_same_rule(sleeps: list[float]) -> None:
    async def run() -> None:
        script = Script(
            fail(429, "RATE_LIMITED", {"Retry-After": "2"}), gateway(502), ok({"markdown": "a"})
        )
        client = make_async(script)
        assert (await client.scrape("https://a.test/")).markdown == "a"
        assert len(script.requests) == 3

        once = Script(gateway(502), ok({"id": "j", "status": "scraping"}))
        with pytest.raises(SnoopScanError):
            await make_async(once).crawl("https://a.test/")
        assert len(once.requests) == 1

        off = Script(gateway(502), ok())
        with pytest.raises(SnoopScanError):
            await make_async(off, max_retries=0).search("q")
        assert len(off.requests) == 1
        await client.aclose()

    asyncio.run(run())
    assert sleeps[0] == 2.0


# -- the key -------------------------------------------------------------------


def auth_header(client: SnoopScan | AsyncSnoopScan) -> str:
    return client._client.headers["Authorization"]


def test_the_key_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SNOOPSCAN_API_KEY", "sk_env")
    assert auth_header(SnoopScan()) == "Bearer sk_env"
    assert auth_header(AsyncSnoopScan()) == "Bearer sk_env"


def test_a_key_passed_wins_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SNOOPSCAN_API_KEY", "sk_env")
    assert auth_header(SnoopScan("sk_mine")) == "Bearer sk_mine"
    assert auth_header(AsyncSnoopScan(api_key="sk_mine")) == "Bearer sk_mine"


def test_no_key_anywhere_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SNOOPSCAN_API_KEY", raising=False)
    with pytest.raises(ValueError, match="SNOOPSCAN_API_KEY"):
        SnoopScan()
    with pytest.raises(ValueError, match="SNOOPSCAN_API_KEY"):
        AsyncSnoopScan()


# -- domain tools ----------------------------------------------------------------


def test_domain_report_sends_the_domain_and_depth(sleeps: list[float]) -> None:
    script = Script(ok({"domain": "example.com"}), ok({"domain": "example.com"}))
    client = make(script)
    assert client.domain_report("example.com", depth="quick")["domain"] == "example.com"
    client.domain_report("example.com", snapshots=10, verifyLinks=5)
    assert script.requests[0].url.path == "/v1/domain/report"
    assert script.bodies() == [
        {"domain": "example.com", "depth": "quick"},
        {"domain": "example.com", "snapshots": 10, "verifyLinks": 5},
    ]


def test_domain_screen_sends_the_list_and_gets_a_longer_deadline() -> None:
    script = Script(ok({"counts": {"registered": 2}, "checked": 2, "results": []}))
    client = make(script)
    names = [f"name{i}.com" for i in range(500)]
    out = client.domain_screen(names, dns=False)
    assert out["checked"] == 2
    request = script.requests[0]
    assert request.url.path == "/v1/domain/screen"
    assert script.bodies()[0] == {"domains": names, "dns": False}
    assert request.extensions["timeout"]["read"] == 300.0


def test_domain_snapshots_sends_the_years() -> None:
    script = Script(ok({"snapshots": [], "pending": []}), ok({"snapshots": []}))
    client = make(script)
    client.domain_snapshots("example.com", years=[2014, 2019], snapshots=6)
    client.domain_snapshots("example.com")
    assert script.requests[0].url.path == "/v1/domain/history/snapshots"
    assert script.bodies() == [
        {"domain": "example.com", "snapshots": 6, "years": [2014, 2019]},
        {"domain": "example.com"},
    ]


def test_the_domain_tools_are_safe_to_retry(sleeps: list[float]) -> None:
    script = Script(gateway(502), ok({}), gateway(503), ok({}), gateway(504), ok({}))
    client = make(script)
    client.domain_report("a.com")
    client.domain_screen(["a.com"])
    client.domain_snapshots("a.com")
    assert len(script.requests) == 6


def test_the_async_domain_tools_match_the_sync_ones() -> None:
    async def run() -> list[dict[str, Any]]:
        script = Script(ok({}), ok({}), ok({}))
        client = make_async(script)
        await client.domain_report("a.com", depth="deep")
        await client.domain_screen(["a.com", "b.io"])
        await client.domain_snapshots("a.com", years=[2020])
        await client.aclose()
        assert [r.url.path for r in script.requests] == [
            "/v1/domain/report",
            "/v1/domain/screen",
            "/v1/domain/history/snapshots",
        ]
        return script.bodies()

    assert asyncio.run(run()) == [
        {"domain": "a.com", "depth": "deep"},
        {"domains": ["a.com", "b.io"]},
        {"domain": "a.com", "years": [2020]},
    ]


def test_parse_by_url_reads_through_scrape() -> None:
    script = Script(ok({"markdown": "# pdf", "metadata": {"sourceURL": "https://a.test/x.pdf"}}))
    out = make(script).parse(url="https://a.test/x.pdf")
    assert out["markdown"] == "# pdf"
    assert out["kind"] == "url"
    assert script.requests[0].url.path == "/v1/scrape"
    assert script.bodies()[0]["parsers"] == ["pdf"]


def test_brand_sends_the_domain_and_the_max_age() -> None:
    script = Script(ok({"name": "Stripe", "domain": "stripe.com"}), ok({}))
    client = make(script)
    assert client.brand("stripe.com")["name"] == "Stripe"
    client.brand("stripe.com", maxAge=0)
    assert [r.url.path for r in script.requests] == ["/v1/brand", "/v1/brand"]
    assert script.bodies() == [{"domain": "stripe.com"}, {"domain": "stripe.com", "maxAge": 0}]


def test_the_async_brand_matches_the_sync_one() -> None:
    async def run() -> list[dict[str, Any]]:
        script = Script(ok({}))
        client = make_async(script)
        await client.brand("stripe.com", maxAge=1000)
        await client.aclose()
        return script.bodies()

    assert asyncio.run(run()) == [{"domain": "stripe.com", "maxAge": 1000}]
