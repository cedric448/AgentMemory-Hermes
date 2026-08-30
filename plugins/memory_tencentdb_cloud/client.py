"""HTTP client for TencentDB Agent Memory cloud instance (v3 data plane).

Thin port of the official memory_tencentdb Gateway client, pointed directly
at the managed cloud endpoint. All endpoints are /v3/* with
team_id/agent_id/user_id isolation and Bearer + x-tdai-service-id auth.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 6  # seconds


class TDAMCloudClient:
    """HTTP client for the Agent Memory cloud v3 data plane. Thread-safe."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        service_id: str,
        team_id: str = "team-default",
        agent_id: str = "agent-default",
        user_id: str = "user-default",
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self._endpoint = (endpoint or "").rstrip("/")
        self._api_key = (api_key or "").strip()
        self._service_id = (service_id or "").strip()
        self._team_id = team_id or "team-default"
        self._agent_id = agent_id or "agent-default"
        self._user_id = user_id or "user-default"
        self._timeout = timeout

    # -- low level ------------------------------------------------------------

    def _headers(self, content_type: bool) -> Dict[str, str]:
        h: Dict[str, str] = {}
        if content_type:
            h["Content-Type"] = "application/json"
        h["Authorization"] = f"Bearer {self._api_key}"
        h["x-tdai-service-id"] = self._service_id
        return h

    def _post(self, path: str, body: Dict[str, Any], timeout: Optional[int] = None, retries: int = 1) -> Dict[str, Any]:
        url = f"{self._endpoint}{path}"
        data = json.dumps(body).encode("utf-8")
        last_err: Optional[Exception] = None
        # One retry for transient cloud errors (5xx / network) — the managed
        # instance occasionally returns 522 under load.
        for attempt in range(retries + 1):
            req = urllib.request.Request(
                url, data=data, headers=self._headers(True), method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout or self._timeout) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                if raw.get("code", -1) != 0:
                    logger.warning("tdam-cloud %s code=%s: %s", path, raw.get("code"), raw.get("message"))
                return raw
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", errors="replace")[:500]
                except Exception:
                    pass
                logger.warning("tdam-cloud %s HTTP %d: %s", path, e.code, detail)
                if e.code >= 500:
                    last_err = e
                    time.sleep(0.5)
                    continue
                raise
            except Exception as e:
                logger.debug("tdam-cloud %s failed: %s", path, e)
                last_err = e
                time.sleep(0.5)
        raise last_err  # type: ignore[misc]

    def _isolation(self) -> Dict[str, str]:
        return {
            "team_id": self._team_id,
            "agent_id": self._agent_id,
            "user_id": self._user_id,
        }

    # -- v3 data plane --------------------------------------------------------

    def conversation_add(
        self,
        messages: List[Dict[str, Any]],
        *,
        session_id: str = "",
    ) -> Dict[str, Any]:
        """L0: append conversation messages."""
        body = {
            **self._isolation(),
            "session_id": session_id or "default",
            "messages": messages,
        }
        return self._post("/v3/conversation/add", body)

    def conversation_search(self, query: str, *, limit: int = 5, session_id: str = "") -> Dict[str, Any]:
        """L0: search raw conversation history."""
        body = {**self._isolation(), "query": query, "limit": limit}
        if session_id:
            body["session_id"] = session_id
        return self._post("/v3/conversation/search", body)

    def atomic_search(self, query: str, *, limit: int = 5, type_filter: str = "") -> Dict[str, Any]:
        """L1: search structured long-term memories."""
        body = {**self._isolation(), "query": query, "limit": limit}
        if type_filter:
            body["type"] = type_filter
        return self._post("/v3/atomic/search", body)

    def core_read(self) -> Dict[str, Any]:
        """L3: read persona / core memory."""
        return self._post("/v3/core/read", dict(self._isolation()))
