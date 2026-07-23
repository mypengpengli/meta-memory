# Hosted Meta Memory：远端 Agent 与中央共享记忆

Hosted 模式适合运行在其他电脑、云环境、手机或机器人上的 Agent。只有一台
服务器直接拥有 SQLite 和资产目录；其他设备通过 HTTPS 使用生成的 Skill 和
launcher，不挂载、不复制、也不直接打开数据库文件。

要在云服务器直接使用仓库提供的 Docker/Compose、唯一 worker、自动备份和 Caddy
HTTPS，请从 [Docker 云端部署](container-deployment.md) 开始；本文继续说明协议、
身份边界和所有远端命令。

## Agent 与服务器分别需要什么

远端 Agent 宿主必须能够：

1. 加载安装目录中的 `SKILL.md`；
2. 执行本机 launcher 并读取 JSON 输出；
3. 从环境变量读取 Token；
4. 为一个宿主会话保留稳定 `session_id`；
5. 为每个并发回合保留独立 `turn_id`、请求文件和完整回答文件；
6. 访问服务器的 HTTPS 地址。

生成的客户端已经实现 HTTP、幂等键、精确回答校验、JSON 写入 outbox 和分块
上传。宿主不需要自己拼 HTTP 请求。服务器需要持续运行 `meta-memory serve`，
并且只有服务器安装 Heartbeat/Dream 计划任务。

## 先确定不会变化的身份

| 字段 | 含义 | 规则 |
| --- | --- | --- |
| `profile_id` | 这一套记忆数据的所有者 | 从 `shared init` 输出复制，不自行取名 |
| `agent_id` | 一个真实 Agent/设备身份 | 每个 Agent 唯一，服务器与远端完全一致 |
| `workspace_id` | 项目、设备或长期工作区 | 显式给出；远端绝不能依赖服务器当前目录 |
| `subject_id` | 此次交互主要服务的人或对象 | 例如 `person:owner`；默认值写入远端配置 |
| `audience_id` | 谁可以看到一类共享信息 | 从 `shared init` 输出复制 |
| `channel_id` | 活动、状态、地图和空间观察的地址 | 从 `shared init` 输出复制；可选 |
| `token_env` | 保存 Token 的环境变量名 | 配置中只写变量名，不写 Token 值 |

一个 Token 可以允许多个 `subject_ids`，例如家庭机器人可被允许服务家长和孩子；
远端安装仍选择一个默认 `subject_id`。只有当服务器绑定明确列出另一个 subject
时，Agent 才可在单条命令中显式覆盖为该 subject。不要覆盖配置固定的
`agent_id`、`workspace_id`、`audience_id` 或 `channel_id`，也不要猜测家庭成员 ID。

`channel_id` 是可选的：不配置时，普通 `before/after`、项目记忆和 workspace
范围资产仍可工作，但 `before.shared_context` 为空，活动、状态、地图和空间观察
也没有发布目标。需要共享世界信息时，请管理员创建 channel 后重新运行
`install-remote-agent`；不要让 Agent 临时编造一个 channel。

## 1. 在唯一服务器上初始化

本节是**非 Docker 部署**的裸 Python 替代流程。已经使用 Compose 的服务器应跳过
本节，不要在宿主机再启动第二个 API 或计划任务，也不要直接打开 `runtime/data`；
管理方式见 [Docker 云端部署](container-deployment.md)。

不使用 Docker 时，安装 Python 3.10+ 和 Meta Memory，然后初始化服务端存储：

```bash
git clone https://github.com/mypengpengli/meta-memory.git
cd meta-memory
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
meta-memory setup --name "Family memory" --maintenance yes --dream yes --non-interactive
```

Windows PowerShell：

```powershell
git clone https://github.com/mypengpengli/meta-memory.git
Set-Location meta-memory
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install .
meta-memory setup --name "Family memory" --maintenance yes --dream yes --non-interactive
```

