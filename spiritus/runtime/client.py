"""Internal HTTP/SSE adapter for the pinned OpenCode server contract."""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import requests


class OpenCodeError(RuntimeError):
    """An OpenCode transport or API failure with useful response context."""

    def __init__(self, message: str, *, status: int | None = None, data: Any = None):
        super().__init__(message)
        self.status = status
        self.data = data


class OpenCodeClient:
    """Small version-pinned adapter; raw payloads do not escape this boundary."""

    def __init__(
        self,
        port: int,
        *,
        directory: Path | str | None = None,
        request_timeout: float = 10,
    ):
        self.base_url = f"http://127.0.0.1:{port}"
        self.directory = str(Path(directory).resolve()) if directory is not None else None
        self.request_timeout = request_timeout

    @property
    def _params(self) -> dict[str, str] | None:
        return {"directory": self.directory} if self.directory else None

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        request_params = dict(self._params or {})
        request_params.update(params or {})
        try:
            response = requests.request(
                method,
                self.base_url + path,
                json=body,
                params=request_params or None,
                timeout=timeout or self.request_timeout,
            )
        except requests.RequestException as exc:
            raise OpenCodeError(f"OpenCode request failed: {exc}") from exc
        if not response.ok:
            try:
                data = response.json()
            except ValueError:
                data = response.text[:500]
            message = data.get("data", {}).get("message") if isinstance(data, dict) else None
            raise OpenCodeError(
                message or f"OpenCode returned HTTP {response.status_code}",
                status=response.status_code,
                data=data,
            )
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise OpenCodeError("OpenCode returned a non-JSON response") from exc

    def agents(self) -> list[dict]:
        return self._request("GET", "/agent")

    def providers(self) -> dict:
        return self._request("GET", "/provider")

    def tool_ids(self) -> list[str]:
        # The first project-local TypeScript tool load may initialize OpenCode's
        # plugin runtime. Keep that cold-start budget separate from normal API
        # calls, which should still fail quickly.
        return self._request("GET", "/experimental/tool/ids", timeout=60)

    def tool_catalog(self, provider_id: str, model_id: str) -> list[dict]:
        return self._request(
            "GET",
            "/experimental/tool",
            params={"provider": provider_id, "model": model_id},
            timeout=60,
        )

    def commands(self) -> list[dict]:
        return self._request("GET", "/command")

    def mcp_status(self) -> dict[str, dict]:
        return self._request("GET", "/mcp", timeout=30)

    def create_session(self, body: dict | None = None) -> dict:
        return self._request("POST", "/session", body=body or {})

    def sessions(self) -> list[dict]:
        return self._request("GET", "/session")

    def session(self, session_id: str) -> dict:
        return self._request("GET", f"/session/{session_id}")

    def delete_session(self, session_id: str) -> Any:
        return self._request("DELETE", f"/session/{session_id}")

    def children(self, session_id: str) -> list[dict]:
        return self._request("GET", f"/session/{session_id}/children")

    def abort(self, session_id: str) -> bool:
        return self._request("POST", f"/session/{session_id}/abort")

    def messages(self, session_id: str) -> list[dict]:
        return self._request("GET", f"/session/{session_id}/message")

    def prompt(self, session_id: str, body: dict) -> dict:
        return self._request(
            "POST", f"/session/{session_id}/message", body=body, timeout=120
        )

    def prompt_async(self, session_id: str, body: dict) -> None:
        self._request("POST", f"/session/{session_id}/prompt_async", body=body)

    def command(self, session_id: str, body: dict) -> dict:
        return self._request(
            "POST",
            f"/session/{session_id}/command",
            body=body,
            timeout=120,
        )

    def reply_permission(
        self,
        request_id: str,
        reply: str,
        *,
        message: str | None = None,
    ) -> None:
        body = {"reply": reply}
        if message:
            body["message"] = message
        self._request("POST", f"/permission/{request_id}/reply", body=body)

    def events(
        self,
        *,
        ready: threading.Event | None = None,
        stop: threading.Event | None = None,
    ) -> Iterator[dict]:
        """Yield decoded SSE envelopes until the caller stops iteration."""
        try:
            with requests.get(
                self.base_url + "/event",
                params=self._params,
                stream=True,
                timeout=(self.request_timeout, 300),
            ) as response:
                response.raise_for_status()
                if ready is not None:
                    ready.set()
                for line in response.iter_lines(chunk_size=1, decode_unicode=True):
                    if stop is not None and stop.is_set():
                        return
                    if not line or not line.startswith("data:"):
                        continue
                    yield json.loads(line[5:].strip())
        except (requests.RequestException, ValueError) as exc:
            if ready is not None:
                ready.set()
            raise OpenCodeError(f"OpenCode event stream failed: {exc}") from exc
