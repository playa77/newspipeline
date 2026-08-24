"""Guard tests for invariant G6 (see DECISIONS.md): a stalled feed FETCH
must not block the event loop.

Scope note: this pins one regression shape — blocking work on the loop that
begins with the scrape's first feed request and outlasts the deadline.
It cannot detect blocking work confined to an executor thread or beginning
after the first response; those shapes are covered by code review only.
"""

# version: 0.1.2

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from newspipeline.pipeline import Pipeline

from conftest import make_interest, make_settings

SLOW_FEED_SECONDS = 2.0

SLOW_FEED_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Slow Feed</title></channel></rss>
"""


class _SlowFeedHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path == "/slow.xml":
            # Stall long enough that a loop-blocking scrape would blow the
            # 1.0s deadline below.
            threading.Event().wait(SLOW_FEED_SECONDS)
            body, ctype = SLOW_FEED_XML, "application/rss+xml"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def slow_feed_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowFeedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _scrape_interest(slow_feed_server, store, tmp_path: Path, url_path: str):
    port = slow_feed_server.server_port
    settings = make_settings(port, tmp_path / "output")
    interest = make_interest(port, [], feeds=[{
        "name": "Test Feed", "url": f"http://127.0.0.1:{port}{url_path}",
        "category": "news",
    }])
    return Pipeline(store=store, settings=settings, api_key="unused"), interest


def test_scrape_keeps_event_loop_responsive(slow_feed_server, store, tmp_path):
    pipeline, interest = _scrape_interest(
        slow_feed_server, store, tmp_path, "/slow.xml",
    )

    async def scenario():
        loop_responsive = asyncio.Event()

        async def probe():
            # Same loop; only fires if the loop keeps running mid-scrape.
            await asyncio.sleep(0.05)
            loop_responsive.set()

        run_id = store.create_run(interest["name"])["id"]
        probe_task = asyncio.create_task(probe())
        scrape_task = asyncio.create_task(pipeline._scrape(run_id, interest))

        # The deadline (1.0s) sits strictly between the probe delay (0.05s)
        # and the feed stall (2.0s): a TimeoutError here means the loop was
        # still being serviced while the scrape was in flight — exactly the
        # invariant under guard.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(scrape_task), timeout=1.0)
        assert loop_responsive.is_set(), (
            "event loop was blocked while a feed was being scraped"
        )
        # Bounded so a hung scrape fails red instead of wedging the suite.
        await asyncio.wait_for(scrape_task, timeout=30.0)

    asyncio.run(scenario())


def test_scrape_survives_errored_feed(slow_feed_server, store, tmp_path):
    pipeline, interest = _scrape_interest(
        slow_feed_server, store, tmp_path, "/missing.xml",
    )

    async def scenario():
        run_id = store.create_run(interest["name"])["id"]
        await pipeline._scrape(run_id, interest)

    # A failing feed is logged and swallowed, never raised.
    asyncio.run(scenario())
