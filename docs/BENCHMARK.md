# 测试报告与 Benchmark

> 测试日期:2026-08-30(两轮:初版插件 + 四资产扩展)
> 环境:2 台 TencentOS 4 服务器(腾讯云,Python 3.11.6,Hermes Agent 0.19.0)
> 被测实例:TencentDB Agent Memory **免费版**(广州区)
> 模型:glm-5.3(经 OpenAI 兼容网关)
> 测试脚本:`tests/four_assets_test.py`(四资产能力)、`tests/memory_benchmark.py`(记忆 benchmark)

## 1. 四资产能力测试(免费版数据面)

官方"四资产"(Chat Memory / Skill / Wiki / CodeGraph)的注入链路走代理服务,
免费版不可用(code 5901)。本测试验证的是**数据面直接访问**的真实可用性,
测试脚本见 `tests/four_assets_test.py`(可重复执行,自动创建/清理测试资产)。

最终结果汇总(2026-08-30 实测):

| 资产 | 能力 | 数据面接口 | 免费版结果 |
|------|------|-----------|-----------|
| **Chat Memory** | L0 对话写入/查询/检索 | `/v3/conversation/add·query·search` | ✅ 全部可用 |
| Chat Memory | L3 核心记忆写/读 | `/v3/core/write·read` | ✅ 可用(手动维护) |
| Chat Memory | L1 结构化记忆抽取 | `/v3/atomic/search·query` | ⚠️ 管线延迟产出:开通初期持续为空,2026-08-31 起开始产出(work_fact 等);插件已加**自适应门控**(initialize 时探测,L1 有产物才注册 `tdai_memory_search` 并启用 prefetch 的 atomic 路) |
| Chat Memory | L2 场景块 | `/v3/scenario/ls·read·write` | ⚠️ 只能列举;write 不能创建文件(404) |
| Chat Memory | 上下文 offload | `/v3/offload/*` | ❌ 未路由(404) |
| **Skill** | 全生命周期 CRUD | `/v3/skill/create·get·get-by-name·list·search·files/*·delete` | ✅ **16 项接口全部可用** |
| **Wiki** | 元数据 CRUD | `/v3/knowledge/create·get·list·update·delete` (type=wiki) | ✅ 可用(仅元数据) |
| Wiki | 内容检索/页面读取 | Knowledge Service 数据面 | ❌ 需自建 :8421(免费版不含) |
| **CodeGraph** | 元数据 CRUD | `/v3/knowledge/*` (type=code-graph) | ✅ 可用(仅元数据) |
| CodeGraph | 图查询 | Knowledge Service 数据面 | ❌ 需自建 :8421(免费版不含) |

测试执行明细(最后一轮完整跑通的部分,每项含延迟):

```
Setup         team/agent provisioning           PASS
ChatMemory    L0 conversation/add               PASS
ChatMemory    L0 conversation/query             PASS
ChatMemory    L0 conversation/search            PASS  (索引异步)
ChatMemory    L3 core/write / core/read         PASS
ChatMemory    L1 atomic/search                  PASS  (items=0,免费版管线不产出)
ChatMemory    L2 scenario/ls                    PASS
ChatMemory    L2 scenario/write(创建)           N/A   404 file not found
ChatMemory    offload/ingest                    N/A   404 未路由
Skill         create / get / get-by-name        PASS  (skill_id=skl-…)
Skill         files/write / files/read          PASS
Skill         search / list / delete            PASS  (search 同步命中)
Wiki          knowledge/create·get·list·delete  PASS
Wiki          内容检索                          N/A   需 Knowledge Service
CodeGraph     knowledge/create·get·list·delete  PASS
CodeGraph     图查询                            N/A   需 Knowledge Service
```

### Skill 资产的关键前置条件(重要发现)

`skill/create` 要求 agent 已在**元数据面**注册,否则报
`50001 agent_not_found`。元数据面接口(`/v3/meta/*`)以 `x-tdai-user-key`
鉴权,实例 API Key 即 system_admin 身份,可直接:

