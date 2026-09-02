# 测试报告(Benchmark)

> 测试时间跨度:2026-08-30 ~ 2026-09-01(过程性记录见 [`TESTING_LOG.md`](./TESTING_LOG.md))
> 环境:2 台 TencentOS 4 服务器(本机 43.155.112.112 / 远端 43.134.203.148),Python 3.11.6,
> Hermes Agent 0.19.0(pip),腾讯云 Agent Memory **免费版**(广州),模型 glm-5.3
> 两台 Hermes 共享同一实例、同一隔离四元组(team-hermes-test / agent-hermes / user-hermes-v2)

## 快速结论

| 维度 | 结论 |
|---|---|
| 功能 | 四资产中 Chat Memory(L0/L3)、Skill 全可用;Wiki/CodeGraph 元数据可用;L1 延迟产出;L2 只读;offload 不可用 |
| 召回质量 | 双机 9 用例:本机 **8/9**、远端 **5/9**(差异主因:检索新近度淹没 + 实例延迟) |
| 性能 | prefetch 空闲 0.35~4.1s / 高负载压满 6.5s deadline,0 次超出 Hermes 8s 预算 |
| 平台语义 | L0/L1 按 (team,agent,user) 隔离;**L3 按 (team,agent) 共享**(Agent 级画像);检索索引 2~3 分钟异步生效 |
| 缺陷 | 累计登记 15 项,插件侧 8 项已修复(见 §5),其余为平台限制/模型行为 |

## 1. 测试套件总览

| 脚本 | 覆盖 | 运行方式 |
|---|---|---|
| `tests/four_assets_test.py` | 四资产能力(Chat Memory/Skill/Wiki/CodeGraph),自动建/清元数据资产 | `python3 tests/four_assets_test.py` |
| `tests/three_layers_test.py` | 三层记忆内核(短期/长期/团队),含索引等待约 5 分钟 | `python3 tests/three_layers_test.py` |
| `tests/recall_quality_test.py` | 双机召回质量 9 用例,write/ask 两阶段 | 先 `--phase write`,等 2 分钟,再 `--phase ask --host local/remote` |
| `tests/memory_benchmark.py` | 延迟/命中率/预算 benchmark | `python3 tests/memory_benchmark.py --runs 6` |

跨机测试通过 `REMOTE_SSH` 环境变量驱动远端(如
`export REMOTE_SSH="sshpass -p *** ssh root@<ip>"`)。

## 2. 功能测试

### 2.1 四资产能力(免费版数据面)

官方"四资产"的注入链路走代理服务(免费版禁用,code 5901),本节实测**数据面直接访问**能力:

| 资产 | 能力 | 数据面接口 | 免费版结果 |
|------|------|-----------|-----------|
| **Chat Memory** | L0 对话写入/查询/检索 | `/v3/conversation/add·query·search` | ✅ 全部可用 |
| Chat Memory | L3 核心记忆写/读 | `/v3/core/write·read` | ✅ 可用(手动维护) |
| Chat Memory | L1 结构化记忆抽取 | `/v3/atomic/search·query` | ⚠️ 管线延迟产出(开通数小时后才开始) |
| Chat Memory | L2 场景块 | `/v3/scenario/ls·read·write` | ⚠️ 只能列举;write 不能创建文件(404) |
| Chat Memory | 上下文 offload | `/v3/offload/*` | ❌ 未路由(404) |
| **Skill** | 全生命周期 CRUD | `/v3/skill/create·get·get-by-name·list·search·files/*·versions·export…` | ✅ **16 项接口全部可用** |
| **Wiki** | 元数据 CRUD | `/v3/knowledge/*` (type=wiki) | ✅ 可用(仅元数据) |
| Wiki | 内容检索/页面读取 | Knowledge Service 数据面 | ❌ 需自建 :8421(免费版不含) |
| **CodeGraph** | 元数据 CRUD | `/v3/knowledge/*` (type=code-graph) | ✅ 可用(仅元数据) |
| CodeGraph | 图查询 | Knowledge Service 数据面 | ❌ 需自建 :8421(免费版不含) |

Skill 资产的前置条件与 schema 踩坑(详见 [TESTING_LOG.md](./TESTING_LOG.md) 阶段 4):

