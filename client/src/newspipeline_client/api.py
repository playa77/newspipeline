"""HTTP layer for the newspipeline server JSON API (httpx, Qt-free).

One method per endpoint; every non-2xx response raises ApiError carrying
the server's structured ``{"error": ...}`` message. No secrets ever flow
through this module (G1): the API surface has no secret fields by design.
"""

# version: 0.3.0

from __future__ import annotations

from typing import Any

import httpx


class ApiError(RuntimeError):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class ApiClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

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
        return self._request("GET", f"/api/status?limit={limit}")

    # ---- interests -----------------------------------------------------------

    def list_interests(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/interests")["interests"]

    def create_interest(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/interests", payload)["interest"]

    def update_interest(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"/api/interests/{name}", payload)["interest"]

    def delete_interest(self, name: str) -> None:
        self._request("DELETE", f"/api/interests/{name}")

    def rename_interest(self, name: str, new_name: str) -> dict[str, Any]:
        return self._request("POST", f"/api/interests/{name}/rename", {"to": new_name})

    def trigger_interest(self, name: str) -> None:
        self._request("POST", f"/api/interests/{name}/trigger", {})

    # ---- feeds -----------------------------------------------------------------

    def add_feed(self, name: str, feed: dict[str, Any]) -> list[dict[str, Any]]:
        return self._request("POST", f"/api/interests/{name}/feeds", feed)["feeds"]

    def delete_feed(self, name: str, index: int) -> list[dict[str, Any]]:
        return self._request("DELETE", f"/api/interests/{name}/feeds/{index}")["feeds"]
