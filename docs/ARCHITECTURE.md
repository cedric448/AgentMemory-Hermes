# 架构文档

## 1. 总体架构

```
┌─────────────────────────── 测试机 A (Hermes) ───────────────────────────┐
│                                                                          │
│  hermes -z "..." / hermes chat                                           │
│      │                                                                   │
│      ▼                                                                   │
│  AIAgent.run                                                             │
│      │                                                                   │
│      ▼                                                                   │
│  MemoryManager (agent/memory_manager.py)                                 │
│      │  每轮: prefetch_all() → 注入 <memory_context>                     │
│      │        sync_turn()   → 上报本轮对话                               │
│      │        get_tool_schemas() → 注册搜索工具                          │
│      ▼                                                                   │
│  TencentdbCloudProvider (本插件, ~/.hermes/plugins/memory_tencentdb_cloud)│
│      │                                                                   │
│      ├─ prefetch: 3 线程并行                                             │
│      │     ├─ /v3/core/read            (L3 核心记忆/画像)                │
│      │     ├─ /v3/atomic/search        (L1 结构化记忆)                   │
│      │     └─ /v3/conversation/search  (L0 原始对话)                     │
│      │                                                                   │
│      ├─ sync_turn: Queue → daemon 线程 → /v3/conversation/add (L0)       │
│      │                                                                   │
│      └─ handle_tool_call: tdai_memory_search / tdai_conversation_search  │
│                                                                          │
└──────────────────────────────┬───────────────────────────────────────────┘
                               │ HTTPS (Bearer + x-tdai-service-id)
                               ▼
              ┌─────────────────────────────────────────┐
              │  TencentDB Agent Memory 云实例 (v3 数据面) │
              │  https://memory.<region>.tencenttdai.com │
              │                                          │
              │  隔离四元组: team_id / agent_id /         │
              │             user_id / session_id          │
              └─────────────────────────────────────────┘
                               ▲
              ┌────────────────┴────────────────┐
              │  测试机 B (同样的 Hermes + 插件)   │
              │  与测试机 A 共享同一份云端记忆      │
              └─────────────────────────────────┘
```

## 2. 与 Hermes 的集成点

Hermes 通过 `plugins/memory/` 插件发现机制加载外部记忆 provider,本插件是一个
`agent.memory_provider.MemoryProvider` 的标准实现:

| MemoryManager 钩子 | 插件方法 | 行为 |
|---|---|---|
| 会话初始化 | `initialize(session_id)` | 构建 v3 客户端,启动 capture 后台线程 |
| 每轮 API 调用前 | `prefetch(query)` | 并行云端检索,返回注入文本;空串表示无召回 |
| 每轮完成后 | `sync_turn(user, assistant)` | 入队,后台线程上报云端(fire-and-forget) |
| 工具注册 | `get_tool_schemas()` | 2 个搜索工具的 OpenAI function schema |
| 工具调用 | `handle_tool_call(name, args)` | 返回 JSON 结果字符串 |
| 会话切换 | `on_session_switch(new_id)` | 更新内部 session_id |
| 进程退出 | `shutdown()` | 同步兜底 flush 队列中未上报的对话 |

插件发现机制(Hermes 侧 `plugins/memory/__init__.py`):
1. **bundled**: `<site-packages>/plugins/memory/<name>/`
2. **user-installed**: `$HERMES_HOME/plugins/<name>/`(本插件采用,不受 pip 升级影响)

发现要求:`__init__.py` 源码前 8KB 内含 `MemoryProvider` 字样,且存在
`MemoryProvider` 子类或 `register(ctx)` 函数(本插件两者都提供)。

激活:`~/.hermes/config.yaml` 中 `memory.provider: memory_tencentdb_cloud`。

## 3. 云端数据面(v3 API)

认证:
```
Authorization: Bearer <TDAI_MEMORY_API_KEY>
x-tdai-service-id: <TDAI_MEMORY_INSTANCE_ID>   # 即 mem-xxxx 实例 ID
```

隔离模型:v3 强制 session 隔离,L0/L1 请求必须携带
`team_id / agent_id / user_id`(+ `session_id`),缺失返回 422。

