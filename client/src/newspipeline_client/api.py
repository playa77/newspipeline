"""HTTP layer for the newspipeline server JSON API (httpx, Qt-free).

One method per endpoint; every non-2xx response raises ApiError carrying
the server's structured ``{"error": ...}`` message. No secrets ever flow
through this module (G1): the API surface has no secret fields by design.
"""

# version: 0.6.0

from __future__ import annotations

from typing import Any

import httpx
from urllib.parse import quote


class ApiError(RuntimeError):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class ApiClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self._client = httpx.Client(base_url=base_url, timeout=timeout)
        self.recipient_id = ""
        self._owns_transport = True

    def close(self) -> None:
        if self._owns_transport:
            self._client.close()

    def for_recipient(self, recipient_id: str) -> "ApiClient":
        scoped = object.__new__(ApiClient)
        scoped._client = self._client
        scoped.recipient_id = recipient_id
        scoped._owns_transport = False
        return scoped

    def _interest_path(self, name: str = "", suffix: str = "") -> str:
        prefix = f"/api/recipients/{quote(self.recipient_id, safe='')}/interests" if self.recipient_id else "/api/interests"
        return prefix + (f"/{quote(name, safe='')}" if name else "") + suffix

    def list_recipients(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/recipients")["recipients"]

    def create_recipient(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/recipients", payload)

    def update_recipient(self, rid: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"/api/recipients/{quote(rid, safe='')}", payload)

    def delete_recipient(self, rid: str) -> None:
        self._request("DELETE", f"/api/recipients/{quote(rid, safe='')}")

    def get_recipient_defaults(self) -> dict[str, Any]:
        return self._request("GET", "/api/recipient-defaults")

    def put_recipient_defaults(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", "/api/recipient-defaults", payload)

    def get_interest(self, name: str) -> dict[str, Any]:
        return self._request("GET", self._interest_path(name))

    def get_deliveries(self) -> list[dict[str, Any]]:
        return self._request("GET", f"/api/deliveries?recipient={quote(self.recipient_id, safe='')}")["deliveries"]

    def retry_delivery(self, run_id: int) -> None:
        self._request("POST", f"/api/deliveries/{run_id}/retry?recipient={quote(self.recipient_id, safe='')}", {})

    # ---- plumbing ----------------------------------------------------------

    def _request(
        self, method: str, path: str, json_body: Any = None,
    ) -> Any:
        try:
            resp = self._client.request(method, path, json=json_body)
        except httpx.HTTPError as exc:
            raise ApiError(f"Connection to the server failed: {exc}") from exc
        if resp.status_code >= 400:
            try:
                message = resp.json().get("error", resp.text)
            except ValueError:
                message = resp.text
            raise ApiError(f"{message} (HTTP {resp.status_code})", resp.status_code)
        if not resp.content:
            return None
        return resp.json()

    # ---- settings / status ---------------------------------------------------

    def get_settings(self) -> dict[str, Any]:
        return self._request("GET", "/api/settings")

    def put_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", "/api/settings", patch)

    def get_status(self, limit: int = 10) -> dict[str, Any]:
        return self._request("GET", f"/api/recipients/{quote(self.recipient_id, safe='')}/status" if self.recipient_id else f"/api/status?limit={limit}")

    # ---- interests -----------------------------------------------------------

    def list_interests(self) -> list[dict[str, Any]]:
        return self._request("GET", self._interest_path())["interests"]

    def create_interest(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", self._interest_path(), payload)["interest"]

    def update_interest(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", self._interest_path(name), payload)["interest"]

    def delete_interest(self, name: str) -> None:
        self._request("DELETE", self._interest_path(name))

    def rename_interest(self, name: str, new_name: str) -> dict[str, Any]:
        return self._request("POST", self._interest_path(name, "/rename"), {"to": new_name})

    def trigger_interest(self, name: str) -> None:
        self._request("POST", self._interest_path(name, "/trigger"), {})

    # ---- feeds -----------------------------------------------------------------

    def add_feed(self, name: str, feed: dict[str, Any]) -> list[dict[str, Any]]:
        return self._request("POST", self._interest_path(name, "/feeds"), feed)["feeds"]

    def delete_feed(self, name: str, index: int) -> list[dict[str, Any]]:
        return self._request("DELETE", self._interest_path(name, f"/feeds/{index}"))["feeds"]
