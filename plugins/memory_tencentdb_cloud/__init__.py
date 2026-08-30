"""memory_tencentdb_cloud — Hermes MemoryProvider for the TencentDB Agent
Memory cloud instance (v3 data plane).

Direct integration with a managed Agent Memory instance:
- prefetch(): inject L1 atomic memories + L3 core memory + L0 history hits
- sync_turn(): fire-and-forget L0 conversation capture (background thread)
- tools: tdai_memory_search (L1), tdai_conversation_search (L0)

Configuration via environment variables (put in $HERMES_HOME/.env):
  TDAI_MEMORY_ENDPOINT      e.g. https://memory.ap-guangzhou.tencenttdai.com
  TDAI_MEMORY_API_KEY       instance API key
  TDAI_MEMORY_INSTANCE_ID   instance id, e.g. mem-xxxxxxxx (sent as x-tdai-service-id)
  TDAI_MEMORY_TEAM_ID       isolation team (default team-default)
  TDAI_MEMORY_AGENT_ID      isolation agent (default agent-default)
  TDAI_MEMORY_USER_ID       isolation user (default user-default)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from queue import Queue
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from .client import TDAMCloudClient

logger = logging.getLogger(__name__)

# MemoryProvider literal required by the discovery text scan.


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


class TencentdbCloudProvider(MemoryProvider):
    """MemoryProvider backed by the Agent Memory cloud v3 API."""

    def __init__(self):
        self._client: Optional[TDAMCloudClient] = None
        self._session_id = ""
        self._queue: "Queue[Optional[Dict[str, Any]]]" = Queue()
        self._worker: Optional[threading.Thread] = None
        self._started = False

    # -- properties / availability -------------------------------------------

    @property
    def name(self) -> str:
        return "memory_tencentdb_cloud"

    def is_available(self) -> bool:
        return bool(_env("TDAI_MEMORY_ENDPOINT") and _env("TDAI_MEMORY_API_KEY"))

    # -- lifecycle -------------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id or "default"
        self._client = TDAMCloudClient(
            endpoint=_env("TDAI_MEMORY_ENDPOINT"),
            api_key=_env("TDAI_MEMORY_API_KEY"),
            service_id=_env("TDAI_MEMORY_INSTANCE_ID", "default"),
            team_id=_env("TDAI_MEMORY_TEAM_ID", "team-default"),
            agent_id=_env("TDAI_MEMORY_AGENT_ID", "agent-default"),
            user_id=_env("TDAI_MEMORY_USER_ID", "user-default"),
        )
        if not self._started:
            self._worker = threading.Thread(
                target=self._capture_worker, daemon=True, name="tdam-capture"
            )
            self._worker.start()
            self._started = True
        logger.info(
            "memory_tencentdb_cloud initialized: endpoint=%s service=%s session=%s",
            _env("TDAI_MEMORY_ENDPOINT"), _env("TDAI_MEMORY_INSTANCE_ID"), self._session_id,
        )

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        self._session_id = new_session_id or self._session_id

    def shutdown(self) -> None:
        if self._started and self._worker is not None:
            self._queue.put(None)  # sentinel
            self._worker.join(timeout=2)
            self._started = False
        # Safety net: flush anything still queued synchronously (daemon
        # threads can be killed abruptly on interpreter exit).
        while True:
            try:
                payload = self._queue.get_nowait()
            except Exception:
                break
            if payload is None or not self._client:
                continue
            try:
                self._client.conversation_add(
                    payload["messages"], session_id=payload["session_id"]
                )
            except Exception as e:
                logger.warning("tdam-cloud shutdown flush failed: %s", e)

    # -- recall ------------------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall: L3 core + L1 atomic + L0 conversation hits, in parallel.

        MemoryManager imposes an 8s budget on external prefetch; the three
        cloud calls run concurrently with a hard per-call timeout so the
        total stays well under budget even when the instance is slow.
        """
        if not self._client or not query:
            return ""
        results: Dict[str, str] = {}
        threads: List[threading.Thread] = []

        def _section(key: str, fn, fmt) -> None:
            try:
                res = fn()
                items = self._items(res.get("data") or {}) if key != "core" else [res.get("data") or {}]
                lines = [fmt(i) for i in items if fmt(i)]
                if lines:
                    results[key] = "\n".join(lines)
            except Exception as e:
                logger.debug("tdam-cloud %s failed: %s", key, e)

        jobs = [
            ("core", lambda: self._client._post("/v3/core/read", self._client._isolation(), timeout=5.5, retries=0), lambda i: str(i.get("content") or "")),
            ("atomic", lambda: self._client._post("/v3/atomic/search", {**self._client._isolation(), "query": query, "limit": 8}, timeout=5.5, retries=0), self._fmt_atomic),
            ("conv", lambda: self._client._post("/v3/conversation/search", {**self._client._isolation(), "query": query, "limit": 8}, timeout=5.5, retries=0), self._fmt_conv),
        ]
        for key, fn, fmt in jobs:
            t = threading.Thread(target=_section, args=(key, fn, fmt), daemon=True)
            t.start()
            threads.append(t)
        # Single shared deadline so the whole prefetch stays within
        # MemoryManager's external-prefetch budget (8s).
        deadline = time.time() + 6.5
        for t in threads:
            t.join(max(0.05, deadline - time.time()))
        sections: List[str] = []
        if results.get("core"):
            sections.append(f"<core_memory>\n{results['core']}\n</core_memory>")
        if results.get("atomic"):
            sections.append("<long_term_memories>\n" + results["atomic"] + "\n</long_term_memories>")
        if results.get("conv"):
            sections.append("<related_conversations>\n" + results["conv"] + "\n</related_conversations>")
        if not sections:
            return ""
        return "<memory_context source=\"tencentdb-agent-memory\">\n" + "\n".join(sections) + "\n</memory_context>"

    @staticmethod
    def _items(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """conversation/search returns 'messages'; atomic/search returns 'items'."""
        return data.get("items") or data.get("messages") or []

    @staticmethod
    def _fmt_atomic(item: Dict[str, Any]) -> str:
        content = item.get("content") or ""
        if not content:
            return ""
        typ = item.get("type") or "memory"
        ts = item.get("updated_at") or item.get("created_at") or ""
        prefix = f"[{typ}" + (f" | {ts}" if ts else "") + "] "
        return prefix + str(content).replace("\n", " ")

    @staticmethod
    def _fmt_conv(item: Dict[str, Any]) -> str:
        content = item.get("content") or ""
        if not content:
            return ""
        role = item.get("role") or "msg"
        ts = item.get("timestamp") or item.get("created_at") or ""
        prefix = f"[{role}" + (f" | {ts}" if ts else "") + "] "
        return prefix + str(content).replace("\n", " ")

    # -- capture -----------------------------------------------------------------

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if not self._client:
            return
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        payload = {
            "session_id": session_id or self._session_id,
            "messages": [
                {"role": "user", "content": user_content, "timestamp": now},
                {"role": "assistant", "content": assistant_content, "timestamp": now},
            ],
        }
        self._queue.put(payload)

    def _capture_worker(self) -> None:
        while True:
            payload = self._queue.get()
            try:
                if payload is None:
                    return
                try:
                    self._client.conversation_add(
                        payload["messages"], session_id=payload["session_id"]
                    )
                except Exception as e:
                    logger.warning("tdam-cloud conversation_add failed: %s", e)
            finally:
                self._queue.task_done()

    # -- tools --------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        if not self._client:
            return []
        return [
            {
                "name": "tdai_memory_search",
                "description": (
                    "Search the user's structured long-term memories (preferences, "
                    "facts, decisions, instructions) stored in TencentDB Agent Memory."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "What to search for."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Max results (default 5)."},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "tdai_conversation_search",
                "description": (
                    "Search past raw conversation history stored in TencentDB "
                    "Agent Memory."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "What to search for."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Max results (default 5)."},
                    },
                    "required": ["query"],
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if not self._client:
            return json.dumps({"error": "provider not initialized"})
        query = str(args.get("query") or "").strip()
        try:
            limit = int(args.get("limit") or 5)
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(20, limit))
        try:
            if tool_name == "tdai_memory_search":
                res = self._client.atomic_search(query, limit=limit)
            elif tool_name == "tdai_conversation_search":
                res = self._client.conversation_search(query, limit=limit)
            else:
                return json.dumps({"error": f"unknown tool {tool_name}"})
        except Exception as e:
            return json.dumps({"error": str(e)})
        items = self._items(res.get("data") or {})
        return json.dumps({"items": items, "total": len(items)}, ensure_ascii=False)

    # -- config -------------------------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "TDAI_MEMORY_ENDPOINT", "description": "Agent Memory instance endpoint URL", "required": True, "secret": False},
            {"key": "TDAI_MEMORY_API_KEY", "description": "Agent Memory instance API key", "required": True, "secret": True},
            {"key": "TDAI_MEMORY_INSTANCE_ID", "description": "Instance id (e.g. mem-xxxx)", "required": True, "secret": False},
            {"key": "TDAI_MEMORY_TEAM_ID", "description": "Isolation team id", "required": False, "default": "team-default"},
            {"key": "TDAI_MEMORY_AGENT_ID", "description": "Isolation agent id", "required": False, "default": "agent-default"},
            {"key": "TDAI_MEMORY_USER_ID", "description": "Isolation user id", "required": False, "default": "user-default"},
        ]


# Export for register(ctx) pattern used by discovery.
provider = TencentdbCloudProvider()


def register(ctx):
    ctx.register_memory_provider(provider)
