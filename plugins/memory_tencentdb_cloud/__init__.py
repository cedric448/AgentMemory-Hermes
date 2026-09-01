"""memory_tencentdb_cloud — Hermes MemoryProvider for the TencentDB Agent
Memory cloud instance (v3 data plane).

Direct integration with a managed Agent Memory instance:
- prefetch(): inject L1 atomic memories + L3 core memory + L0 history hits
- sync_turn(): inline L0 conversation capture (bounded ~4s, race-free)
- tools: tdai_memory_search (L1, auto-offline when the extraction pipeline
  has never produced memories — e.g. free edition), tdai_conversation_search (L0)

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
import re
import threading
import time
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from .client import TDAMCloudClient

logger = logging.getLogger(__name__)

# MemoryProvider literal required by the discovery text scan.

# Turns whose assistant reply indicates a memory-lookup miss ("no record",
# "不确定", "I don't know" ...) carry no recallable information and — because
# they lexically overlap with the question — pollute L0 keyword search and
# push real memories out of the top-N. We skip capturing those turns and
# also filter such hits at recall time. Conservative patterns only: strong
# miss-phrases, not every occurrence of e.g. "不知道" (which can appear in
# genuinely informative answers).
_NEGATIVE_MEMORY_RE = re.compile(
    "|".join([
        r"没有(任何|查到|找到|检索到|相关)?(的)?(记录|信息|结果|印象|线索)",
        r"没有(任何)?关于",
        r"没有任何.{0,16}(记录|信息|结果|印象|线索)",
        r"查(询)?不到",
        r"检索不到",
        r"未找到(任何|相关)",
        r"无(法)?(查|检)到",
        r"(记忆|记录)中(没有|不)",
        r"不(掌握|记得)",
        r"没有关于",
        r"\bno (record|records|information|results?)\b",
        r"\bdon'?t (recall|have|know)\b",
        r"\bnot (been )?(recorded|found)\b",
        r"\b(i (do|did)n'?t|unable to) (find|recall)\b",
    ]),
    re.IGNORECASE,
)


def _is_negative_memory(text: str) -> bool:
    """True if the text looks like a memory-lookup miss (not worth keeping)."""
    if not text:
        return False
    # Match on the first 400 chars — miss replies lead with the disclaimer.
    return bool(_NEGATIVE_MEMORY_RE.search(str(text)[:400]))


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


class TencentdbCloudProvider(MemoryProvider):
    """MemoryProvider backed by the Agent Memory cloud v3 API."""

    def __init__(self):
        self._client: Optional[TDAMCloudClient] = None
        self._session_id = ""
        self._started = False
        # L1 atomic extraction productivity, probed at initialize(). On the
        # free edition the pipeline never produces anything, so the L1 tool
        # would only waste tokens and mislead the model — it stays offline
        # until the probe finds actual L1 memories.
        self._l1_available = False

    # -- properties / availability -------------------------------------------

    @property
    def name(self) -> str:
        return "memory_tencentdb_cloud"

    def is_available(self) -> bool:
        return bool(_env("TDAI_MEMORY_ENDPOINT") and _env("TDAI_MEMORY_API_KEY"))

    # -- lifecycle -------------------------------------------------------------

    def _probe_l1(self) -> bool:
        """True if the instance has any L1 atomic memories.

        Probe failure (network/5xx) counts as available — transient errors
        must not permanently hide the tool; an empty answer is the
        deterministic "extraction pipeline idle" signal.
        """
        try:
            raw = self._client._post(
                "/v3/atomic/query",
                {**self._client._isolation(), "limit": 1},
                timeout=3.5,
                retries=0,
            )
            items = self._items(raw.get("data") or {}) if raw.get("code") == 0 else None
            if items is None:
                logger.warning("tdam-cloud L1 probe failed (code=%s); assuming available", raw.get("code"))
                return True
            return len(items) > 0
        except Exception as e:
            logger.warning("tdam-cloud L1 probe error (%s); assuming available", e)
            return True

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
        self._started = True
        self._l1_available = self._probe_l1()
        logger.info(
            "memory_tencentdb_cloud initialized: endpoint=%s service=%s session=%s l1_tools=%s",
            _env("TDAI_MEMORY_ENDPOINT"), _env("TDAI_MEMORY_INSTANCE_ID"),
            self._session_id, "on" if self._l1_available else "off (no L1 memories)",
        )

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        self._session_id = new_session_id or self._session_id

    def shutdown(self) -> None:
        """No-op for uploads: sync_turn uploads inline, so by the time
        MemoryManager tears providers down (after its ≤5s executor drain)
        every completed turn is already persisted. Nothing left to flush.
        (A daemon worker + queue design was tried and caused tail-turn
        loss on hermes one-shot os._exit paths — do not reintroduce.)"""
        self._started = False

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

        def _section(key: str, fn, fmt, dedupe: bool = False) -> None:
            try:
                res = fn()
                items = self._items(res.get("data") or {}) if key != "core" else [res.get("data") or {}]
                lines = [fmt(i) for i in items if fmt(i)]
                if dedupe:
                    seen = set()
                    uniq = []
                    for line in lines:
                        k = line[:120].lower()
                        if k not in seen:
                            seen.add(k)
                            uniq.append(line)
                    lines = uniq
                if lines:
                    results[key] = "\n".join(lines)
            except Exception as e:
                logger.debug("tdam-cloud %s failed: %s", key, e)

        jobs = [
            ("core", lambda: self._client._post("/v3/core/read", self._client._isolation(), timeout=5.5, retries=0), lambda i: str(i.get("content") or ""), False),
            ("conv", lambda: self._client._post("/v3/conversation/search", {**self._client._isolation(), "query": query, "limit": 8}, timeout=5.5, retries=0), self._fmt_conv, True),
        ]
        if self._l1_available:
            jobs.insert(1, ("atomic", lambda: self._client._post("/v3/atomic/search", {**self._client._isolation(), "query": query, "limit": 8}, timeout=5.5, retries=0), self._fmt_atomic, True))
        for key, fn, fmt, dq in jobs:
            t = threading.Thread(target=_section, args=(key, fn, fmt, dq), daemon=True)
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
        """Format an L0 hit; returns "" for memory-miss entries (pollution)."""
        content = item.get("content") or ""
        if not content or _is_negative_memory(content):
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
        """Upload the turn synchronously (bounded ~4s).

        MemoryManager already invokes sync_turn on its own serialized
        background thread and waits for those tasks (≤5s) at shutdown
        before hard-exiting one-shot runs — so uploading inline here is
        race-free. A second async layer (internal queue + worker) was
        tried and caused tail-turn loss on os._exit paths.
        """
        if not self._client:
            return
        # Pollution guard: a "no record" reply carries nothing recallable and
        # lexically overlaps with the question, so capturing the turn would
        # push real memories out of keyword-search top-N later.
        if _is_negative_memory(assistant_content):
            logger.info("tdam-cloud skip capture (memory-miss reply): %.80s", assistant_content)
            return
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            self._client.conversation_add(
                [
                    {"role": "user", "content": user_content, "timestamp": now},
                    {"role": "assistant", "content": assistant_content, "timestamp": now},
                ],
                session_id=session_id or self._session_id,
                timeout=4,
                retries=0,
            )
        except Exception as e:
            logger.warning("tdam-cloud conversation_add failed: %s", e)

    # -- tools --------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        if not self._client:
            return []
        schemas: List[Dict[str, Any]] = [
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
        # L1 search is only worth advertising when the extraction pipeline
        # actually produces memories (probe at initialize).
        if self._l1_available:
            schemas.insert(0, {
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
            })
        return schemas

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
        if tool_name == "tdai_conversation_search":
            # drop memory-miss entries ("no record" replies) — pollution
            items = [i for i in items if not _is_negative_memory(i.get("content") or "")]
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
