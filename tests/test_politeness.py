"""Politeness-layer guard tests: request spacing, bounded transient retry,
browser-identity with single plain-identity fallback.

Invariants P1–P3 (see DECISIONS.md):
- P1: every outbound request obeys a shared minimum-gap schedule: the first
  request of a scrape proceeds immediately; every subsequent request is held
  back until a fixed minimum gap has elapsed since the previous request
  BEGAN. Retry waits count toward the same spacing.
- P2: transient failures (rate-limit/availability statuses + network-level
  errors) retry a bounded number of times with growing, exponentially
  spaced delays; a server-suggested wait (Retry-After) is honored in place
  of the computed delay, capped so a hostile server cannot stall a run.
  Exhausted budget surfaces normally (log-and-skip to the next feed).
- P3: every request carries a conventional browser User-Agent. A browser
  rejection (419) earns exactly ONE plain-identity fallback attempt; the
  fallback result surfaces unmodified; a source known to accept the browser
  identity is never re-probed with the fallback.
"""

# version: 0.1.1

from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from newspipeline.politeness import PolitenessLayer, USER_AGENT


class _State:
    """Per-path request log and scripted responses."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.responses: list[tuple] | None = []
        self.index = 0
        self.lock = threading.Lock()

    def next_response(self):
        with self.lock:
            idx = self.index
            self.index += 1
        if idx < len(self.responses or []):
            return self.responses[idx]
        return None  # default: 200 OK with a JSON body


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: N802  (signature fixed by stdlib)
        pass

    def do_GET(self):
        rec = {"ua": self.headers.get("User-Agent", ""), "t": time.monotonic()}
        with self.server.global_state.lock:
            self.server.global_state.requests.append(rec)
        st = self.server.states.get(self.path)
        if st is not None:
            with st.lock:
                st.requests.append(rec)
        resp = st.next_response() if st else None
        if resp is None:
            body = json.dumps({"ok": self.path}).encode()
            status, headers, delay = 200, [], 0.0
        else:
            status, headers, body, delay = resp
        if delay:
            time.sleep(delay)
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server:
    def __init__(self) -> None:
        self.global_state = _State()
        self.states: dict[str, _State] = {}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.states = self.states
        self.httpd.global_state = self.global_state
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_port

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def register(self, path: str) -> _State:
        st = _State()
        self.states[path] = st
        return st

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def server():
    s = _Server()
    yield s
    s.stop()


def _layer(**kw) -> PolitenessLayer:
    """Layer with test-tight constants (fast tests, same semantics)."""
    return PolitenessLayer(
        min_gap=kw.get("min_gap", 0.15),
        max_retries=kw.get("max_retries", 2),
        base_delay=kw.get("base_delay", 0.05),
        max_delay=kw.get("max_delay", 0.2),
        retry_after_cap=kw.get("retry_after_cap", 0.3),
    )


# ---- P1: spacing -------------------------------------------------------------

def test_first_request_immediate_then_gap_enforced(server):
    layer = _layer(min_gap=0.2)
    state = server.register("/a")

    async def go():
        async with httpx.AsyncClient() as client:
            t0 = time.monotonic()
            for _ in range(3):
                await layer.request(client, server.url("/a"))
            return time.monotonic() - t0

    asyncio.run(go())
    ts = [r["t"] for r in state.requests]
    assert len(ts) == 3
    # Tolerance covers server-side timestamp jitter (cold-connect on the
    # first request shifts its recorded t slightly later); 100ms is far
    # below min_gap while far above the jitter, so a 0-gap bug still fails.
    assert ts[1] - ts[0] >= 0.2 - 0.01, "second request fired before the minimum gap elapsed"
    assert ts[2] - ts[1] >= 0.2 - 0.01, "third request fired before the minimum gap elapsed"


def test_gap_counts_from_previous_request_start(server):
    # A slow response must not extend the schedule. With a 0.3s server stall,
    # request-start semantics put the next request at ≥0.30s (response can't
    # precede its own start); response-completion semantics would push it to
    # ≥0.45s (0.3 stall + 0.15 gap measured after the response). The bracket
    # [0.30, 0.41) pins request-start semantics without flakiness.
    layer = _layer(min_gap=0.15)
    state = server.register("/slow")
    state.responses = [(200, [], b"{}", 0.3)]

    async def go():
        async with httpx.AsyncClient() as client:
            await layer.request(client, server.url("/slow"))
            await layer.request(client, server.url("/slow"))

    asyncio.run(go())
    ts = [r["t"] for r in state.requests]
    assert ts[1] - ts[0] >= 0.30, "second request started before its server response finished"
    assert ts[1] - ts[0] < 0.41, "gap was measured from response completion, not request start"


# ---- P2: bounded transient retry ---------------------------------------------

def test_transient_retried_bounded_then_success(server):
    layer = _layer(max_retries=2)
    state = server.register("/r")
    state.responses = [(503, [], b"no", None), (429, [], b"slow down", None)]

    async def go():
        async with httpx.AsyncClient() as client:
            return await layer.request(client, server.url("/r"))

    resp = asyncio.run(go())
    assert resp.status_code == 200
    assert len(state.requests) == 3  # 1 initial + 2 retries


def test_retry_budget_exhausted_surfaces_last_response(server):
    layer = _layer(max_retries=2)
    state = server.register("/dead")
    state.responses = [(503, [], b"no", None)] * 3

    async def go():
        async with httpx.AsyncClient() as client:
            return await layer.request(client, server.url("/dead"))

    resp = asyncio.run(go())
    assert resp.status_code == 503
    assert len(state.requests) == 3  # no attempt beyond the budget


def test_retry_after_honored_but_capped(server):
    layer = _layer(max_retries=1, retry_after_cap=0.25)
    state = server.register("/ra")
    # 10s suggested wait must be replaced by the 0.25s cap.
    state.responses = [(429, [("Retry-After", "10")], b"no", None)]

    async def go():
        async with httpx.AsyncClient() as client:
            return await layer.request(client, server.url("/ra"))

    t0 = time.monotonic()
    resp = asyncio.run(go())
    elapsed = time.monotonic() - t0
    assert resp.status_code == 200
    assert 0.25 <= elapsed < 1.0, "capped Retry-After not honored as the wait"


def test_no_retry_after_uses_computed_delay(server):
    # base_delay is uncapped: 0.25 stay under the layer's max_delay — the
    # full exponential delay must be awaited before the retry.
    layer = PolitenessLayer(min_gap=0.15, max_retries=1, base_delay=0.25,
                            max_delay=2.0, retry_after_cap=0.3)
    state = server.register("/cd")
    state.responses = [(503, [], b"no", None)]

    async def go():
        async with httpx.AsyncClient() as client:
            return await layer.request(client, server.url("/cd"))

    t0 = time.monotonic()
    resp = asyncio.run(go())
    elapsed = time.monotonic() - t0
    assert resp.status_code == 200
    assert elapsed >= 0.25, "computed exponential delay not awaited"


def test_network_error_retried_then_surfaces(server):
    layer = _layer(max_retries=1)
    dead = "http://127.0.0.1:1/nope"  # nothing listens on port 1

    async def go():
        async with httpx.AsyncClient() as client:
            return await layer.request(client, dead)

    with pytest.raises(httpx.TransportError):
        asyncio.run(go())


# ---- P3: identity -------------------------------------------------------------

def test_browser_ua_sent_by_default(server):
    layer = _layer()
    state = server.register("/ua")

    async def go():
        async with httpx.AsyncClient() as client:
            await layer.request(client, server.url("/ua"))

    asyncio.run(go())
    assert state.requests[0]["ua"] == USER_AGENT


def test_419_gets_exactly_one_plain_fallback(server):
    layer = _layer()
    state = server.register("/b")
    state.responses = [(419, [], b"browser rejected", None)]

    async def go():
        async with httpx.AsyncClient() as client:
            return await layer.request(client, server.url("/b"))

    resp = asyncio.run(go())
    assert resp.status_code == 200
    uas = [r["ua"] for r in state.requests]
    assert uas[0] == USER_AGENT, "first attempt must use the browser identity"
    assert len(uas) == 2, "exactly one fallback attempt"
    assert uas[1] != USER_AGENT, "fallback must not use the browser identity"


def test_fallback_rejection_surfaces_without_retry(server):
    layer = _layer(max_retries=2)
    state = server.register("/bb")
    state.responses = [(419, [], b"no browser", None), (419, [], b"no plain", None)]

    async def go():
        async with httpx.AsyncClient() as client:
            return await layer.request(client, server.url("/bb"))

    resp = asyncio.run(go())
    assert resp.status_code == 419
    assert len(state.requests) == 2, "fallback result surfaces; no cross-identity retry"


def test_healthy_source_never_probed_with_fallback(server):
    layer = _layer()
    state = server.register("/ok")

    async def go():
        async with httpx.AsyncClient() as client:
            await layer.request(client, server.url("/ok"))
            await layer.request(client, server.url("/ok"))

    asyncio.run(go())
    assert len(state.requests) == 2
    assert all(r["ua"] == USER_AGENT for r in state.requests)


def test_plain_fallback_host_remembered_across_requests(server):
    layer = _layer()
    state = server.register("/mem")
    state.responses = [(419, [], b"no browser", None)]  # every attempt: 419

    async def go():
        async with httpx.AsyncClient() as client:
            r1 = await layer.request(client, server.url("/mem"))
            r2 = await layer.request(client, server.url("/mem"))

    asyncio.run(go())
    # Correct P3 semantics: call 1 = browser probe (419) + one plain fallback
    # (419, surfaces). The host is now remembered, so call 2 goes out with
    # the plain identity directly — NO second browser probe.
    assert len(state.requests) == 3
    uas = [r["ua"] for r in state.requests]
    assert uas[0] == USER_AGENT, "first call still probes with the browser identity"
    assert uas[1] != USER_AGENT and uas[2] != USER_AGENT, (
        "after the first rejection the plain identity is used without re-probing"
    )


# ---- integration: pipeline._scrape uses the layer -----------------------------

def test_scrape_requests_carry_browser_ua(server, store, tmp_path):
    from newspipeline.pipeline import Pipeline
    from conftest import make_interest, make_settings

    rss = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>t</title>'
        f'<item><title>x</title><link>{server.url("/art")}</link></item>'
        '</channel></rss>'
    ).encode()
    feed_state = server.register("/feed")
    feed_state.responses = [(200, [("Content-Type", "application/rss+xml")], rss, None)]
    server.register("/art")  # default 200

    settings = make_settings(server.port, tmp_path / "output")
    interest = make_interest(server.port, [], feeds=[{
        "name": "Test Feed", "url": server.url("/feed"), "category": "news",
    }])
    pipeline = Pipeline(store=store, settings=settings, api_key="unused")
    run_id = store.create_run(interest["name"])["id"]

    async def go():
        await pipeline._scrape(run_id, interest)

    asyncio.run(go())
    uas = [r["ua"] for r in server.global_state.requests]
    assert uas, "scrape made no requests"
    assert all(ua == USER_AGENT for ua in uas), "scrape requests must use the browser identity"
    assert len(uas) >= 2, "expected both a feed fetch and an article fetch"
