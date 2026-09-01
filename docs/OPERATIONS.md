# 操作文档(安装 / 配置 / 验证 / 运维 / 排障)

> 约定:`$HERMES_HOME` 默认 `~/.hermes`。文中所有密钥均为占位符,请替换为你自己的凭据,**不要提交真实密钥到任何仓库**。

## 1. 前置条件

| 组件 | 要求 | 检查命令 |
|---|---|---|
| OS | Linux(已在 TencentOS 4 验证) | — |
| Python | 3.11+ | `python3 --version` |
| Hermes Agent | 0.19+ | `hermes --version` |
| 腾讯云 Agent Memory 实例 | 已开通,拿到实例 ID + API Key + 公网地址 | 控制台查看 |
| 网络 | 能访问实例公网地址 | `curl -sI $ENDPOINT` |

## 2. 安装 Hermes

```bash
pip install hermes-agent
hermes --version   # 确认安装成功
```

## 3. 安装插件

```bash
mkdir -p ~/.hermes/plugins
cp -r plugins/memory_tencentdb_cloud ~/.hermes/plugins/

# 验证插件可被发现(应看到 memory_tencentdb_cloud 出现在列表中)
python3 - <<'EOF'
import sys; sys.path.insert(0, '/usr/local/lib/python3.11/site-packages')
from plugins.memory import discover_memory_providers
print(discover_memory_providers())
EOF
```

> 注意:此处 `available: False` 是正常的——裸 Python 不会加载 `.env`,
> Hermes 运行时会加载,`hermes memory status` 显示 `available ✓` 即可。

## 4. 配置

### 4.1 模型(~/.hermes/.env)

Hermes 内置 `zai`(GLM)provider,支持 OpenAI 兼容自定义网关:

```bash
GLM_API_KEY=<你的 tokenhub API Key>
GLM_BASE_URL=https://tokenhub.tencentmaas.com/v1   # 或任意 OpenAI 兼容网关
```

~/.hermes/config.yaml:

```yaml
model:
  provider: zai
  default: glm-5.3        # 与网关侧模型名一致
```

### 4.2 记忆(~/.hermes/.env)

```bash
TDAI_MEMORY_ENDPOINT=https://memory.ap-guangzhou.tencenttdai.com
TDAI_MEMORY_API_KEY=<实例 API Key>
TDAI_MEMORY_INSTANCE_ID=mem-xxxxxxxx
TDAI_MEMORY_TEAM_ID=team-hermes-test
TDAI_MEMORY_AGENT_ID=agent-hermes
TDAI_MEMORY_USER_ID=user-hermes-v2
```

chmod 600 ~/.hermes/.env

### 4.3 激活 provider(~/.hermes/config.yaml)

```yaml
memory:
  provider: memory_tencentdb_cloud
```

### 4.4 确认

```bash
hermes memory status
#  Provider:  memory_tencentdb_cloud
#  Plugin:    installed ✓
#  Status:    available ✓
```

## 5. 多机部署(共享记忆)

在第二台机器重复 2~4 步,**使用完全相同的四元组**
(team/agent/user id),两台机器即共享同一份云端记忆。
若希望按机器隔离,改用不同 `TDAI_MEMORY_USER_ID`。

局域网内可脚本化批量部署,例如:

```bash
ssh root@<remote> 'mkdir -p ~/.hermes/plugins'
scp -r plugins/memory_tencentdb_cloud root@<remote>:~/.hermes/plugins/
scp ~/.hermes/config.yaml ~/.hermes/.env root@<remote>:~/.hermes/
```

## 6. 功能验证

```bash
# 会话 A:写入一条事实
hermes -z "记住:我的幸运数字是 73"

# ⏳ 等待 2~3 分钟(云端检索索引异步生效)

# 会话 B:验证召回(注意是新会话)
hermes -z "我的幸运数字是多少?"
# 期望输出包含 73
```

也可以直接用 curl 检查云端状态:

```bash
ENDPOINT=https://memory.ap-guangzhou.tencenttdai.com
API_KEY=<实例 API Key>
INSTANCE_ID=mem-xxxxxxxx

# 查看最近上报的对话
curl -s -X POST "$ENDPOINT/v3/conversation/query" \
  -H "Authorization: Bearer $API_KEY" \
  -H "x-tdai-service-id: $INSTANCE_ID" \
  -H "Content-Type: application/json" \
  -d '{"team_id":"team-hermes-test","agent_id":"agent-hermes",
       "user_id":"user-hermes-v2","limit":20}' | jq

# 关键词检索
curl -s -X POST "$ENDPOINT/v3/conversation/search" \
  -H "Authorization: Bearer $API_KEY" \
  -H "x-tdai-service-id: $INSTANCE_ID" \
  -H "Content-Type: application/json" \
  -d '{"team_id":"team-hermes-test","agent_id":"agent-hermes",
       "user_id":"user-hermes-v2","query":"幸运数字","limit":5}' | jq
```

