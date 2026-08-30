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

## 7. 日常运维

| 操作 | 命令/方式 |
|---|---|
| 查看插件状态 | `hermes memory status` |
| 健康检查 | `hermes doctor` |
| 关闭外部记忆 | `hermes memory off` |
| 查看运行日志 | `tail -f ~/.hermes/logs/agent.log` |
| 查看错误 | `tail -f ~/.hermes/logs/errors.log` |
| 升级插件 | 覆盖 `~/.hermes/plugins/memory_tencentdb_cloud/` 后重启会话 |
| 云端数据巡检 | 见 §6 的 curl 示例 |

## 8. 排障

### 8.1 召回为空

1. **索引延迟**:写入后 2~3 分钟内检索不到是正常现象,先等待
2. **实例高负载**:免费版偶发 522/慢响应,prefetch 有 6.5s 硬时限,
   超时的 section 会被丢弃(优雅降级)。可稍后重试
3. **隔离维度不匹配**:确认 `.env` 里 team/agent/user 与写入时一致
4. **索引失效**:如果某隔离维度**批量删除过会话**,该维度检索可能永久
   失效(实测),换一个新的 `user_id` 即可恢复——**避免批量删除**

### 8.2 capture 丢失

- `hermes -z` 一次性模式进程退出较快,最后一轮可能未上报(已知残留问题)
- 交互式 `hermes` 不受影响
- 验证方法:对话后用 §6 的 query 接口检查是否落库

### 8.3 插件未被加载

```bash
# 1) 确认目录与文件
ls ~/.hermes/plugins/memory_tencentdb_cloud/
#    应有 __init__.py client.py plugin.yaml

# 2) 确认 config.yaml 激活
grep -A1 "memory:" ~/.hermes/config.yaml

# 3) 确认环境变量(Hermes 运行时加载 .env)
hermes memory status
```

### 8.4 HTTP 错误码

| 码 | 含义 | 处理 |
|---|---|---|
| 422 | 缺 isolation 字段(team/agent/user/session) | 检查 .env 四元组 |
| 401/403 | API Key 无效 | 检查 API Key / service-id |
| 522 | 实例源站超时(免费版高负载) | 重试;插件已自动重试 5xx |
| 5901 | proxy 服务免费版不可用 | 与本插件无关(本插件不走 proxy) |

### 8.5 时间戳格式

`conversation/add` 的 `messages[].timestamp` 只接受 **UTC `Z` 后缀**
的 ISO 格式(如 `2026-08-30T14:00:00Z`);`+08:00` 或无时区格式会返回
400 "Invalid ISO datetime"。插件内部已处理,自行扩展时注意。
