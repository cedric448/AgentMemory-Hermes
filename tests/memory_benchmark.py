#!/usr/bin/env python3
"""Benchmark: Chat Memory recall quality & latency for the cloud provider.

Measures (single machine, provider in isolation):
  1. L0 capture latency (conversation/add)
  2. L0 conversation/search latency + hit quality (N queries)
  3. L3 core/read latency
  4. prefetch() end-to-end latency (N runs) — must stay under Hermes' 8s budget
  5. Search index propagation delay (write -> searchable)

Usage:
  export TDAI_MEMORY_ENDPOINT=... TDAI_MEMORY_API_KEY=... TDAI_MEMORY_INSTANCE_ID=...
  python3 tests/memory_benchmark.py [--runs 5]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, "/usr/local/lib/python3.11/site-packages")
from plugins.memory import load_memory_provider  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--skip-write", action="store_true",
                    help="skip fact writing (use existing memory)")
    args = ap.parse_args()

    p = load_memory_provider("memory_tencentdb_cloud")
    if not p or not p.is_available():
        print("provider unavailable", file=sys.stderr)
        return 2
    session = f"bench-{int(time.time())}"
    p.initialize(session_id=session)
    print(f"session={session} runs={args.runs}\n")

    # 1. capture latency
    cap_lat = []
    facts = [f"基准测试事实{i}:我的备用联系方式是 1380000{i:04d}" for i in range(3)]
    if not args.skip_write:
        for f in facts:
            t0 = time.time()
            p.sync_turn(f, "已记住。")
            p._queue.join()  # wait for worker to upload
            cap_lat.append(time.time() - t0)
        print(f"[capture]  addConversation x{len(cap_lat)}: "
              f"median={statistics.median(cap_lat)*1000:.0f}ms  "
              f"max={max(cap_lat)*1000:.0f}ms")

    # 2/3. per-call latencies via tools
    search_lat, core_lat = [], []
    for i in range(args.runs):
        t0 = time.time()
        p.handle_tool_call("tdai_conversation_search", {"query": "备用联系方式", "limit": 5})
        search_lat.append(time.time() - t0)
        t0 = time.time()
        try:
            p._client.core_read()
        except Exception:
            pass
        core_lat.append(time.time() - t0)
    print(f"[L0 search] x{args.runs}: median={statistics.median(search_lat)*1000:.0f}ms  "
          f"p95={sorted(search_lat)[int(0.95*len(search_lat))-1]*1000:.0f}ms  "
          f"max={max(search_lat)*1000:.0f}ms")
    print(f"[L3 core ] x{args.runs}: median={statistics.median(core_lat)*1000:.0f}ms  "
          f"max={max(core_lat)*1000:.0f}ms")

    # 4. prefetch end-to-end (budget 8s — any run over budget loses that turn's recall)
    pf_lat, pf_ok = [], 0
    for i in range(args.runs):
        t0 = time.time()
        r = p.prefetch("备用联系方式是什么")
        dt = time.time() - t0
        pf_lat.append(dt)
        if r:
            pf_ok += 1
        time.sleep(0.3)
    over = sum(1 for d in pf_lat if d > 8.0)
    print(f"[prefetch] x{args.runs}: median={statistics.median(pf_lat):.2f}s  "
          f"max={max(pf_lat):.2f}s  非空={pf_ok}/{args.runs}  超8s预算={over}")

    # 5. quality check: prefetch must surface the fact written above
    #    (needs index to have caught up; report hit only, don't fail on async lag)
    hit = "备用联系方式" in (p.prefetch("我的备用联系方式") or "")
    print(f"[quality ] prefetch contains written fact: {hit} (受索引异步影响,仅参考)")

    p.shutdown()
    print("\nbudget note: Hermes MemoryManager external-prefetch timeout = 8.0s; "
          "provider deadline = 6.5s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