## 7. 三层记忆模型与使用手册

> 语义均经真实实例实测确认(测试脚本 `tests/three_layers_test.py`,
> 结果详见 `docs/BENCHMARK.md` §2.8)。

### 7.1 三层速查表

| 层 | 数据面 | 作用域 | 插件如何用 | 适合记什么 |
|---|---|---|---|---|
| **短期记忆** | L0 conversation | (team, agent, user, **session**) | 每轮自动上报;召回时按当前 query 检索历史注入 `<related_conversations>`;提供 `tdai_conversation_search` 工具 | "刚才说过的话"、本次/近期会话细节 |
| **长期记忆** | L1 atomic + L3 core | L1: (team, agent, user);**L3: (team, agent),团队内共享** | L1 由云端管线异步抽取(分钟级~小时级延迟);L3 画像注入 `<core_memory>`(所有用户相同);L1 有产物时自动提供 `tdai_memory_search` 工具 | 稳定的偏好、事实、约定(L1 自动抽 / L3 手动精修) |
| **团队记忆** | 团队共享层 | (team) 级 | Wiki 知识资产(元数据 CRUD)不随 user 隔离,团队内共享 | 团队文档、共享约定(注:内容检索需自建 Knowledge Service) |

隔离语义实测结论:

- **L0 / L1**:严格按 (team, agent, user) 隔离——不同用户互不可见
- **L3 core**:按 (team, agent) 共享——同团队同 Agent 的所有用户读写同一份画像
- **Wiki/Knowledge**:按 team 共享的资产(元数据层)
- 想给不同用户独立记忆:用不同 `TDAI_MEMORY_USER_ID`;想让团队共享画像:共用同一 agent_id 即可(L3 天然共享)

### 7.2 日常使用(无需任何操作)

插件自动完成全部三层读写:

1. 每轮对话结束 → 自动上报 L0(失败自动落盘重试)
2. 每轮对话开始 → 自动并行召回(L3 画像 + L1 结构化记忆 + L0 历史)注入上下文
3. Agent 可按需调用搜索工具主动查记忆
4. 云端管线异步把 L0 对话提炼为 L1 结构化记忆(自动)

只需要记住两条:**问过的东西过几分钟就能被新会话想起**;
**画像(L3)是全团队 Agent 共享的,别放私人信息**。

### 7.3 手动管理各层(可选)

```bash
ENDPOINT=https://memory.ap-guangzhou.tencenttdai.com
API_KEY=<实例 API Key>; INSTANCE_ID=mem-xxxxxxxx
AUTH=(-H "Authorization: Bearer $API_KEY" -H "x-tdai-service-id: $INSTANCE_ID" -H "Content-Type: application/json")

# ── 短期记忆(L0)─────────────────────────────
# 查看某会话全部消息
curl -s -X POST "$ENDPOINT/v3/conversation/query" "${AUTH[@]}" \
  -d '{"team_id":"team-hermes-test","agent_id":"agent-hermes",
       "user_id":"user-hermes-v2","session_id":"<会话ID>","limit":20}' | jq

# 检索历史对话
curl -s -X POST "$ENDPOINT/v3/conversation/search" "${AUTH[@]}" \
  -d '{"team_id":"team-hermes-test","agent_id":"agent-hermes",
       "user_id":"user-hermes-v2","query":"关键词","limit":5}' | jq

# ── 长期记忆(L1 抽取产物)──────────────────────
# 列出云端提炼出的结构化记忆
curl -s -X POST "$ENDPOINT/v3/atomic/query" "${AUTH[@]}" \
  -d '{"team_id":"team-hermes-test","agent_id":"agent-hermes",
       "user_id":"user-hermes-v2","limit":20}' | jq

# ── 长期记忆(L3 共享画像)──────────────────────
# 读/写团队共享画像(注意:同 team+agent 的所有用户共用!)
curl -s -X POST "$ENDPOINT/v3/core/read" "${AUTH[@]}" \
  -d '{"team_id":"team-hermes-test","agent_id":"agent-hermes","user_id":"user-hermes-v2"}' | jq
curl -s -X POST "$ENDPOINT/v3/core/write" "${AUTH[@]}" \
  -d '{"team_id":"team-hermes-test","agent_id":"agent-hermes","user_id":"user-hermes-v2",
       "content":"# 用户画像\n- 喜欢的编程语言: Rust\n"}' | jq

# ── 团队记忆(Wiki 资产)──────────────────────
# 创建团队知识源(元数据;内容检索需自建 Knowledge Service)
curl -s -X POST "$ENDPOINT/v3/knowledge/create" "${AUTH[@]}" \
  -d '{"knowledge_id":"team-wiki-1","type":"wiki","name":"团队 Wiki",
       "service_url":"'"$ENDPOINT"'/v3","team_id":"team-hermes-test"}' | jq
```

