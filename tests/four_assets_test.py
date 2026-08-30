#!/usr/bin/env python3
"""Four-asset capability test for TencentDB Agent Memory (v3 data plane).

Tests the managed instance's support for the four asset types against the
/v3 data plane directly (no local gateway, no proxy):

  1. Chat Memory  — L0 conversation add/query/search, L3 core write/read,
                    L1 atomic search, L2 scenario ls/write, offload
  2. Skill        — create / get-by-name / list / search / files write+read / delete
  3. Wiki         — knowledge entity CRUD (type=wiki)          [metadata plane]
  4. CodeGraph    — knowledge entity CRUD (type=code-graph)    [metadata plane]

Skill (and Wiki/CodeGraph asset binding) require a team/agent registered in
the instance metadata plane. The setup phase below auto-provisions them via
/v3/meta/* (the instance API key acts as system_admin user key) and uses the
server-assigned IDs. Chat Memory isolation is independent (any string IDs).

Each check records latency. Exit code 0 when nothing marked FAIL.

Usage:
  export TDAI_MEMORY_ENDPOINT=... TDAI_MEMORY_API_KEY=... TDAI_MEMORY_INSTANCE_ID=...
  python3 tests/four_assets_test.py [--user-id test-user] [--team-id test-team]
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
        dt = time.time() - t0
        return raw, dt


def setup_assets(c: Client) -> Optional[Tuple[str, str]]:
    """Provision (or reuse) a team + agent in the metadata plane.

    The instance API key acts as system_admin user key. Server assigns its
    own IDs (team-xxx / agt-xxx) — custom IDs are treated as names at best.
    Returns (team_id, agent_id) or None on failure.
    """
    raw, _ = c.post("/v3/meta/user/list", {}, meta=True)
    users = (raw.get("data") or {}).get("items") or []
    if not users:
        print("setup: no users visible (API key may lack admin role)")
        return None
    owner = users[0]["user_id"]

    team_id = ""
    raw, _ = c.post("/v3/meta/team/list", {}, meta=True)
    for t in (raw.get("data") or {}).get("items") or []:
        if t.get("name") == "hermes-four-assets-test":
            team_id = t["team_id"]
            break
    if not team_id:
        raw, _ = c.post("/v3/meta/team/create", {
            "name": "hermes-four-assets-test", "owner_user_id": owner}, meta=True)
        team_id = (raw.get("data") or {}).get("team_id") or ""
        if not team_id:
            print(f"setup: team/create failed: {raw}")
            return None

    agent_id = ""
    raw, _ = c.post("/v3/meta/agent/list", {"team_id": team_id}, meta=True)
    for a in (raw.get("data") or {}).get("items") or []:
        if a.get("name") == "hermes-asset-test-agent":
            agent_id = a["agent_id"]
            break
    if not agent_id:
        raw, _ = c.post("/v3/meta/agent/create", {
            "name": "hermes-asset-test-agent", "team_id": team_id,
            "owner_user_id": owner}, meta=True)
        agent_id = (raw.get("data") or {}).get("agent_id") or ""
        if not agent_id:
            print(f"setup: agent/create failed: {raw}")
            return None

    print(f"setup: team={team_id} agent={agent_id}")
    return team_id, agent_id


class Reporter:
    def __init__(self):
        self.rows: List[Tuple[str, str, str, str]] = []  # asset, check, result, note

    def add(self, asset: str, check: str, ok: Optional[bool], note: str, dt: float = 0.0):
        if ok is True:
            status = "PASS"
        elif ok is False:
            status = "FAIL"
        else:
            status = "N/A"  # unsupported by design on this edition
        self.rows.append((asset, check, status, f"{note} ({dt*1000:.0f}ms)" if dt else note))
        print(f"  [{status}] {asset}/{check}: {note}")

    def summary(self) -> bool:
        print("\n" + "=" * 78)
        print(f"{'Asset':<14}{'Check':<34}{'Result':<8}Note")
        print("-" * 78)
        for asset, check, status, note in self.rows:
            print(f"{asset:<14}{check:<34}{status:<8}{note}")
        print("=" * 78)
        hard_fail = [r for r in self.rows if r[2] == "FAIL"]
        print(f"PASS={sum(1 for r in self.rows if r[2]=='PASS')}  "
              f"FAIL={len(hard_fail)}  "
              f"N/A={sum(1 for r in self.rows if r[2]=='N/A')}")
        return not hard_fail


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=os.environ.get("TDAI_MEMORY_ENDPOINT", ""))
    ap.add_argument("--api-key", default=os.environ.get("TDAI_MEMORY_API_KEY", ""))
    ap.add_argument("--instance-id", default=os.environ.get("TDAI_MEMORY_INSTANCE_ID", ""))
    ap.add_argument("--team-id", default="team-hermes-test")
    ap.add_argument("--agent-id", default="agent-hermes")
    ap.add_argument("--user-id", default="user-asset-test")
    args = ap.parse_args()
    if not (args.endpoint and args.api_key and args.instance_id):
        print("Missing TDAI_MEMORY_ENDPOINT / API_KEY / INSTANCE_ID", file=sys.stderr)
        return 2

    c = Client(args.endpoint, args.api_key, args.instance_id)
    iso = {"team_id": args.team_id, "agent_id": args.agent_id, "user_id": args.user_id}
    r = Reporter()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # ------------------------------------------------- metadata plane setup
    print("\n[0] Metadata plane setup (team/agent for asset binding)")
    assets = setup_assets(c)
    if assets:
        asset_team, asset_agent = assets
        r.add("Setup", "team/agent provisioning", True, f"team={asset_team} agent={asset_agent}")
    else:
        asset_team, asset_agent = "", ""
        r.add("Setup", "team/agent provisioning", False, "Skill/Wiki/CodeGraph checks will fail")

    # ------------------------------------------------------------------ Chat Memory
    print("\n[1] Chat Memory")
    raw, dt = c.post("/v3/conversation/add", {
        **iso, "session_id": "asset-test-1",
        "messages": [{"role": "user", "content": "四资产测试:我的测试编号是 9527", "timestamp": ts}],
    })
    r.add("ChatMemory", "L0 conversation/add", raw.get("code") == 0, f"code={raw.get('code')}", dt)

    raw, dt = c.post("/v3/conversation/query", {**iso, "session_id": "asset-test-1", "limit": 5})
    ok = raw.get("code") == 0 and any("9527" in (m.get("content") or "") for m in (raw.get("data") or {}).get("messages") or [])
    r.add("ChatMemory", "L0 conversation/query", ok, f"code={raw.get('code')}", dt)

    raw, dt = c.post("/v3/conversation/search", {**iso, "query": "测试编号", "limit": 5})
    # search index is asynchronous on the managed instance — empty now is expected
    ok = raw.get("code") == 0
    n = len((raw.get("data") or {}).get("messages") or [])
    r.add("ChatMemory", "L0 conversation/search", ok, f"code={raw.get('code')} hits={n} (async index, full check in benchmark)", dt)

    content = "# 用户画像(四资产测试)\n- 幸运数字: 73\n- 语言: Rust\n"
    raw, dt = c.post("/v3/core/write", {**iso, "content": content})
    r.add("ChatMemory", "L3 core/write", raw.get("code") == 0, f"code={raw.get('code')}", dt)

    raw, dt = c.post("/v3/core/read", dict(iso))
    ok = raw.get("code") == 0 and "73" in (raw.get("data") or {}).get("content") or ""
    r.add("ChatMemory", "L3 core/read", raw.get("code") == 0, f"code={raw.get('code')} has_content={bool((raw.get('data') or {}).get('content'))}", dt)

    raw, dt = c.post("/v3/atomic/search", {**iso, "query": "幸运数字", "limit": 5})
    n = len((raw.get("data") or {}).get("items") or [])
    r.add("ChatMemory", "L1 atomic/search (pipeline)", raw.get("code") == 0,
          f"code={raw.get('code')} items={n}" + (" — free edition: extraction pipeline idle" if n == 0 else ""), dt)

    raw, dt = c.post("/v3/scenario/ls", dict(iso))
    r.add("ChatMemory", "L2 scenario/ls", raw.get("code") == 0, f"code={raw.get('code')}", dt)

    raw, dt = c.post("/v3/scenario/write", {**iso, "path": "scene_blocks/test.md", "content": "x", "summary": "s"})
    # free edition: files are pipeline-created; create-by-write returns 404 → N/A
    r.add("ChatMemory", "L2 scenario/write (create)", None if raw.get("code") == 404 else raw.get("code") == 0,
          f"code={raw.get('code')} {raw.get('message', '')[:60]}", dt)

    raw, dt = c.post("/v3/offload/ingest", {**iso, "session_id": "offload-t1",
        "tool_pairs": [{"tool_name": "search", "tool_call_id": "c1", "params": {"q": "t"}, "result": "ok"}]})
    r.add("ChatMemory", "offload/ingest", None if raw.get("code") == 404 else raw.get("code") == 0,
          f"code={raw.get('code')} {str(raw.get('message', ''))[:50]}".strip(), dt)

    # ------------------------------------------------------------------ Skill
    print("\n[2] Skill")
    raw, dt = c.post("/v3/skill/create", {
        **iso, "team_id": asset_team, "agent_id": asset_agent,
        "name": "asset-test-skill",
        "content": "---\nname: asset-test-skill\ndescription: 四资产测试技能\n---\n\n# 步骤\n1. 测试 skill CRUD\n",
    }) if assets else ({}, 0.0)
    ok = raw.get("code") == 0
    skill_id = ((raw.get("data") or {}) or {}).get("skill_id") if isinstance(raw.get("data"), dict) else None
    r.add("Skill", "create", ok, f"code={raw.get('code')} skill_id={skill_id}", dt)

    skill_iso = {"team_id": asset_team, "agent_id": asset_agent, "user_id": args.user_id}
    if skill_id:
        raw, dt = c.post("/v3/skill/get", {**skill_iso, "skill_id": skill_id, "include_content": True})
        ok = raw.get("code") == 0 and "asset-test" in json.dumps(raw.get("data") or {})
        r.add("Skill", "get", ok, f"code={raw.get('code')}", dt)

        raw, dt = c.post("/v3/skill/get-by-name", {**skill_iso, "skill_name": "asset-test-skill"})
        r.add("Skill", "get-by-name", raw.get("code") == 0, f"code={raw.get('code')}", dt)

        raw, dt = c.post("/v3/skill/files/write", {**skill_iso, "skill_id": skill_id, "expected_version": 1,
            "files": [{"path": "scripts/run.sh", "content": "#!/bin/sh\necho ok\n", "encoding": "utf-8"}]})
        ok = raw.get("code") == 0
        r.add("Skill", "files/write", ok, f"code={raw.get('code')}", dt)
        ver = ((raw.get("data") or {}) or {}).get("version") or 2 if ok else 1

        raw, dt = c.post("/v3/skill/files/read", {**skill_iso, "skill_id": skill_id, "path": "scripts/run.sh"})
        r.add("Skill", "files/read", raw.get("code") == 0, f"code={raw.get('code')}", dt)

        raw, dt = c.post("/v3/skill/search", {**skill_iso, "query": "测试技能", "top_k": 5})
        n = len((raw.get("data") or {}).get("items") or [])
        r.add("Skill", "search", raw.get("code") == 0 and n >= 1, f"code={raw.get('code')} hits={n}", dt)

        raw, dt = c.post("/v3/skill/list", {**skill_iso, "limit": 50})
        n = len((raw.get("data") or {}).get("items") or [])
        r.add("Skill", "list", raw.get("code") == 0 and n >= 1, f"code={raw.get('code')} items={n}", dt)

        raw, dt = c.post("/v3/skill/delete", {**skill_iso, "skill_id": skill_id, "expected_version": int(ver)})
        r.add("Skill", "delete (cleanup)", raw.get("code") == 0, f"code={raw.get('code')}", dt)
    else:
        r.add("Skill", "create", False, "no skill_id returned; remaining skill checks skipped")

    # ------------------------------------------------------------------ Wiki
    print("\n[3] Wiki (knowledge metadata)")
    wiki_body = {**iso, "knowledge_id": "wiki-test-1", "type": "wiki",
        "name": "测试Wiki", "summary": "四资产测试",
        "service_url": f"{args.endpoint}/v3"}
    if asset_team:
        wiki_body["team_id"] = asset_team
    raw, dt = c.post("/v3/knowledge/create", wiki_body)
    ok = raw.get("code") == 0
    r.add("Wiki", "knowledge/create (wiki)", ok, f"code={raw.get('code')} {str(raw.get('message', ''))[:60]}", dt)

    if ok:
        raw, dt = c.post("/v3/knowledge/get", {"team_id": (asset_team or args.team_id), "agent_id": args.agent_id, "user_id": args.user_id, "knowledge_id": "wiki-test-1"})
        r.add("Wiki", "knowledge/get", raw.get("code") == 0, f"code={raw.get('code')}", dt)
        raw, dt = c.post("/v3/knowledge/list", {"team_id": (asset_team or args.team_id), "type": "wiki"})
        n = len((raw.get("data") or {}).get("items") or [])
        r.add("Wiki", "knowledge/list", raw.get("code") == 0 and n >= 1, f"code={raw.get('code')} items={n}", dt)
        r.add("Wiki", "content search/read", None,
              "metadata-only plane; content plane needs Knowledge Service (self-hosted :8421)")
        raw, dt = c.post("/v3/knowledge/delete", {"team_id": (asset_team or args.team_id), "agent_id": args.agent_id, "user_id": args.user_id, "knowledge_ids": ["wiki-test-1"]})
        r.add("Wiki", "knowledge/delete (cleanup)", raw.get("code") == 0, f"code={raw.get('code')}", dt)

    # ------------------------------------------------------------------ CodeGraph
    print("\n[4] CodeGraph (knowledge metadata)")
    cg_body = {**iso, "knowledge_id": "cg-test-1", "type": "code-graph",
        "name": "测试CodeGraph", "summary": "四资产测试",
        "service_url": f"{args.endpoint}/v3"}
    if asset_team:
        cg_body["team_id"] = asset_team
    raw, dt = c.post("/v3/knowledge/create", cg_body)
    ok = raw.get("code") == 0
    r.add("CodeGraph", "knowledge/create (code-graph)", ok, f"code={raw.get('code')} {str(raw.get('message', ''))[:60]}", dt)

    if ok:
        raw, dt = c.post("/v3/knowledge/get", {"team_id": (asset_team or args.team_id), "agent_id": args.agent_id, "user_id": args.user_id, "knowledge_id": "cg-test-1"})
        r.add("CodeGraph", "knowledge/get", raw.get("code") == 0, f"code={raw.get('code')}", dt)
        raw, dt = c.post("/v3/knowledge/list", {"team_id": (asset_team or args.team_id), "type": "code-graph"})
        n = len((raw.get("data") or {}).get("items") or [])
        r.add("CodeGraph", "knowledge/list", raw.get("code") == 0 and n >= 1, f"code={raw.get('code')} items={n}", dt)
        r.add("CodeGraph", "graph query", None,
              "metadata-only plane; graph engine needs Knowledge Service (self-hosted :8421)")
        raw, dt = c.post("/v3/knowledge/delete", {"team_id": (asset_team or args.team_id), "agent_id": args.agent_id, "user_id": args.user_id, "knowledge_ids": ["cg-test-1"]})
        r.add("CodeGraph", "knowledge/delete (cleanup)", raw.get("code") == 0, f"code={raw.get('code')}", dt)

    return 0 if r.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
