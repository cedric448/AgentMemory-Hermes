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

import glob
import json
import logging
import os
import re
import threading
import time
import urllib.error
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
        self._l1_probed = False
        self._hermes_home = ""
        self._spool_dir: Optional[str] = None

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
        self._hermes_home = str(kwargs.get("hermes_home") or "") or os.path.expanduser("~/.hermes")
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
        self._l1_probed = True
        self._replay_spool(budget_s=12.0, max_items=8)
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

        def _section(key: str, fn, fmt, dedupe: bool = False,
                     assistant_first: bool = False, max_lines: int = 0) -> None:
            try:
                res = fn()
                items = self._items(res.get("data") or {}) if key != "core" else [res.get("data") or {}]
                if assistant_first:
                    # Lexical search ranks user questions (which echo the
                    # query verbatim) above assistant answers that carry the
                    # actual facts. Injecting assistant hits first raises the
                    # fact density of the injected context.
                    tmp = []
                    for i in items:
                        line = fmt(i)
                        if line:
                            role = (i.get("role") or "") if isinstance(i, dict) else ""
                            tmp.append((line, role))
                    asst = [l for l, _role in tmp if _role == "assistant"]
                    other = [l for l, _role in tmp if _role != "assistant"]
                    lines = asst + other
                else:
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
                if max_lines and len(lines) > max_lines:
                    lines = lines[:max_lines]
                if lines:
                    results[key] = "\n".join(lines)
            except Exception as e:
                logger.debug("tdam-cloud %s failed: %s", key, e, exc_info=True)

        jobs = [
            ("core", lambda: self._client._post("/v3/core/read", self._client._isolation(), timeout=5.5, retries=0), lambda i: str(i.get("content") or ""), False, False),
            ("conv", lambda: self._client._post("/v3/conversation/search", {**self._client._isolation(), "query": query, "limit": 30}, timeout=5.5, retries=0), self._fmt_conv, True, True, 10),
        ]
        if self._l1_available:
            jobs.insert(1, ("atomic", lambda: self._client._post("/v3/atomic/search", {**self._client._isolation(), "query": query, "limit": 8}, timeout=5.5, retries=0), self._fmt_atomic, True, False))
        for job in jobs:
            key, fn, fmt, dq, af = job[0], job[1], job[2], job[3], job[4]
            t = threading.Thread(target=_section, args=job, daemon=True)
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

    _SPOOL_MAX_FILES = 200

    def _get_spool_dir(self) -> str:
        if not self._spool_dir:
            self._spool_dir = os.path.join(self._hermes_home, "tdam-cloud-spool")
            os.makedirs(self._spool_dir, exist_ok=True)
        return self._spool_dir

    def _spool(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """Persist a failed turn for replay at next initialize (best effort)."""
        try:
            d = self._get_spool_dir()
            name = f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}-{int(time.time()*1000)%1000000}.json"
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                json.dump({"session_id": session_id, "messages": messages}, f, ensure_ascii=False)
            # cap growth: if the instance is down for days, drop the oldest
            try:
                files = sorted(glob.glob(os.path.join(d, "*.json")))
                for old in files[: max(0, len(files) - self._SPOOL_MAX_FILES)]:
                    os.remove(old)
                if len(files) > self._SPOOL_MAX_FILES:
                    logger.warning("tdam-cloud spool overflow: dropped %d oldest turns", len(files) - self._SPOOL_MAX_FILES)
            except Exception:
                pass
            logger.warning("tdam-cloud turn spooled for retry: %s", name)
        except Exception as e:
            logger.error("tdam-cloud spool write failed; turn lost: %s", e)

    def _replay_spool(self, budget_s: float, max_items: int) -> None:
        """Best-effort replay of spooled turns. Bounded: stops on the first
        failure (instance likely still down), on budget, or on max_items.
        Permanent 4xx failures are dropped (they can never succeed)."""
        try:
            files = sorted(glob.glob(os.path.join(self._get_spool_dir(), "*.json")))
        except Exception:
            return
        if not files:
            return
        replayed = dropped = 0
        deadline = time.time() + budget_s
        for path in files[:max_items]:
            if time.time() >= deadline:
                return
            try:
                with open(path, encoding="utf-8") as f:
                    entry = json.load(f)
            except Exception:
                os.remove(path)  # corrupt beyond repair
                dropped += 1
                continue
            try:
                self._client.conversation_add(
                    entry["messages"], session_id=entry["session_id"], timeout=4, retries=0
                )
                os.remove(path)
                replayed += 1
            except urllib.error.HTTPError as e:
                if 400 <= e.code < 500 and e.code != 429:
                    logger.warning("tdam-cloud spool replay dropped (HTTP %d, permanent): %s", e.code, path)
                    os.remove(path)
                    dropped += 1
                else:
                    logger.warning("tdam-cloud spool replay paused (HTTP %d); will retry next session", e.code)
                    return
            except Exception as e:
                logger.warning("tdam-cloud spool replay paused (%s); will retry next session", e)
                return
        if replayed or dropped:
            logger.info("tdam-cloud spool replay: %d replayed, %d dropped, %d remain",
                        replayed, dropped, len(files) - replayed - dropped)

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
        Failed uploads (network/5xx/timeout/429) are spooled to
        $HERMES_HOME/tdam-cloud-spool/ and replayed at next initialize.
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
        msgs = [
            {"role": "user", "content": user_content, "timestamp": now},
            {"role": "assistant", "content": assistant_content, "timestamp": now},
        ]
        try:
            self._client.conversation_add(
                msgs,
                session_id=session_id or self._session_id,
                timeout=4,
                retries=0,
            )
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500 and e.code != 429:
                logger.warning("tdam-cloud conversation_add permanent failure (HTTP %d); turn dropped", e.code)
                return
            logger.warning("tdam-cloud conversation_add failed (HTTP %d); spooling", e.code)
            self._spool(session_id or self._session_id, msgs)
        except Exception as e:
            logger.warning("tdam-cloud conversation_add failed (%s); spooling", e)
            self._spool(session_id or self._session_id, msgs)

    # -- tools --------------------------------------------------------------------

    def _ensure_client(self) -> None:
        """Build the client from env if not initialized yet.

        Hermes registers provider tools BEFORE calling initialize()
        (observed: 'registered (0 tools)' in agent.log), so
        get_tool_schemas() must not depend on initialize() having run —
        otherwise the search tools are never advertised at all.
        """
        if self._client is None and self.is_available():
            self._client = TDAMCloudClient(
                endpoint=_env("TDAI_MEMORY_ENDPOINT"),
                api_key=_env("TDAI_MEMORY_API_KEY"),
                service_id=_env("TDAI_MEMORY_INSTANCE_ID", "default"),
                team_id=_env("TDAI_MEMORY_TEAM_ID", "team-default"),
                agent_id=_env("TDAI_MEMORY_AGENT_ID", "agent-default"),
                user_id=_env("TDAI_MEMORY_USER_ID", "user-default"),
            )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        self._ensure_client()
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
        # actually produces memories (probe at initialize). Before the probe
        # has run (registration happens before initialize), advertise
        # optimistically — handle_tool_call degrades to an empty result.
        if self._l1_available or not self._l1_probed:
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
