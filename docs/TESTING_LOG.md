# 测试过程记录(Test Log)

> 时间跨度:2026-08-30 ~ 2026-09-01
> 环境:2 台 TencentOS 4(43.155.112.112 本机 / 43.134.203.148 远端),Python 3.11.6,
> Hermes Agent 0.19.0(pip),腾讯云 Agent Memory 免费版(广州),模型 glm-5.3
> 本文档记录"怎么测的、看到了什么、发现了什么问题、怎么修的"。
> 结论性数据见 `BENCHMARK.md`,操作方法见 `OPERATIONS.md`。所有密钥已脱敏。

---

## 阶段 0:环境准备(08-30)

```bash
# 连通性矩阵(两台机器均执行)
curl -s ifconfig.me                                  # 确认出口 IP
curl -sI https://github.com -o /dev/null -w "%{http_code}\n"        # 200
curl -sI https://memory.ap-guangzhou.tencenttdai.com -o /dev/null   # 404(根路径,实例存活)
curl -s -X POST https://tokenhub.tencentmaas.com/v1/chat/completions ...  # glm-5.3 回复 "ok"
```

数据面鉴权探测(用真实凭据,此处脱敏):

```
POST /v3/core/read  + Bearer <API_KEY> + x-tdai-service-id: <INSTANCE_ID>
→ {"code":0,...}          # v3 数据面免费版可用
POST /hermes/<spaceId>/v1/chat/completions
→ {"code":5901,"message":"free edition instance is not allowed to access proxy service"}
                          # 免费版禁用官方代理注入路由 → 决定自研 provider
POST /mcp → 无响应        # 无 MCP 接入
```

## 阶段 1:安装与 LLM 冒烟(08-30)

```bash
pip install hermes-agent            # 0.19.0
# ~/.hermes/.env: GLM_API_KEY / GLM_BASE_URL(tokenhub)
# ~/.hermes/config.yaml: model.provider=zai, default=glm-5.3
hermes -z "请只回复两个字母: ok"     # → "ok"  ✅ 模型链路通
```

## 阶段 2:记忆链路端到端(08-30)

### 2.1 首轮打通(边测边修)

第一轮跑出的 bug 与修复过程:

| 步骤 | 现象 | 根因 | 修复 |
|---|---|---|---|
| 直写云端 OK,但插件上报 400 | `Invalid ISO datetime` | 服务端只认 UTC `Z` 后缀,`+08:00` 被拒 | `time.gmtime()` + `%Y-%m-%dT%H:%M:%SZ` |
| 写入成功但检索为空 | search 返回 0 命中 | **索引异步**,实测 2~3 分钟生效 | 非 bug,测试加等待 |
| 插件检索永远空 | 工具返回 `{"items":[]}` | conversation/search 的数据键是 **`messages`**,atomic/search 才是 `items` | `_items()` 兼容两者 |
| 偶发检索跳过 | prefetch 无结果 | 串行 3 次调用最坏 36s,超出 MemoryManager 8s prefetch 预算被静默跳过 | 三路并行 + 5.5s 单请求超时 + 6.5s 共享 deadline |

修完后首次端到端:

```
A 会话: hermes -z "我叫张伟,我最喜欢的编程语言是 Rust,请记住"
B 会话: hermes -z "我最喜欢什么编程语言?"
→ "根据我的记录,你最喜欢的编程语言是 Rust。"   ✅ 单机跨会话召回 PASS
```

### 2.2 跨机召回

```
A 机写入"幸运数字 73" → 等 90~150s 索引生效
B 机 hermes -z "我的幸运数字是多少?请直接给出数字" → "73"   ✅ PASS
```

**重要教训(过程中踩坑)**:期间为清理测试数据对某隔离维度批量
`conversation/delete` → 该 user 的检索索引永久失效(query 可见、search 永远
0 命中,双盲复核确认)。只能换新 user_id 恢复。**结论:永远不要批量删除。**

## 阶段 3:缺陷修复循环(08-30 ~ 09-01)

每个缺陷走"复现 → 定位 → 修复 → 验证 → 推送"闭环:

### #2 检索污染(commit 6292eb1)

- 复现:远端连续问"幸运数字"失败后,失败回答("查不到…")在 L0 检索中
  排名超过正确记忆(与提问字面重叠度更高)→ 召回失败,自我强化
- 定位:云端 search 按 score 排序,失败回答 score 0.0317 > 正确回答 0.0294
- 修复:capture 侧跳过"记忆未命中"类回复(`_is_negative_memory`,中英文
  14 条保守正则);召回侧剔除该类条目 + 按前缀去重
- 验证:污染条目从 prefetch 消失,"已记住…73"浮出;真机负轮次未上报;
  跨机召回恢复 PASS