本插件用到的端点:

| 端点 | 层级 | 用途 | 响应数据键 |
|---|---|---|---|
| `POST /v3/conversation/add` | L0 | 上报对话消息(messages[].timestamp 需 UTC `Z` 格式) | accepted_ids |
| `POST /v3/conversation/search` | L0 | 关键词检索历史对话 | **messages**(注意不是 items) |
| `POST /v3/conversation/query` | L0 | 按 session 拉取/巡检 | messages |
| `POST /v3/atomic/search` | L1 | 检索结构化长期记忆 | items |
| `POST /v3/atomic/query` | L1 | 列举结构化记忆 | items |
| `POST /v3/core/read` | L3 | 读取用户核心记忆/画像 | content |
| `POST /v3/conversation/delete` | L0 | 批量删除(慎用,见排障) | deleted_count |

> 响应包络统一为 `{code, message, data, request_id}`,code=0 为成功。

## 4. 关键设计决策

### 4.1 prefetch 并行 + 硬时限

Hermes MemoryManager 对外部 provider 的 prefetch 有 **8 秒预算**
(`_EXTERNAL_PREFETCH_TIMEOUT_S = 8.0`),超时该轮召回被静默跳过。
云实例(尤其免费版)延迟波动大(实测 0.3s~36s,偶发 HTTP 522),因此:

- 三个云端调用**并行发出**(单次串行实测最坏 36s)
- 单请求超时 5.5s、不重试(retries=0)
- 全局共享 deadline 6.5s,join 按剩余时间分配
- 任何一路失败/超时只影响该 section,其余照常注入(优雅降级)

### 4.2 capture 异步 + 退出兜底

`sync_turn` 只入队即返回,不阻塞对话;daemon 线程消费 Queue 上报。
`shutdown()` 先 sentinel 结束 worker,再**同步 drain** 队列兜底
—— 否则进程退出时 daemon 线程被强杀,最后几轮对话丢失。

已知残留问题:`hermes -z` 一次性模式下,若进程未走到 `shutdown()`,
最后一轮仍可能丢失(交互式 `hermes` 不受影响)。

### 4.3 召回格式

注入文本结构(拼进本轮上下文):

```
<memory_context source="tencentdb-agent-memory">
<core_memory>…L3 画像/核心记忆…</core_memory>
<long_term_memories>[type | ts] content …</long_term_memories>
<related_conversations>[role | ts] content …</related_conversations>
</memory_context>
```

各 section 均为可选项,无数据不输出。

### 4.4 为什么不用官方 Hermes 插件 / 代理路由

| 方案 | 数据位置 | 依赖 | 免费版可用 |
|---|---|---|---|
| 官方 `memory_tencentdb` 插件 | 本地 (Node Gateway + SQLite) | Node.js sidecar | ✅(但不用云实例) |
| 官方代理注入 `/hermes/<spaceId>` | 云端 | 付费版实例 | ❌ code 5901 |
| **本插件** | **云端** | 无 | ✅ |

## 5. 配置项

全部通过环境变量注入(建议写入 `$HERMES_HOME/.env`):

| 变量 | 必填 | 说明 | 示例 |
|---|---|---|---|
| `TDAI_MEMORY_ENDPOINT` | ✅ | 实例公网地址 | `https://memory.ap-guangzhou.tencenttdai.com` |
| `TDAI_MEMORY_API_KEY` | ✅ | 实例 API Key | `02Q8…`(保密) |
| `TDAI_MEMORY_INSTANCE_ID` | ✅ | 实例 ID,作为 `x-tdai-service-id` | `mem-xxxxxxxx` |
| `TDAI_MEMORY_TEAM_ID` | — | 隔离 team,默认 `team-default` | `team-hermes-test` |
| `TDAI_MEMORY_AGENT_ID` | — | 隔离 agent,默认 `agent-default` | `agent-hermes` |
| `TDAI_MEMORY_USER_ID` | — | 隔离 user,默认 `user-default` | `user-hermes-v2` |

多机共享记忆:两台机器使用**相同的四元组**即可读写同一份记忆;
用不同 `user_id` 则天然按机器/用户隔离。