创建仅限所列 Agent 的家庭 audience/channel。使用全局 `--json` 便于准确复制
返回值：

```bash
meta-memory --json shared init --type household --key home \
  --label "Family home" --restricted \
  --member-agent home-robot --member-agent family-planner
```

Windows PowerShell：

```powershell
meta-memory --json shared init --type household --key home `
  --label "Family home" --restricted `
  --member-agent home-robot --member-agent family-planner
```

记录输出中的三个真实值：

- `audience.profile_id`
- `audience.audience_id`
- `channel.channel_id`

不要把示例文本 `family-profile` 或 `<channel-id>` 当作真实 ID。

为后续命令设置变量（把引号内文本替换为刚才的输出）：

```bash
PROFILE_ID='paste audience.profile_id here'
AUDIENCE_ID='paste audience.audience_id here'
CHANNEL_ID='paste channel.channel_id here'
```

```powershell
$profileId = 'paste audience.profile_id here'
$audienceId = 'paste audience.audience_id here'
$channelId = 'paste channel.channel_id here'
```

## 2. 生成服务器 Agent 绑定文件

推荐使用命令生成，不需要手写 JSON。下面把机器人允许为家长和孩子服务，并把
上一步的 audience 与 channel 都加入允许列表：

```bash
meta-memory init-agents-file \
  --output "$HOME/.meta-memory/agents.json" \
  --agent-id home-robot \
  --profile-id "$PROFILE_ID" \
  --workspace-id home-robot-workspace \
  --subject-id person:owner \
  --subject-id person:child \
  --audience-id "$AUDIENCE_ID" \
  --audience-id "$CHANNEL_ID" \
  --token-env META_MEMORY_TOKEN_HOME_ROBOT
```

Windows PowerShell：

```powershell
meta-memory init-agents-file `
  --output "$HOME\.meta-memory\agents.json" `
  --agent-id home-robot `
  --profile-id $profileId `
  --workspace-id home-robot-workspace `
  --subject-id person:owner `
  --subject-id person:child `
  --audience-id $audienceId `
  --audience-id $channelId `
  --token-env META_MEMORY_TOKEN_HOME_ROBOT
```

为第二个 Agent 再运行一次，使用新的 `agent_id`、workspace 和 Token 变量名；命令
会保留已有 Agent。更新同名 Agent 时才加 `--replace-agent`。仓库中的
`extras/http/agents.example.json` 也可作为人工编辑参考：Bash 使用 `cp`，
PowerShell 使用 `Copy-Item`，其中 `profile_id` 必须替换为 `shared init` 输出。

### Token 必须两端同值

为每个 Agent 生成一个不同的随机值。`agents.json` 和远端配置都只保存变量名；
Token 的实际值必须在服务器进程与对应远端 Agent 进程中完全相同。

Bash 当前会话示例：

```bash
TOKEN_VALUE="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export META_MEMORY_TOKEN_HOME_ROBOT="$TOKEN_VALUE"
```

Windows PowerShell 当前会话示例：

```powershell
$tokenValue = python -c "import secrets; print(secrets.token_urlsafe(48))"
$env:META_MEMORY_TOKEN_HOME_ROBOT = $tokenValue
```

把同一个值设置到远端 Agent 的服务环境，再重启两端进程。上面的设置只对当前
终端及其子进程有效；作为系统服务运行时，请在服务管理器的环境配置中持久化。
不要把 Token 放入 Skill、launcher 参数、日志、聊天、地图 metadata 或记忆。

## 3. 验证服务器并发布 HTTPS

纯服务器不需要安装本地 Codex/Claude Skill。请用服务器模式 Overview，避免普通
Overview 因“没有本地 Agent 激活”而给出误导性的 `NEEDS_ACTION`：

```bash
meta-memory overview --server --agents-file "$HOME/.meta-memory/agents.json"
meta-memory schedule status
```

本机 HTTP 配合 Caddy、Nginx、云负载均衡或私有网络 HTTPS 网关是常见部署：

```bash
meta-memory serve --agents-file "$HOME/.meta-memory/agents.json" \
  --host 127.0.0.1 --port 8765