```
POST /v3/meta/user/list            → 拿到 owner user_id(usr-…)
POST /v3/meta/team/create          → ⚠️ 服务端自动分配 team_id(自定义 ID 会被当 name)
POST /v3/meta/agent/create         → ⚠️ 同样自动分配 agt-… ID
```

之后 Skill 请求必须携带**服务端分配的** `team_id`(team-…)/`agent_id`(agt-…)。
其它易错点:
- skill `content` 必须带 YAML frontmatter(`---\nname: …\ndescription: …\n---`),否则 `42203`
- `get-by-name` 的字段名是 `skill_name` 而非 `name`(40001 zod 校验)
- `files/write` 的 `files[].encoding` 必填(`"utf-8"`);`files/read` 用单数 `path`
- knowledge `get/delete` 的 team_id 必须与实体归属一致,否则 403 team_id mismatch

## 2. Chat Memory benchmark

### 2.1 写入 → 单机新会话召回

```
会话 A: hermes -z "我叫张伟,我最喜欢的编程语言是 Rust,请记住"
会话 B: hermes -z "我最喜欢什么编程语言?"
结果  : "根据我的记录,你最喜欢的编程语言是 Rust。"        ✅ PASS
```

### 2.2 跨机召回(核心场景,两轮验证均 PASS)

```
A 机:  hermes -z "我的幸运数字是 73,请记住"   → "已记住"
       (等待约 2.5 分钟索引生效)
B 机:  hermes -z "我的幸运数字是多少?请直接给出数字"
结果  : "73"                                    ✅ PASS
复测  : hermes -z "请记住:我的高铁会员号是 G888888" → 跨机召回验证通过
```

### 2.3 性能数据(2026-08-30,`tests/memory_benchmark.py --runs 6`)

两种实例负载状态下的对比:

| 指标 | 空闲时段 | 高负载时段(522 频发) |
|---|---|---|
| L0 capture(写入) | <1s | median 3.7s / max 12.3s |
| L0 search | <1s | median 3.9s / p95 6.5s / max 19.3s |
| L3 core/read | <0.5s | median 0.9s / max 6.4s |
| **prefetch 端到端** | **0.35~4.1s** | **median=max=6.50s(全部命中 deadline)** |
| prefetch 超出 Hermes 8s 预算 | 0 | **0(6.5s deadline 生效)** |
| prefetch 非空率 | 100% | 100% |

> 结论:并行化 + 5.5s 单请求超时 + 6.5s 总 deadline 后,即使实例高负载,
> prefetch 也不会超出 MemoryManager 的 8s 预算,召回保持可用;
> 代价是高负载时部分 section 可能被截断(优雅降级)。

### 2.4 检索索引生效延迟

| 实验 | 写入 → 可检索 |
|---|---|
| 第 1 组 | ≈2 分钟 |
| 第 2 组 | 2.5~3 分钟 |
| 第 3 组 | 100s |
| 基准脚本写入 | ≈2 分钟 |

结论:索引异步生效,**写入后立即提问检索不到属预期**,建议等待 2~3 分钟。

### 2.5 召回质量

| 场景 | 无云端记忆 | 有云端记忆 |
|---|---|---|
| 新会话问个人事实(数字/偏好/编号) | "不确定/没有记录" | 准确回答 |
| 跨机器共享用户上下文 | 不可能 | 同四元组即可 |
| 语义近似问法("幸运数字"→" lucky number") | — | L0 关键词检索依赖字面重叠,近似问法可能漏召回 |

### 2.6 检索污染修复验证(2026-08-30)

修复前:`user-hermes-v2` 中"不确定/查不到"类 assistant 回复在 L0 检索中
排名高于正确记忆(字面重叠分更高),曾导致跨机召回失败。

修复后实测(`user-hermes-v2` 内已有 3 条污染数据的情况下):

```
prefetch("我的幸运数字是多少")
→ <related_conversations> 中不再出现任何"不确定/查不到"条目
→ "[assistant] 好的,已记住:你的幸运数字是 73。" 正常浮出
→ 重复提问自动去重(前缀 120 字符)
capture 过滤:负样本轮次不入队(队列 0);正常轮次入队(队列 1)
真实 hermes -z 验证:问"我的社保卡号是多少"(答"查不到")→ 该轮未上报云端
跨机召回:远端 hermes -z "我的幸运数字是多少?" → "73"   ✅ PASS
```