### 7.4 三层相关注意事项

- **短期**:检索索引异步,写入后 2~3 分钟才可召回;"查无记录"类回复
  会被插件过滤,不会污染后续检索
- **长期**:L1 抽取延迟大(分钟级~小时级),不要依赖它做实时召回;
  L3 是团队共享的,写入前想清楚受众;`tdai_memory_search` 工具只在
  L1 有产物时才会出现在 Agent 的工具列表里
- **团队**:免费版 Wiki/CodeGraph 仅元数据 CRUD;内容级团队共享需要
  付费版代理路由或自建 Knowledge Service

## 8. 日常运维

| 操作 | 命令/方式 |
|---|---|
| 查看插件状态 | `hermes memory status` |
| 健康检查 | `hermes doctor` |
| 关闭外部记忆 | `hermes memory off` |
| 查看运行日志 | `tail -f ~/.hermes/logs/agent.log` |
| 查看错误 | `tail -f ~/.hermes/logs/errors.log` |
| 升级插件 | 覆盖 `~/.hermes/plugins/memory_tencentdb_cloud/` 后重启会话 |
| 云端数据巡检 | 见 §6 的 curl 示例 |

## 9. 排障

### 9.1 召回为空

1. **索引延迟**:写入后 2~3 分钟内检索不到是正常现象,先等待
2. **实例高负载**:免费版偶发 522/慢响应,prefetch 有 6.5s 硬时限,
   超时的 section 会被丢弃(优雅降级)。可稍后重试
3. **隔离维度不匹配**:确认 `.env` 里 team/agent/user 与写入时一致
4. **索引失效**:如果某隔离维度**批量删除过会话**,该维度检索可能永久
   失效(实测),换一个新的 `user_id` 即可恢复——**避免批量删除**

### 9.2 capture 丢失

- 上传失败(网络/5xx/超时/429)的轮次会持久化到
  `~/.hermes/tdam-cloud-spool/*.json`,下次会话 initialize 时自动重放
  (有界:≤12s / ≤8 条,遇到失败即停,下个会话继续)
- 4xx 永久错误(除 429)不重试,直接丢弃并记日志
- spool 目录超过 200 条会丢弃最旧(实例长期不可用时兜底)
- `hermes -z` 模式下,实例单请求延迟 >5s 时末轮可能被 MemoryManager
  的退出 drain 预算放弃(少见;常态零丢失)
- 验证方法:对话后用 §6 的 query 接口检查是否落库

### 9.3 插件未被加载

```bash
# 1) 确认目录与文件
ls ~/.hermes/plugins/memory_tencentdb_cloud/
#    应有 __init__.py client.py plugin.yaml

# 2) 确认 config.yaml 激活
grep -A1 "memory:" ~/.hermes/config.yaml

# 3) 确认环境变量(Hermes 运行时加载 .env)
hermes memory status
```

### 9.4 HTTP 错误码

| 码 | 含义 | 处理 |
|---|---|---|
| 422 | 缺 isolation 字段(team/agent/user/session) | 检查 .env 四元组 |
| 401/403 | API Key 无效 | 检查 API Key / service-id |
| 522 | 实例源站超时(免费版高负载) | 重试;插件已自动重试 5xx |
| 5901 | proxy 服务免费版不可用 | 与本插件无关(本插件不走 proxy) |

### 9.5 时间戳格式

`conversation/add` 的 `messages[].timestamp` 只接受 **UTC `Z` 后缀**
的 ISO 格式(如 `2026-08-30T14:00:00Z`);`+08:00` 或无时区格式会返回
400 "Invalid ISO datetime"。插件内部已处理,自行扩展时注意。