```

Windows PowerShell：

```powershell
meta-memory overview --server --agents-file "$HOME\.meta-memory\agents.json"
meta-memory serve --agents-file "$HOME\.meta-memory\agents.json" `
  --host 127.0.0.1 --port 8765
```

让公开的 `https://memory.example.com` 反向代理到
`http://127.0.0.1:8765`。也可以直接提供证书：

```bash
meta-memory serve --agents-file "$HOME/.meta-memory/agents.json" \
  --host 0.0.0.0 --port 8765 \
  --tls-cert fullchain.pem --tls-key privkey.pem
```

远端客户端只允许 HTTPS；仅 `localhost` 开发可使用 HTTP。默认单个资产上限为
64 MiB，如确实需要更大的点云或地图，可显式设置如 `--max-asset-mb 1024`；上传
仍会分块。不要让多台设备通过共享盘直接打开同一个 SQLite 文件。

## 4. 在远端电脑安装生成 Skill

远端电脑也要安装同一版本的 `meta-memory`，但不运行 `setup` 或服务端计划任务。
请把包装进一个持久虚拟环境；安装器会固定当前 Python，不能只依赖“恰好在源码
目录中所以可以 import”。

```bash
python -m venv "$HOME/.venvs/meta-memory"
source "$HOME/.venvs/meta-memory/bin/activate"
python -m pip install --upgrade pip
python -m pip install "git+https://github.com/mypengpengli/meta-memory.git"
meta-memory --version
```

```powershell
python -m venv "$HOME\.venvs\meta-memory"
& "$HOME\.venvs\meta-memory\Scripts\Activate.ps1"
python -m pip install --upgrade pip
python -m pip install "git+https://github.com/mypengpengli/meta-memory.git"
meta-memory --version
```

然后仍在这个已安装环境中生成非密钥 Skill、配置和 launcher：

```bash
AUDIENCE_ID='paste audience.audience_id here'
CHANNEL_ID='paste channel.channel_id here'

meta-memory install-remote-agent \
  --agent-id home-robot \
  --skill-dir "$HOME/.robot/skills" \
  --server-url https://memory.example.com \
  --workspace-id home-robot-workspace \
  --subject-id person:owner \
  --audience-id "$AUDIENCE_ID" \
  --channel-id "$CHANNEL_ID" \
  --token-env META_MEMORY_TOKEN_HOME_ROBOT

export META_MEMORY_TOKEN_HOME_ROBOT='<the-exact-same-token-value-as-server>'
```

Windows PowerShell：

```powershell
$audienceId = 'paste audience.audience_id here'
$channelId = 'paste channel.channel_id here'

meta-memory install-remote-agent `
  --agent-id home-robot `
  --skill-dir "$HOME\.robot\skills" `
  --server-url https://memory.example.com `
  --workspace-id home-robot-workspace `
  --subject-id person:owner `
  --audience-id $audienceId `
  --channel-id $channelId `
  --token-env META_MEMORY_TOKEN_HOME_ROBOT

$env:META_MEMORY_TOKEN_HOME_ROBOT = '<the-exact-same-token-value-as-server>'
```

让宿主加载 `<skill-dir>/meta-memory-remote/SKILL.md`，重启 Agent，然后使用安装
输出中的精确 launcher（下文记作 `<launcher>`）：

```text
<launcher> status
```

`status` 成功只证明连接和身份。完成一个真实用户回合后，再要求
`lifecycle_state: active`、`local_outbox_pending: 0`、
`local_outbox_corrupt: 0`，并检查兼容字段
`last_before`/`last_after`（旧客户端可能显示 `last_before_at`/`last_after_at`）均晚于
本次安装时间，才算端到端启用。

## 远端每回合的严格协议

```text
预先保存 turn_id → before → 完整起草但不发送 → after → 发送原文件
```

每个并发回合先生成并持久化一个 UUID `turn_id`，然后传给 `before`；不要等网络
成功后才决定 Turn ID：

```text
<launcher> before --turn-id <preallocated-uuid> \
  --session-id <stable-host-conversation-id> --query-file <unique-request-file>