### #1 `-z` 尾轮丢失(commit 2805b9d)

- 复现:离线压测 3 轮 + 立即 shutdown → 云端 4/6 条(TAIL2 丢失)
- 定位:插件内 queue+daemon worker 与 MemoryManager 后台 sync 线程双重异步;
  oneshot 用 `os._exit` 硬退出(main.py `_exit_after_oneshot`),daemon worker
  连同在途 POST 被杀
- 修复:删除第二层异步,`sync_turn` 直接同步上传(4s 超时)。依据:MemoryManager
  硬退出前会 drain 等待 sync 任务 ≤5s,同步上传天然在该窗口内完成
- 验证:6/6 条落库;真机连续两次 `-z`(RT1/RT2)末轮全部立即落库

### #5 L1 空工具(commit 4f456f3)

- 发现:修 #5 时意外发现 L1 抽取管线**开始产出** work_fact(开通数小时后),
  推翻"免费版 L1 不产出"的早期结论 → 改为"延迟产出"
- 修复:initialize 时探测 `/v3/atomic/query`(limit=1):空 → 不注册
  `tdai_memory_search` 且 prefetch 跳过 atomic 路(3→2 路);有产物自动恢复;
  探测失败保守视为可用
- 验证:两条门控路径单测通过;真机 `-z` 中模型综合 L1+L0 准确列出全部测试标记

### #4 capture 持久重试(commit 5c08a2e)

- 复现:端点指向死端口 → sync_turn 异常 → 该轮永久丢失
- 修复:失败轮次(网络/5xx/超时/429)落盘 `~/.hermes/tdam-cloud-spool/*.json`
  (上限 200 条),下次 initialize 有界重放(≤12s/≤8 条/首败即停);4xx 永久
  错误不重试
- 验证:死端点 → spool 落盘 1 文件;恢复端点 → initialize 重放,spool 清空,
  "SPOOL123" 轮次落库

## 阶段 4:四资产能力测试(08-31,commit c4f15a2)

过程:先探测数据面可用性 → 发现 `/v3/skill/*` 全套路由存在 → skill/create 报
`50001 agent_not_found` → 发现需元数据面注册 team/agent → 探明实例 API Key 即
system_admin 身份(`/v3/meta/*` 以 `x-tdai-user-key` 鉴权)→ 注意服务端自动
分配 team-…/agt-… ID → Skill 全链路打通(16 项接口 PASS)。

期间 schema 踩坑(都是 zod 40001):`get-by-name` 用 `skill_name`;
`files/write` 的 `files[].encoding` 必填;`files/read` 用单数 `path`。

最终四资产结论:Chat Memory(L0/L3 可用,L1 延迟产出,L2 只读,offload 404)、
Skill ✅ 全可用、Wiki/CodeGraph ✅ 元数据 CRUD(内容面需自建 Knowledge Service)。

## 阶段 5:benchmark 采集(08-30 / 08-31)

两轮采集对比(`tests/memory_benchmark.py --runs 6`):

| 指标 | 空闲时段 | 高负载时段(522 频发) |
|---|---|---|
| L0 capture | <1s | median 3.7s / max 12.3s |
| L0 search | <1s | median 3.9s / max 19.3s |
| prefetch 端到端 | 0.35~4.1s | median=max=6.50s(压在 deadline) |
| 超 8s 预算次数 | 0 | 0 |

## 阶段 6:三层记忆内核测试(09-01,commit 9d90739)

`tests/three_layers_test.py` 自动生成唯一标记跑完整矩阵,关键实测:

- 短期(L0):会话内 query/跨轮 search/session 隔离全 PASS
- 长期:L3 core 读写 PASS;L1 抽取 120s 观测窗未覆盖(PENDING,后续确认
  为分钟~小时级延迟)
- 团队:**L3 core 跨 user SHARED**(双盲 user 复核:两个全新 user 读到完全
  相同内容;换 agent 为空)→ L3 是 (team, agent) 级"Agent 共享画像",平台
  语义而非泄漏;L0/L1 严格按 user 隔离;Wiki 资产 team 级共享
- 测试后清理:恢复真实用户画像,测试标记数据保留在云端(免费版禁批量删)

---

## 复现索引

| 内容 | 位置 |
|---|---|
| 四资产测试 | `python3 tests/four_assets_test.py` |
| 三层记忆测试 | `python3 tests/three_layers_test.py`(约 6 分钟,含索引等待) |
| 记忆 benchmark | `python3 tests/memory_benchmark.py --runs 6` |
| 跨机召回 | 两机分别执行 BENCHMARK.md §2.2 的 A/B 步骤 |
| 云端巡检 curl | OPERATIONS.md §6 |
