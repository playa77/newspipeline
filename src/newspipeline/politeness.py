"""Politeness layer for every outbound scraper request.

Scope (see DECISIONS.md, decision R-acc accompanying this module):
- P1  shared minimum-gap schedule: every request is held back until a fixed
      minimum gap (REQUEST_MIN_GAP_SECONDS) has elapsed since the PREVIOUS
      REQUEST BEGAN. The first request of a scrape proceeds immediately.
      Retry waits count toward the same spacing.
- P2  transient failures (standard rate-limit/availability statuses plus
      network-level errors) retry a bounded number of times with growing,
      exponentially spaced delays. A Retry-After header is honored in place
      of the computed delay, capped at RETRY_AFTER_CAP_SECONDS so a hostile
      or misconfigured server cannot stall a run for minutes. When the
      retry budget is exhausted, the failure surfaces normally into the
      scraper's preexisting log-and-move-on behavior for the next feed.
- P3  browser identity: every request carries a conventional browser
      User-Agent. A source rejecting browser clients (observed in the wild:
      a nonstandard 419 to browser agents while serving the default
      identity freely) gets exactly ONE fallback attempt with the plain
      default client identity. A source known to accept the browser
      identity is never probed again with the fallback. Worst case per URL:
      1 request (healthy), 1 + MAX_TRANSIENT_RETRIES (transient path), or
      2 (fallback path: browser 419 + one plain attempt).
"""

# version: 0.1.0

from __future__ import annotations

import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)

# Conventional desktop-browser identity for outbound requests.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# --- production tunables (overridable in tests) ------------------------------
REQUEST_MIN_GAP_SECONDS = 2.0
MAX_TRANSIENT_RETRIES = 3
TRANSIENT_BASE_DELAY_SECONDS = 1.0
TRANSIENT_MAX_DELAY_SECONDS = 30.0
RETRY_AFTER_CAP_SECONDS = 60.0

# Transient class: rate-limiting and availability statuses.
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})

# A browser rejection: served the plain identity freely in the wild while
# answering browser agents with this status. Only this triggers the single
# plain-identity fallback (P3) — NOT 403/406/429, which stay in the normal
# transient/retry path.
_BROWSER_REJECTED_STATUS = frozenset({419})


def _is_transient_status(status: int) -> bool:
    return status in _TRANSIENT_STATUSES


class PolitenessLayer:
    """One shared throttle + bounded-retry + identity policy per scrape run.

    Instantiated once per Pipeline (and therefore per interest run); the
    minimum-gap schedule is shared by the feed fetch and every article
    fetch. ``serve``-driven concurrent interests each hold their own layer,
    so the gap is enforced within a run — the sequential scraper guarantees
    the same property when runs are sequential.
    """

    def __init__(
        self,
        *,
        min_gap: float = REQUEST_MIN_GAP_SECONDS,
        max_retries: int = MAX_TRANSIENT_RETRIES,
        base_delay: float = TRANSIENT_BASE_DELAY_SECONDS,
        max_delay: float = TRANSIENT_MAX_DELAY_SECONDS,
        retry_after_cap: float = RETRY_AFTER_CAP_SECONDS,
    ) -> None:
        self.min_gap = min_gap
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.retry_after_cap = retry_after_cap
        self._next_allowed = 0.0  # monotonic clock
        self._plain_fallback_hosts: set[str] = set()

    # -- slot reservation (P1) ------------------------------------------------

    def _reserve_slot(self) -> float:
        """Reserve the next request-start slot; return the wait for it.

        The gap counts from when the previous request BEGAN (not from its
        response), so a slow response never extends the schedule.
        """
        now = time.monotonic()
        start = max(now, self._next_allowed)
        self._next_allowed = start + self.min_gap
        return start - now

    def _retry_delay(self, attempt: int) -> float:
        """Exponentially growing delay, capped (P2). ``attempt`` starts at 0."""
        return min(self.base_delay * (2 ** attempt), self.max_delay)

    @staticmethod
    def _extract_retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None  # HTTP-date form: rare on RSS hosts; ignore

    # -- core ------------------------------------------------------------------

    async def _attempt(self, client: httpx.AsyncClient, url: str, *, plain: bool) -> httpx.Response:
        wait = self._reserve_slot()
        if wait > 0:
            await asyncio.sleep(wait)
        headers = {} if plain else {"User-Agent": USER_AGENT}
        return await client.get(url, headers=headers)

    async def _run_transient_loop(self, client: httpx.AsyncClient, url: str, *, plain: bool) -> httpx.Response:
        last: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._attempt(client, url, plain=plain)
            except httpx.TransportError:
                # Network-level errors belong to the transient class (P2).
                if attempt >= self.max_retries:
                    raise  # budget exhausted: surface into the caller's log-and-skip path
                await asyncio.sleep(min(self._retry_delay(attempt), self.max_delay))
                continue
            if not _is_transient_status(resp.status_code):
                return resp
            last = resp
            if attempt >= self.max_retries:
                break  # budget exhausted: surface the last response
            retry_after = self._extract_retry_after(resp)
            if retry_after is not None:
                await asyncio.sleep(min(retry_after, self.retry_after_cap))
            else:
                await asyncio.sleep(min(self._retry_delay(attempt), self.max_delay))
        assert last is not None  # loop only exits after at least one transient response
        return last

    async def request(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        """GET ``url`` under the shared gap, bounded-retry, and identity policies."""
        host = httpx.URL(url).host or url
        if host in self._plain_fallback_hosts:
            # P3: a source known to reject browser agents is served the
            # plain identity from the first attempt — no repeat probing.
            return await self._run_transient_loop(client, url, plain=True)

        resp = await self._run_transient_loop(client, url, plain=False)
        if resp.status_code in _BROWSER_REJECTED_STATUS:
            logger.info("Browser UA rejected (%d) — plain-identity fallback: %s", resp.status_code, url)
            self._plain_fallback_hosts.add(host)  # never probed again with the browser identity
            # Exactly one fallback attempt; its outcome surfaces unmodified —
            # no transient-retry loop across identities (P3).
            return await self._attempt(client, url, plain=True)
        return resp