<launcher> after --turn-id <same-uuid> --assistant-file <unique-answer-file>
```

`session_id` 在同一宿主对话中保持不变；并发回合必须使用不同 UUID 和文件。
回答必须先完整写入 UTF-8 文件，不要流式发送。解析 JSON `status`，不要只看进程
退出码：

| 结果 | 动作 |
| --- | --- |
| `before: ok` | 使用有界上下文后起草 |
| `before: degraded` + `durability: local_outbox` | 无召回继续起草；保留同一 Turn，等待恢复 |
| `after: ok` 或服务器 `spooled` | 发送回答文件原文 |
| `after: local_outbox` | 原回答已在本机持久化；发送原文，联网后运行 `recovery` |
| `semantic_error` / `client_error` | 不发送、不改写、不另建 Turn；先修正身份或协议 |
| `recovery: deferred` | 网络仍不可用，保留所有文件稍后重试 |
| `recovery: needs_action` | 有被 4xx 阻塞的项目，需要管理员修正配置/身份 |

退出码 `0` 仍可能对应 `degraded`、`local_outbox`、`deferred` 或 `needs_action`，所以
必须读取 JSON；退出码 `2` 表示客户端/语义错误，`3` 表示未被耐久写入流程接住的
连接失败。启动时和网络恢复时运行 `<launcher> recovery`。在待处理数归零前保留
原始请求与回答文件；绝不能用另一份文本覆盖同一 Turn。

launcher 启动/import 失败或任何非 JSON 输出都不是 `degraded`：此时必须先修复
远端包安装或 launcher，不能继续起草。若 `status` 报告 corrupt、foreign-origin
或 identity mismatch，本地记录会保留供人工处理；不要手改 receipt 强制重放。

`activity`、`state`、`observe` 和 `map put` 也是幂等的耐久 JSON 写入；网络确认
不明确时会进入同一 outbox。`recovery` 按顺序重放这些 JSON 操作。

## 共享活动、状态和公开读取命令

先用公开读取命令确认 launcher 实际能看到哪个 channel：

```text
<launcher> shared channels
<launcher> shared feed --limit 20
<launcher> shared states --subject-id person:child --state-key last_seen --limit 20
```

只发布其他 Agent 真正需要的结果，不发布每个工具调用或传感器采样：

```text
<launcher> activity --session-id <conversation-id> \
  --kind household --summary "Refrigerator is not cooling" \
  --source-ref robot:diagnostic-42 \
  --occurred-at <ISO-8601-observed-at> --confidence 0.98

<launcher> state --session-id <conversation-id> \
  --subject-id person:child --state-key last_seen \
  --summary "Child last seen at playground entrance" \
  --source-ref robot-camera:event-43 --confidence 0.92 \
  --observed-at <ISO-8601-observed-at> \
  --valid-until <ISO-8601-expiry-after-a-short-window>
```

时间值必须包含时区，例如 `YYYY-MM-DDTHH:MM:SS+08:00` 或 UTC 的
`YYYY-MM-DDTHH:MM:SSZ`。人物位置等易变信息应使用短有效期；过期记录不会作为
当前状态返回，历史仍可显式查询。若 `person:child` 不在该 Token 的
`subject_ids` 中，服务器会拒绝，而不是扩大权限。

## 图片、地图和空间语义

原始图片、视频、点云和栅格使用资产接口，不要转成 base64 塞入普通记忆：

```text
<launcher> asset upload --file <room-image-or-map-file> \
  --media-type image/jpeg --metadata-file <asset-metadata.json>