- `skill/create` 要求 agent 已在**元数据面**注册(`/v3/meta/*`,实例 API Key 即
  system_admin 身份),否则 `50001 agent_not_found`
- 元数据面 team/agent 由服务端**自动分配 ID**(自定义 ID 被当 name)
- skill `content` 必须带 YAML frontmatter,否则 `42203`
- `get-by-name` 字段名是 `skill_name`;`files/write` 的 `files[].encoding` 必填;
  `files/read` 用单数 `path`(违反均报 40001 zod 校验错)

### 2.2 Chat Memory 端到端

```
单机跨会话:
  A: hermes -z "我叫张伟,我最喜欢的编程语言是 Rust,请记住"
  B: hermes -z "我最喜欢什么编程语言?"        → 准确回答 Rust    ✅

跨机(A 写 → B 召回,两轮验证均 PASS):
  A: hermes -z "我的幸运数字是 73,请记住"     → "已记住"
  B(另一台机器): hermes -z "我的幸运数字是多少?" → "73"           ✅
  A: hermes -z "请记住:我的高铁会员号是 G888888"
  B: hermes -z "请直接输出我的高铁会员号"       → "G888888"       ✅
```

### 2.3 三层记忆内核

| 层 | 检查 | 结果 | 说明 |
|---|---|---|---|
| 短期 | L0 会话内写入/query | PASS | 385~1294ms |
| 短期 | 跨轮 search 召回 | PASS | 90s 索引等待后命中 |
| 短期 | session 隔离 | PASS | query 按 session 精确过滤 |
| 长期 | L1 管线抽取(2 分钟观测窗) | PENDING | 抽取延迟 >2 分钟(实际分钟~小时级) |
| 长期 | L3 core/write + read 回读 | PASS | ~500ms |
| 长期 | 跨会话召回(L0 search) | PASS | 5 hits |
| 团队 | L0/L1 跨 user 隔离(同 team) | ISOLATED | 严格按 (team,agent,user) |
| 团队 | L3 跨 user(同 team) | **SHARED** | 平台语义,见 §4 |
| 团队 | Wiki 资产团队内跨 user 可见/列举/清理 | PASS | 团队资产不随 user 隔离 |

### 2.4 双机召回质量(9 用例)

用例设计:每个事实带**唯一标记**,判分客观化(检查标记出现在回答中;
干扰项用例同时检查错误标记不得出现;否定控制检查不得编造)。

| 用例 | 类型 | 本机 | 远端 |
|---|---|---|---|
| C1 | 精确事实 | PASS | PASS |
| C2 | 多条目-指定 | PASS | PASS(修复后) |
| C3 | 干扰项区分 | PASS | FAIL |
| C4 | 转述召回 | PASS | PASS |
| C5 | 否定控制(不编造) | PASS* | **FAIL ×3(幻觉)** |
| C6 | 时效更新 | PASS | FAIL |
| C7 | 长数字编码 | PASS | FAIL(一次空响应) |
| C8 | 跨层召回(L3) | PASS | PASS |
| C9 | 跨机写入-本机召回 | PASS | PASS |
| **得分** | | **8/9** | **5/9** |

*本机 C5 三次运行中幻觉一次(模型不稳定)。

#### 2.4.1 召回精度结论

**总体:修复后综合精度本机 8/9(89%)、远端 5/9(56%)。瓶颈不在写入层
(可靠性 100%,失败有 spool 兜底),而在检索相关性——免费版 L0 词面检索
相关性弱、近乎按时间排序,是精度的决定性因素。**

分维度精度:

| 维度 | 精度 | 说明 |
|---|---|---|
| 精确事实(C1/C7) | ✅ 稳定 | 唯一标记、长单号都能准确还原 |
| 跨层召回(C8,L3 画像) | ✅ 稳定 | 两机 100%,L3 手动维护的画像最可靠 |
| 跨机召回(C9) | ✅ 稳定 | 共享四元组下,任意机器写入、任意机器可召回 |
| 转述召回(C4) | ✅ 基本稳定 | 依赖字面重叠("香菜"),同义词太远会漏 |
| 多条目区分(C2/C3) | ⚠️ 本机对、远端漏 | 干扰项不串台的前提是答案轮进 top-N |
| 时效更新(C6) | ⚠️ 不稳定 | 取最新值依赖 L0 检索到最新轮次,易被淹没 |
| **否定控制(C5)** | ❌ **远端 3/3 幻觉** | 从未告知的车牌编造出"京A·73V88",73 来自 L3 画像泄漏进幻觉;本机则正确拒绝 |

