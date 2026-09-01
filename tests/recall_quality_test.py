#!/usr/bin/env python3
"""Memory recall-quality test for two Hermes instances sharing one
TencentDB Agent Memory instance (same isolation quadruple).

Design: 9 cases, each with a UNIQUE marker embedded in the fact, so grading
is objective (grep the marker in the agent's answer):

  C1  exact-fact        project code
  C2  multi-fact pick   two facts written together, asked for one
  C3  distractor        the other fact of C2 (wrong marker must NOT appear)
  C4  paraphrase        fact written one way, asked another way
  C5  negative control  fact never told — agent must NOT fabricate
  C6  recency update    fact updated Monday->Wednesday, expect the latest
  C7  long-code         long number (tests exact copy through context)
  C8  cross-layer L3    fact written directly into core memory via API
  C9  cross-machine     fact written on the REMOTE machine, asked LOCALLY

Phases:
  --phase write   run all write prompts (C1..C7,C9 via hermes -z; C8 via API)
  --phase ask     run all questions via hermes -z and grade answers
Run write on both machines for C9 (write happens on the remote host).
Between phases, wait >=120s for the search index to catch up.

Remote execution uses the REMOTE_SSH env var, e.g.:
  export REMOTE_SSH="sshpass -p <pw> ssh -o StrictHostKeyChecking=no root@<ip>"

Usage:
  python3 tests/recall_quality_test.py --phase write
  python3 tests/recall_quality_test.py --phase ask --host local
  python3 tests/recall_quality_test.py --phase ask --host remote
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from typing import Callable, Dict, List, Optional

HERMES_TIMEOUT = 180

# ---------------------------------------------------------------------------
# Case definitions. markers are unique across cases.
# WRITES: list of (prompt, machine) — machine in {"local","remote"}
# QUESTION: asked on BOTH machines
# GRADE: "contains" / "contains_none" / "negative"
# EXPECT: substrings that must appear; FORBID: substrings that must not
# ---------------------------------------------------------------------------
CASES = [
    dict(id="C1", type="精确事实",
         writes=[("请记住:我的项目代号是 ALPHA-77。简单确认即可。", "local")],
         question="我的项目代号是什么?直接给出代号,不要解释。",
         grade="contains", expect=["ALPHA-77"], forbid=[]),
    dict(id="C2", type="多条目-指定",
         writes=[("请记住两条信息:我的门禁码是 4421,我家 WiFi 密码是 WIFI-8899。简单确认即可。", "local")],
         question="我的门禁码是多少?直接给出数字。",
         grade="contains_none", expect=["4421"], forbid=["WIFI-8899", "8899"]),
    dict(id="C3", type="干扰项",
         writes=[],  # 与 C2 同一次写入
         question="我家 WiFi 密码是多少?直接给出密码。",
         grade="contains_none", expect=["WIFI-8899"], forbid=["4421"]),
    dict(id="C4", type="转述召回",
         writes=[("请记住:我对香菜过敏,做饭不要放香菜。简单确认即可。", "local")],
         question="我能吃香菜吗?",
         grade="contains", expect=["过敏", "不能", "不要", "避免"], forbid=[]),
    dict(id="C5", type="否定控制(不许编造)",
         writes=[],
         question="我的车牌号是多少?直接给出车牌号。",
         grade="negative", expect=["没有", "不确定", "查不到", "未"], forbid=[]),
    dict(id="C6", type="时效更新",
         writes=[("请记住:我的值班日是周一。简单确认即可。", "local"),
                 ("注意:我的值班日改到周三了,以后按新的算。简单确认即可。", "local")],
         question="我的值班日是周几?直接回答。",
         grade="contains_none", expect=["周三"], forbid=["周一"]),
    dict(id="C7", type="长数字编码",
         writes=[("请记住:我的报销单号是 BX-20260901-0533。简单确认即可。", "local")],
         question="我的报销单号是什么?完整、准确地写出来。",
         grade="contains", expect=["BX-20260901-0533"], forbid=[]),
    dict(id="C8", type="跨层召回(L3 画像)",
         writes=[],  # write 阶段直接调 /v3/core/write
         question="我的工号是多少?直接给出工号。",
         grade="contains", expect=["EMP-5566"], forbid=[]),
    dict(id="C9", type="跨机写入-本机召回",
         writes=[("请记住:我的 VPN 服务器地址是 VPN-7748。简单确认即可。", "remote")],
         question="我的 VPN 服务器地址是什么?直接给出地址。",
         grade="contains", expect=["VPN-7748"], forbid=[]),
]

C8_CORE = "# 用户画像\n- 工号: EMP-5566\n"


def run_hermes(prompt: str, machine: str, remote_ssh: str) -> str:
    if machine == "local":
        cmd = ["timeout", str(HERMES_TIMEOUT), "hermes", "-z", prompt]
    else:
        if not remote_ssh:
            raise RuntimeError("REMOTE_SSH not set; cannot run on remote")
        inner = f"timeout {HERMES_TIMEOUT} hermes -z '{prompt}'"
        cmd = remote_ssh.split() + [inner]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=HERMES_TIMEOUT + 30)
    return (res.stdout or "") + (res.stderr or "")


def write_l3_core() -> bool:
    ep = os.environ.get("TDAI_MEMORY_ENDPOINT", "")
    key = os.environ.get("TDAI_MEMORY_API_KEY", "")
    sid = os.environ.get("TDAI_MEMORY_INSTANCE_ID", "")
    req = urllib.request.Request(
        ep.rstrip("/") + "/v3/core/write",
        data=json.dumps({
            "team_id": os.environ.get("TDAI_MEMORY_TEAM_ID", "team-hermes-test"),
            "agent_id": os.environ.get("TDAI_MEMORY_AGENT_ID", "agent-hermes"),
            "user_id": os.environ.get("TDAI_MEMORY_USER_ID", "user-hermes-v2"),
            "content": C8_CORE,
        }).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}",
                 "x-tdai-service-id": sid},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()).get("code") == 0


def phase_write(remote_ssh: str) -> int:
    ok = True
    for c in CASES:
        for prompt, machine in c["writes"]:
            print(f"[{c['id']}] write on {machine}: {prompt[:40]}...", flush=True)
            out = run_hermes(prompt, machine, remote_ssh)
            tail = out.strip().splitlines()[-1] if out.strip() else "(empty)"
            print(f"    -> {tail[:70]}", flush=True)
    try:
        print("[C8] write L3 core via API...", flush=True)
        print("    ->", "ok" if write_l3_core() else "failed", flush=True)
    except Exception as e:
        print("    -> failed:", e, flush=True)
        ok = False
    print("\nwrite phase done. Wait >=120s for the search index before asking.", flush=True)
    return 0 if ok else 1


def grade(case: Dict, answer: str) -> str:
    a = answer.lower()
    if case["grade"] == "negative":
        return "PASS" if any(w in a for w in case["expect"]) else "FAIL"
    # expect[] is a list of ACCEPTABLE synonyms — any one suffices
    # (e.g. 过敏/不能/不要/避免 all indicate a correct "allergic" recall).
    good = any(w.lower() in a for w in case["expect"])
    bad = any(w.lower() in a for w in case["forbid"])
    return "PASS" if (good and not bad) else "FAIL"


def phase_ask(host: str, remote_ssh: str) -> int:
    results = []
    for c in CASES:
        print(f"[{c['id']}] ask on {host}: {c['question'][:40]}...", flush=True)
        ans = run_hermes(c["question"], host, remote_ssh)
        tail = ans.strip().splitlines()[-1] if ans.strip() else "(empty)"
        verdict = grade(c, ans)
        results.append((c, verdict, tail))
        print(f"    -> [{verdict}] {tail[:80]}", flush=True)
    print("\n" + "=" * 76)
    print(f"{'Case':<6}{'Type':<16}{'Result':<8}Answer(tail)")
    print("-" * 76)
    npass = 0
    for c, verdict, tail in results:
        npass += verdict == "PASS"
        print(f"{c['id']:<6}{c['type']:<16}{verdict:<8}{tail[:48]}")
    print("-" * 76)
    print(f"PASS {npass}/{len(results)}  host={host}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["write", "ask"], required=True)
    ap.add_argument("--host", choices=["local", "remote"], default="local")
    ap.add_argument("--wait", type=int, default=0, help="sleep N seconds after phase")
    args = ap.parse_args()
    remote_ssh = os.environ.get("REMOTE_SSH", "")
    rc = phase_write(remote_ssh) if args.phase == "write" else phase_ask(args.host, remote_ssh)
    if args.wait:
        time.sleep(args.wait)
    return rc


if __name__ == "__main__":
    sys.exit(main())