<launcher> asset list --media-type image/jpeg --limit 20
<launcher> asset get --asset-id <asset-id>
<launcher> asset download --asset-id <asset-id> --output <local-file>
```

`asset-metadata.json` 必须是 JSON 对象，例如：

```json
{
  "capture_device": "home-robot-front-camera",
  "purpose": "room survey"
}
```

上传中断后保留未修改的原文件，再运行完全相同的 `asset upload --file ...`
命令；客户端会使用本地 receipt 续传已有分块。不要在续传前改名、编辑或覆盖该
文件。普通 `recovery` 重放 JSON outbox；资产续传由重复上传命令完成。

一个可用的 `map-manifest.json` 最小结构如下；`metadata` 必须是对象：

```json
{
  "map_id": "home-floor-1",
  "coordinate_frame": "map",
  "name": "Home first floor",
  "asset_id": "<asset-id>",
  "captured_at": "<ISO-8601-capture-time>",
  "metadata": {
    "format": "occupancy-grid",
    "resolution_m": 0.05
  }
}
```

```text
<launcher> map put --payload-file <map-manifest.json>
<launcher> map list
<launcher> map get --map-id home-floor-1
```

同一 `map_id` 的每次更新会创建不可变的新版本。不要把不同 channel 的地图复用
为同一 `map_id`。

`objects.json` 必须是数组（即使只有一个对象），而不是包含 `objects` 键的对象：

```json
[
  {"label": "water", "confidence": 0.94, "region": "under-sink"}
]
```

记录并查询 Agent 已经得到的空间语义：

```text
<launcher> observe --session-id <conversation-id> \
  --content "Water visible under the kitchen sink" \
  --source-ref robot-camera:event-44 \
  --observed-at <ISO-8601-observed-at> \
  --map-id home-floor-1 --asset-id <asset-id> \
  --location-id kitchen-sink --location-text "Kitchen, under sink" \
  --objects-file <objects.json> --confidence 0.94

<launcher> spatial list --limit 20
<launcher> spatial search "water sink" --limit 10
<launcher> spatial get --observation-id <observation-id>
```

Meta Memory 不执行图片理解、OCR、物体识别、SLAM、地图融合或路径规划。机器人或
上游 Agent 先完成这些工作，再把结果、时间、来源、置信度及资产链接写入这里。
它是共享记忆与证据层，不是实时感知或导航系统。

## 服务端运维

```bash
meta-memory overview --server --agents-file "$HOME/.meta-memory/agents.json"
meta-memory doctor
meta-memory schedule status
meta-memory dream status
meta-memory shared expire
meta-memory backup --output "$HOME/meta-memory-server.zip"
```

在远端 Agent：

```text
<launcher> status
<launcher> recovery
```

如果 outbox 仍有 pending，保留原文件并稍后重试；如果有 blocked，先检查 Token
对应的 `agent_id`、workspace、subjects、audience/channel 和服务器绑定，再运行
恢复。修改 Token 时必须在服务器和对应远端同时设置同一个新值并重启两边。

## HTTP 路由概览

正常集成应使用生成 launcher。需要审计部署时，可按下表确认服务版本面：

| 用途 | 路由 |
| --- | --- |
| 健康 | `GET /healthz` |
| Turn | `POST /v1/turns/before`、`POST /v1/turns/{id}/after`、`POST /v1/turns/{id}/touch` |
| 恢复与状态 | `POST /v1/recovery/replay`、`GET /v1/agent/status` |
| 主动记忆 | `POST /v1/remember` |
| channel/activity/state | `GET/POST /v1/channels`、`/v1/activities`、`/v1/states` |
| 资产 | `GET/POST /v1/assets`、`/v1/assets/{id}`、`/v1/assets/uploads/...` |
| 地图和观察 | `GET/POST /v1/maps`、`GET/POST /v1/spatial-observations`、`GET /v1/spatial-observations/{id}` |

JSON 请求限制为 2 MiB；二进制数据必须走资产路由。旧版 retrieve/event/
feedback/proposal 路由继续兼容，但新 Agent 应优先使用生成 Skill 的任务命令。