影响精度的三个因素:

1. **已修复**:搜索工具从未注册(#13,Hermes 在 initialize 前注册工具)——
   修复后远端 4/9 → 5/9
2. **已缓解**:检索新近度淹没(#14,词面分数扁平,对话量增长后写入事实
   跌出 top-N)——assistant 优先注入 + 窗口 30 后,prefetch 事实命中率
   1/5 → 5/7
3. **残留(平台/模型侧)**:免费版检索无语义相关性;模型在无上下文时会
   编造且混入真实记忆碎片——插件侧无法根治

生产建议:

- **可靠区间**:近期(几分钟~几小时)写入的事实 + L3 画像,召回精度高
- **不可靠区间**:大量对话累积后的旧事实(L0 被淹没)、跨会话长尾记忆 →
  应依赖 L1 抽取(云端管线,延迟小时级)或显式写 L3(`core/write`)
- **必须防御**:否定场景的幻觉(远端 100% 编造),建议 system prompt 加
  "记忆中无此信息时明确回答不知道"
- 精度随对话量增长**衰减**,重要事实建议显式写 L3,不要只依赖对话自动捕获

## 3. 性能 Benchmark

### 3.1 prefetch 延迟(`tests/memory_benchmark.py --runs 6`)

| 指标 | 实例空闲 | 实例高负载(522 频发) |
|---|---|---|
| L0 capture(写入) | <1s | median 3.7s / max 12.3s |
| L0 search | <1s | median 3.9s / p95 6.5s / max 19.3s |
| L3 core/read | <0.5s | median 0.9s / max 6.4s |
| **prefetch 端到端** | **0.35~4.1s** | **median=max=6.50s(全部命中 deadline)** |
| 超出 Hermes 8s 预算次数 | 0 | **0(6.5s deadline 生效)** |
| prefetch 非空率 | 100% | 100% |

> 并行化 + 5.5s 单请求超时 + 6.5s 总 deadline 后,即使实例高负载,
> prefetch 也不会被 MemoryManager 的 8s 预算跳过;代价是高负载时
> 部分 section 可能被截断(优雅降级)。

### 3.2 capture

| 指标 | 数值 |
|---|---|
| 对话轮次阻塞 | ≈0(MemoryManager 在自有后台线程调用 sync_turn) |
| 上报时机 | 同步上传(4s 超时),turn 完成即持久化 |
| 失败处理 | 网络类失败落盘 spool,下次会话自动重放(见 §5 #12) |

### 3.3 检索索引生效延迟

写入 → 可检索:实测 100s ~ 3 分钟(多组)。**写入后立即提问检索不到属预期**。

## 4. 平台语义发现(实测确认)

### 4.1 隔离矩阵

| 数据 | 作用域 | 跨 user(同 team) | 跨 agent |
|---|---|---|---|
| L0 conversation | (team, agent, user, session) | 隔离 | 隔离 |
| L1 atomic | (team, agent, user) | 隔离 | 隔离 |
| **L3 core** | **(team, agent)** | **共享** | 隔离 |
| Wiki/Knowledge 元数据 | (team) | 共享 | — |

L3 的共享语义经双盲 user 复核确认(两个全新 user 读到完全相同的画像;
换 agent 为空)。**含义:L3 是 Agent 级团队画像,多用户共用 Agent 时互相
覆盖,不要放个人隐私数据**;插件注入的 `<core_memory>` 对所有用户相同。

### 4.2 检索相关性弱 + 新近度淹没

免费版 L0 检索分数扁平(0.024~0.033,近乎按时间排序),且**提问轮与查询
字面重叠高、排名天然高于答案轮**。实测:写入轮在对话量增长后跌至第 20/28 位,
跌出小窗口后即无法召回——这是双机质量差异(8/9 vs 5/9)的主因。
插件缓解:检索窗口 30 + assistant 消息优先注入 + 封顶 10 行,事实命中率
1/5 → 5/7;根治需依赖 L1(云端抽取)/付费版语义检索。

### 4.3 L1 延迟产出

开通初期 atomic 持续为空数小时,之后开始产出(work_fact 类型,如
"当前项目代号为 ALPHA-77"),单条延迟分钟~小时级。**不可依赖 L1 做实时
召回主力**;L0(近期上下文)+ L3(画像)是召回主力——与插件设计一致。

### 4.4 幻觉观察(模型侧)

否定控制用例("我的车牌号是多少?"——从未告知)在远端 3/3 次编造出
"京A·73V88",**其中 73 来自 L3 共享画像**(幸运数字),即幻觉内容会混合
真实记忆碎片;本机同样提问则正确拒绝。生产建议在 system prompt 明确
"记忆中无此信息时回答不知道"。

## 5. 缺陷登记表

| # | 问题 | 根因 | 状态/处置 |
|---|---|---|---|
| 1 | 免费版无法使用官方代理注入路由 | `code 5901` | 平台限制;改走 /v3 数据面(本插件) |
| 2 | conversation/search 响应键是 `messages`,atomic/search 是 `items` | 接口结构不一致 | ✅ 修复:`_items()` 兼容 |
| 3 | timestamp 拒绝 `+08:00` 格式 | 服务端仅接受 UTC `Z` | ✅ 修复:`time.gmtime()` |
| 4 | 批量删除会话后该隔离维度检索索引永久失效 | 疑似免费版管线缺陷 | ⚠️ 规避:禁止批量删除;换 user_id 恢复 |
| 5 | 串行 prefetch 最坏 36s,超出 Hermes 8s 预算被静默跳过 | 实例偶发 522/高延迟 | ✅ 修复:三路并行 + 5.5s 超时 + 6.5s deadline |
| 6 | `-z` 尾轮对话丢失 | 插件内 queue+worker 与 MemoryManager 后台 sync 双重异步,`os._exit` 时被杀 | ✅ 修复:移除第二层异步,sync_turn 同步上传 |
| 7 | 失败回答("没有记录")被 capture,污染检索排序 | L0 全量上报 + 词面重叠 | ✅ 修复:双层过滤(capture 跳过 + 召回剔除/去重) |
| 8 | skill/create 报 `50001 agent_not_found` | agent 未在元数据面注册 | ✅ 流程:meta 面预置(§2.1) |
| 9 | meta 面 team/agent create 不接受自定义 ID | 服务端自动分配 team-…/agt-… | 📝 记录:create 后取返回 ID |
| 10 | knowledge get/delete 报 403 team mismatch | 实体归属创建时的 team | 📝 记录:用创建时 team_id |
| 11 | L1 空转期 `tdai_memory_search` 注册给模型,永远返回空 | 抽取管线延迟产出 | ✅ 修复:initialize 探测自适应门控 |
| 12 | capture 上传失败即永久丢失 | 无重试机制 | ✅ 修复:spool 持久化 + 有界重放 |
| 13 | 搜索工具从未注册(`registered (0 tools)`) | Hermes 在 initialize 前注册工具 | ✅ 修复:get_tool_schemas 惰性构建 client |
| 14 | 检索新近度淹没,写入事实跌出 top-N(召回随量衰减) | 免费版检索相关性弱 | ⚠️ 缓解:窗口 30 + assistant 优先 + 封顶;根治依赖 L1/付费版 |
| 15 | 否定控制偶发/远程稳定幻觉(编造车牌,混入 L3 记忆碎片) | 模型行为(无上下文时编造) | 📝 记录:建议 system prompt 兜底声明 |

修复均经真机验证,过程详见 [TESTING_LOG.md](./TESTING_LOG.md)。

## 6. 环境复现

```bash
# 1. 两台机器安装
pip install hermes-agent
cp -r plugins/memory_tencentdb_cloud ~/.hermes/plugins/
# 2. 按 docs/OPERATIONS.md §4 配置(两机同四元组)
# 3. 四资产能力测试(自动建/清测试资产)
python3 tests/four_assets_test.py
# 4. 三层记忆内核测试(含索引等待约 5 分钟)
python3 tests/three_layers_test.py
# 5. 双机召回质量(write 后等 2 分钟再 ask;跨机需 REMOTE_SSH)
python3 tests/recall_quality_test.py --phase write
python3 tests/recall_quality_test.py --phase ask --host local
python3 tests/recall_quality_test.py --phase ask --host remote
# 6. 记忆 benchmark
python3 tests/memory_benchmark.py --runs 6
```
