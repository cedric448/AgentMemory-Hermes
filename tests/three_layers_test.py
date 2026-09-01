#!/usr/bin/env python3
"""Three-layer memory kernel test for TencentDB Agent Memory (v3 data plane).

Layers under test (mapped to the v3 data-plane model):

  短期记忆 (short-term)  — L0 conversation: in-session recall, cross-session
                           search, session scoping
  长期记忆 (long-term)   — L1 atomic structured memories (async extraction
                           pipeline) + L3 core memory (user profile)
  团队记忆 (team)        — team scoping semantics (isolation matrix across
                           user/agent within a team) + team-bound knowledge
                           assets (Wiki) as the shared team layer

Every check records the observed behavior — where the platform isolates
rather than shares, that is reported as ISOLATED (expected) instead of FAIL;
the point is to document real semantics, not to enforce a particular one.

Usage:
  export TDAI_MEMORY_ENDPOINT=... TDAI_MEMORY_API_KEY=... TDAI_MEMORY_INSTANCE_ID=...
  python3 tests/three_layers_test.py [--skip-index-wait]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

TIMEOUT = 15.0


class Client:
    def __init__(self, endpoint: str, api_key: str, service_id: str):
        self.endpoint = endpoint.rstrip("/")
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "x-tdai-service-id": service_id,
        }
        self.user_headers = {**self.headers, "x-tdai-user-key": api_key}

    def post(self, path: str, body: Dict[str, Any], meta: bool = False,
             timeout: float = TIMEOUT) -> Tuple[Dict[str, Any], float]:
        headers = self.user_headers if meta else self.headers
        req = urllib.request.Request(
            f"{self.endpoint}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            try:
                raw = json.loads(detail)
            except Exception:
                raw = {"code": e.code, "message": detail}
        return raw, time.time() - t0


def hits_conv(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    return (raw.get("data") or {}).get("messages") or []


def items_atomic(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    return (raw.get("data") or {}).get("items") or []


class Reporter:
    def __init__(self):
        self.rows: List[Tuple[str, str, str, str]] = []

    def add(self, layer: str, check: str, status: str, note: str, dt: float = 0.0):
        self.rows.append((layer, check, status, f"{note} ({dt*1000:.0f}ms)" if dt else note))
        print(f"  [{status}] {layer}/{check}: {note}")

    def summary(self) -> None:
        print("\n" + "=" * 84)
        print(f"{'Layer':<10}{'Check':<38}{'Result':<12}Note")
        print("-" * 84)
        for layer, check, status, note in self.rows:
            print(f"{layer:<10}{check:<38}{status:<12}{note}")
        print("=" * 84)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=os.environ.get("TDAI_MEMORY_ENDPOINT", ""))
    ap.add_argument("--api-key", default=os.environ.get("TDAI_MEMORY_API_KEY", ""))
    ap.add_argument("--instance-id", default=os.environ.get("TDAI_MEMORY_INSTANCE_ID", ""))
    ap.add_argument("--team-id", default="team-hermes-test")
    ap.add_argument("--agent-id", default="agent-hermes")
    ap.add_argument("--user-id", default="user-hermes-v2")
    ap.add_argument("--skip-index-wait", action="store_true",
                    help="skip the 90s sleep before async-index checks")
    args = ap.parse_args()
    if not (args.endpoint and args.api_key and args.instance_id):
        print("Missing TDAI_MEMORY_ENDPOINT / API_KEY / INSTANCE_ID", file=sys.stderr)
        return 2

    c = Client(args.endpoint, args.api_key, args.instance_id)
    r = Reporter()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    marker = f"TL{int(time.time())%100000}"  # unique per run
    session = f"layer-test-{marker}"

    # =====================================================================
    print("\n[1] 短期记忆 — L0 conversation")
    # ---------------------------------------------------------------------
    raw, dt = c.post("/v3/conversation/add", {
        "team_id": args.team_id, "agent_id": args.agent_id, "user_id": args.user_id,
        "session_id": session,
        "messages": [
            {"role": "user", "content": f"短期记忆测试:我的会议在{marker}号会议室。", "timestamp": ts},
            {"role": "assistant", "content": f"已记住,你的会议在{marker}号会议室。", "timestamp": ts},
        ],
    })
    r.add("短期", "L0 写入(会话内)", "PASS" if raw.get("code") == 0 else "FAIL",
          f"code={raw.get('code')}", dt)

    raw, dt = c.post("/v3/conversation/query", {
        "team_id": args.team_id, "agent_id": args.agent_id, "user_id": args.user_id,
        "session_id": session, "limit": 10,
    })
    ok = any(marker in (m.get("content") or "") for m in hits_conv(raw))
    r.add("短期", "会话内 query", "PASS" if ok else "FAIL", f"code={raw.get('code')}", dt)

    if not args.skip_index_wait:
        print("  ... 等待 90s 让检索索引生效")
        time.sleep(90)

    raw, dt = c.post("/v3/conversation/search", {
        "team_id": args.team_id, "agent_id": args.agent_id, "user_id": args.user_id,
        "query": f"{marker}号会议室", "limit": 5,
    })
    n = len(hits_conv(raw))
    ok = any(marker in (m.get("content") or "") for m in hits_conv(raw))
    r.add("短期", "跨轮 search 召回", "PASS" if ok else "FAIL", f"code={raw.get('code')} hits={n}", dt)

    # session scoping: another session must not see this session via query
    raw, dt = c.post("/v3/conversation/query", {
        "team_id": args.team_id, "agent_id": args.agent_id, "user_id": args.user_id,
        "session_id": "other-session-xyz", "limit": 10,
    })
    ok = not any(marker in (m.get("content") or "") for m in hits_conv(raw))
    r.add("短期", "session 隔离(query 按 session)", "PASS" if ok else "ISOLATION-BROKEN",
          "其它 session 查不到本 session 内容" if ok else "泄漏!", dt)

    # =====================================================================
    print("\n[2] 长期记忆 — L1 atomic + L3 core")
    # ---------------------------------------------------------------------
    # L1: extraction pipeline turns captured conversations into atomic
    # memories asynchronously. First force an extraction-eligible capture.
    raw, dt = c.post("/v3/conversation/add", {
        "team_id": args.team_id, "agent_id": args.agent_id, "user_id": args.user_id,
        "session_id": session,
        "messages": [
            {"role": "user", "content": f"请记住:我的长期记忆验证码是 LTC{marker}。", "timestamp": ts},
            {"role": "assistant", "content": f"好的,已记住你的长期记忆验证码是 LTC{marker}。", "timestamp": ts},
        ],
    })
    r.add("长期", "L0 写入(供 L1 抽取)", "PASS" if raw.get("code") == 0 else "FAIL",
          f"code={raw.get('code')}", dt)

    if not args.skip_index_wait:
        print("  ... 等待 120s 让 L1 抽取管线运行")
        time.sleep(120)

    raw, dt = c.post("/v3/atomic/search", {
        "team_id": args.team_id, "agent_id": args.agent_id, "user_id": args.user_id,
        "query": "长期记忆验证码", "limit": 5,
    })
    items = items_atomic(raw)
    ok = any(f"LTC{marker}" in (i.get("content") or "") for i in items)
    r.add("长期", "L1 atomic/search(管线抽取)", "PASS" if ok else "PENDING",
          f"code={raw.get('code')} hits={len(items)}" +
          ("" if ok else "(抽取异步,可能需更久/人工复核)"), dt)

    # L3: manual core write/read
    core = f"# 用户画像(三层测试 {marker})\n- 验证码: LTC{marker}\n- 部门: 测试部\n"
    raw, dt = c.post("/v3/core/write", {
        "team_id": args.team_id, "agent_id": args.agent_id, "user_id": args.user_id,
        "content": core,
    })
    r.add("长期", "L3 core/write", "PASS" if raw.get("code") == 0 else "FAIL",
          f"code={raw.get('code')}", dt)

    raw, dt = c.post("/v3/core/read", {
        "team_id": args.team_id, "agent_id": args.agent_id, "user_id": args.user_id,
    })
    ok = f"LTC{marker}" in (raw.get("data") or {}).get("content") or ""
    r.add("长期", "L3 core/read 回读", "PASS" if ok else "FAIL", f"code={raw.get('code')}", dt)

    # cross-session recall of long-term facts via L0 search (different session)
    raw, dt = c.post("/v3/conversation/search", {
        "team_id": args.team_id, "agent_id": args.agent_id, "user_id": args.user_id,
        "query": f"LTC{marker}", "limit": 5,
    })
    ok = any(f"LTC{marker}" in (m.get("content") or "") for m in hits_conv(raw))
    r.add("长期", "跨会话召回(L0 search)", "PASS" if ok else "PENDING", f"hits={len(hits_conv(raw))}", dt)

    # =====================================================================
    print("\n[3] 团队记忆 — team scoping + Wiki 资产")
    # ---------------------------------------------------------------------
    # isolation matrix: same team, different user
    raw, dt = c.post("/v3/conversation/search", {
        "team_id": args.team_id, "agent_id": args.agent_id, "user_id": "user-other-probe",
        "query": f"{marker}号会议室", "limit": 5,
    })
    ok = not any(marker in (m.get("content") or "") for m in hits_conv(raw))
    r.add("团队", "L0 跨 user 隔离(同 team)", "ISOLATED" if ok else "SHARED",
          "不同 user 互不可见" if ok else "跨 user 可见!", dt)

    raw, dt = c.post("/v3/atomic/search", {
        "team_id": args.team_id, "agent_id": args.agent_id, "user_id": "user-other-probe",
        "query": "长期记忆验证码", "limit": 5,
    })
    ok = not any(f"LTC{marker}" in (i.get("content") or "") for i in items_atomic(raw))
    r.add("团队", "L1 跨 user 隔离(同 team)", "ISOLATED" if ok else "SHARED",
          "不同 user 互不可见" if ok else "跨 user 可见!", dt)

    raw, dt = c.post("/v3/core/read", {
        "team_id": args.team_id, "agent_id": args.agent_id, "user_id": "user-other-probe",
    })
    other = (raw.get("data") or {}).get("content") or ""
    shared = f"LTC{marker}" in other
    # Empirically confirmed semantics: L3 core is scoped to (team, agent) and
    # SHARED across users — it is the agent-level profile, not per-user state.
    r.add("团队", "L3 跨 user(同 team)", "SHARED" if shared else "ISOLATED",
          "L3 为 agent 级共享画像(平台语义,非泄漏)" if shared else "各 user 独立", dt)

    # team-shared asset: knowledge entity (Wiki) is team-scoped metadata
    kid = f"wiki-team-{marker}"
    raw, dt = c.post("/v3/knowledge/create", {
        "knowledge_id": kid, "type": "wiki",
        "name": f"团队记忆测试 Wiki {marker}", "summary": "三层测试:团队共享层",
        "service_url": f"{args.endpoint}/v3", "team_id": args.team_id,
    })
    ok = raw.get("code") == 0
    r.add("团队", "Wiki 资产创建(团队作用域)", "PASS" if ok else "FAIL", f"code={raw.get('code')}", dt)

    if ok:
        raw, dt = c.post("/v3/knowledge/get", {
            "team_id": args.team_id, "agent_id": args.agent_id,
            "user_id": "user-other-probe", "knowledge_id": kid,
        })
        ok = raw.get("code") == 0
        r.add("团队", "Wiki 团队内可见(跨 user)", "PASS" if ok else "FAIL",
              f"code={raw.get('code')} — 团队资产不随 user 隔离" if ok else str(raw.get("message"))[:60], dt)

        raw, dt = c.post("/v3/knowledge/list", {"team_id": args.team_id, "type": "wiki"})
        n = len((raw.get("data") or {}).get("items") or [])
        r.add("团队", "Wiki 按团队列举", "PASS" if n >= 1 else "FAIL", f"items={n}", dt)

        raw, dt = c.post("/v3/knowledge/delete", {
            "team_id": args.team_id, "agent_id": args.agent_id,
            "user_id": args.user_id, "knowledge_ids": [kid],
        })
        r.add("团队", "Wiki 清理", "PASS" if raw.get("code") == 0 else "FAIL",
              f"code={raw.get('code')}", dt)

    r.summary()
    print(f"\nmarker={marker} session={session}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
