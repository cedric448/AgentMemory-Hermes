# AgentMemory-Hermes

将 [腾讯云 TencentDB Agent Memory](https://cloud.tencent.com/document/product/1813/132100) 接入 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 的记忆插件,让 Hermes 获得跨会话、跨机器的云端持久记忆。

## 为什么不是官方插件

官方仓库 (`TencentCloud/TencentDB-Agent-Memory`) 提供的 Hermes 插件 `memory_tencentdb` 采用**本地 Gateway 架构**:Node.js sidecar 在本机跑完整记忆管线,数据存本地 SQLite,并不直连云实例。而本插件:

- **直连托管云实例**的 v3 数据面 (`/v3/*`),零本地依赖(纯 Python 标准库)
- 记忆存在云端,**多台机器共享同一份记忆**
- 无需 Node.js / 无需本地部署 MemoryCore

> 注意:云实例的官方"代理注入"路由(`/hermes/<spaceId>/v1/chat/completions`)需要**付费版**实例;免费版只开放 `/v3` 数据面,这正是本插件使用的接口。

## 功能

| 能力 | 实现方式 | 云端接口 |
|------|----------|----------|
| 对话上报 (L0) | 每轮对话结束后台线程写入,fire-and-forget | `POST /v3/conversation/add` |
| 记忆召回注入 | 每轮对话前并行查 L3 核心记忆 + L1 结构化记忆 + L0 历史,拼装 `<memory_context>` 注入 | `/v3/core/read` `/v3/atomic/search` `/v3/conversation/search` |
| 模型搜索工具 | `tdai_memory_search`(L1)、`tdai_conversation_search`(L0) | 同上 |

## 快速开始

```bash
# 1. 安装 Hermes(python 3.11+)
pip install hermes-agent

# 2. 安装本插件(用户级目录,pip 升级不会覆盖)
mkdir -p ~/.hermes/plugins
cp -r plugins/memory_tencentdb_cloud ~/.hermes/plugins/

# 3. 配置模型与记忆(详见 docs/OPERATIONS.md)
#    ~/.hermes/.env 写入 TDAI_MEMORY_ENDPOINT / TDAI_MEMORY_API_KEY / TDAI_MEMORY_INSTANCE_ID 等
#    ~/.hermes/config.yaml 写入 memory.provider: memory_tencentdb_cloud

# 4. 验证
hermes -z "记住:我的幸运数字是 73"
# 等待 2-3 分钟索引生效后,开新会话:
hermes -z "我的幸运数字是多少?"
```

## 目录结构

```
├── README.md
├── docs/
│   ├── ARCHITECTURE.md      # 架构文档
│   ├── OPERATIONS.md        # 操作文档(安装/配置/运维/排障)
│   └── BENCHMARK.md         # 测试报告与基准数据
├── tests/
│   ├── four_assets_test.py  # 四资产能力测试(Chat Memory/Skill/Wiki/CodeGraph)
│   └── memory_benchmark.py  # 记忆召回 benchmark(延迟/命中率/预算)
└── plugins/memory_tencentdb_cloud/
    ├── __init__.py          # MemoryProvider 实现
    ├── client.py            # v3 数据面 HTTP 客户端
    └── plugin.yaml          # 插件元数据
```

## 免费版实例已知限制

- L1 结构化记忆由云端异步管线抽取,**延迟产出**(开通初期可能持续为空);插件在 initialize 时自动探测,L1 无产物期间 `tdai_memory_search` 工具自动下线、prefetch 跳过 atomic 路,有产物后自动恢复
- L2 场景块只读(数据面 write 不能创建文件);L3 核心记忆可通过 `/v3/core/write` 手动维护
- 检索索引异步,写入后约 **2~3 分钟**才可检索
- 不要对活跃隔离维度批量删除会话(会导致搜索索引失效)
- 实例负载高时偶发 522/高延迟,插件已做超时与降级处理
- Skill 资产需先在元数据面(`/v3/meta/*`)注册 team/agent;详见 `docs/BENCHMARK.md` §1
- Wiki/CodeGraph 免费版仅开放元数据 CRUD,内容检索/图查询需自建 Knowledge Service