已知残留:用户侧的重复提问(非 assistant 回复)仍会 capture 并参与检索,
暂无法与真实记忆区分;影响有限(问题本身不含答案信息)。

### 2.7 `-z` 尾轮丢失修复验证(2026-08-31)

根因:插件内自建的 queue+daemon worker 与 MemoryManager 的后台 sync 线程
形成双重异步——`-z` 硬退出(`os._exit`)时,末轮 payload 可能尚未入队,
或在途 POST 被 worker 连带杀死。

修复:移除插件内第二层异步,`sync_turn` 直接同步上传(4s 超时,单次)。
MemoryManager 在硬退出前会等待后台 sync 任务 ≤5s,同步上传在该窗口内
天然完成,无竞态。

```
离线压测:3 轮连续 sync_turn + 立即 shutdown → 云端 6/6 条消息落库(修复前 4/6)
真机验证:远端连续两次 hermes -z(RT1/RT2)→ 两次末轮均立即落库   ✅ PASS
残余约束:实例单请求延迟 >5s 时,末轮可能被 MemoryManager 5s drain
预算放弃(hermes 侧硬边界);常态 <1s 零丢失,上报失败会写错误日志
```

补充(2026-09-01,#12 修复后):上传失败(5xx/网络/超时/429)的轮次
持久化到 `$HERMES_HOME/tdam-cloud-spool/` 并在下次 initialize 重放,
配合 #1 的同步上传,丢失窗口收敛为"进程被 SIGKILL 且 spool 写盘前"
这一极小窗口。

### 2.8 三层记忆内核测试(2026-09-01)

测试脚本:`tests/three_layers_test.py`(可重复执行,自动生成唯一标记)。

| 层 | 检查 | 结果 | 说明 |
|---|---|---|---|
| 短期 | L0 会话内写入/query | PASS | 385~1294ms |
| 短期 | 跨轮 search 召回 | PASS | 90s 索引等待后命中 |
| 短期 | session 隔离 | PASS | query 按 session 精确过滤 |
| 长期 | L1 管线抽取(2 分钟观测窗) | PENDING | 抽取延迟 >2 分钟,更早轮次已产出 work_fact |
| 长期 | L3 core/write + read 回读 | PASS | ~500ms |
| 长期 | 跨会话召回(L0 search) | PASS | 5 hits |
| 团队 | L0 跨 user 隔离(同 team) | ISOLATED | 不同 user 互不可见 |
| 团队 | L1 跨 user 隔离(同 team) | ISOLATED | 不同 user 互不可见 |
| 团队 | L3 跨 user(同 team) | **SHARED** | **平台语义:L3 是 (team, agent) 级共享画像** |
| 团队 | Wiki 资产团队内跨 user 可见 | PASS | 团队资产不随 user 隔离 |
| 团队 | Wiki 按团队列举/清理 | PASS | — |

**关键语义发现(已用双盲 user 复核确认)**:

1. **L3 core 的作用域是 (team, agent),不按 user 隔离**——同一 team+agent
   下任何 user 读写的是同一份画像。它实际是"Agent 级共享画像":
   - 适用:团队内该 Agent 需要统一记住的用户群特征(如项目约定)
   - 注意:多用户共用同一 Agent 时,画像互相覆盖,不适合存个人隐私画像
   - 插件影响:prefetch 注入的 `<core_memory>` 对所有用户相同
2. **L1 抽取延迟是分钟级到更久**(RT1 轮次约 1 小时后才出现在 atomic),
   依赖 L1 做实时召回不可靠,L0/L3 是召回主力(插件已按此设计)
3. L0/L1 严格按 (team, agent, user) 隔离;跨 user 共享只能走
   L3(画像)或 Wiki 资产(元数据)

## 3. 稳定性/缺陷发现

| # | 现象 | 根因 | 处置 |
|---|---|---|---|
| 1 | 免费版无法使用官方代理注入路由 | `code 5901` | 改走 /v3 数据面(本插件) |
| 2 | conversation/search 响应键是 `messages`,atomic/search 是 `items` | 接口响应结构不一致 | 插件内 `_items()` 兼容两者 |
| 3 | timestamp 拒绝 `+08:00` 格式 | 服务端仅接受 UTC `Z` | 插件统一用 `time.gmtime()` |
| 4 | **批量删除会话后,该隔离维度新写入消息不再入检索索引** | 疑似免费版管线缺陷 | 换新 `user_id` 恢复;避免批量删除 |
| 5 | 串行 prefetch 最坏 36s,超出 Hermes 8s 预算被静默跳过 | 云实例偶发 522/高延迟 | 三路并行 + 5.5s 单请求超时 + 6.5s 总 deadline |
| 6 | daemon capture 线程在进程退出时可能丢尾部数据 | `-z` 退出路径不保证调用 shutdown() | ✅ **已修复**(2026-08-31):移除插件内第二层异步(queue+daemon worker),`sync_turn` 直接同步上传(4s 超时,单次)——MemoryManager 本身就在后台线程调 sync_turn 并在硬退出前有 ≤5s 的 executor drain,双重异步才是竞态根源。真机两轮连续 `-z` 末轮全部落库。**残余**:实例单请求延迟 >5s 时,末轮仍可能被 MemoryManager 的 drain 预算放弃(hermes 侧硬边界,常态 <1s 零丢失) |
| 11 | L1 空转时 `tdai_memory_search` 工具仍注册给模型,永远返回空(浪费 token、误导模型) | 免费版抽取管线延迟产出 | ✅ **已修复**(2026-08-31):initialize 时探测 `/v3/atomic/query`(limit=1)——空则本会话不注册 L1 工具、prefetch 跳过 atomic 路(并行路 3→2);有产物自动恢复;探测瞬时失败保守视为可用。真机验证:工具按探测结果自动启停,L1 产出后模型可综合 L1+L0 召回 |
| 12 | capture 上传失败(522/网络抖动)时该轮对话静默丢失 | 失败仅记日志,无重试 | ✅ **已修复**(2026-09-01):失败轮次持久化到 `$HERMES_HOME/tdam-cloud-spool/*.json`(上限 200 条,超出丢最旧),下次 initialize 有界重放(≤12s/≤8 条,首败即停);4xx 永久错误不重试直接丢弃。验证:死端点 → spool 落盘,恢复端点 → initialize 重放落库 |
| 7 | 失败回答("没有记录")也会被 capture,污染后续检索排序 | L0 全量上报 | ✅ **已修复**(2026-08-30):双层过滤——capture 侧跳过"记忆未命中"类回复(`_is_negative_memory`,中英文模式,保守不误杀);召回侧从 prefetch/tool 结果中剔除该类条目并按内容去重。实测:污染条目从召回中消失,正确记忆重新浮出 |
| 8 | skill/create 报 `50001 agent_not_found` | agent 未在元数据面注册 | 见 §1 的 meta 面预置流程 |
| 9 | meta 面 team/agent create 不接受自定义 ID | 服务端自动分配 team-…/agt-… | create 后取返回 ID 使用 |
| 10 | knowledge get/delete 报 403 team_id mismatch | 实体归属创建时的 team | 用创建时的 team_id 访问 |

## 4. 环境复现

```bash
# 1. 两台机器安装
pip install hermes-agent
cp -r plugins/memory_tencentdb_cloud ~/.hermes/plugins/
# 2. 按 docs/OPERATIONS.md §4 配置(两机同四元组)
# 3. 四资产能力测试(自动建/清测试资产)
python3 tests/four_assets_test.py
# 4. 三层记忆内核测试(短期/长期/团队,含索引等待约 5 分钟)
python3 tests/three_layers_test.py
# 5. 记忆 benchmark
python3 tests/memory_benchmark.py --runs 6
# 6. 跨机召回:两机分别执行 §2.2 的 A/B 步骤
```
