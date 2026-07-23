# Meta Memory

> Local or hosted durable memory for AI agents — with inspectable evidence, reviewable changes, and a CLI that tells you what to do next.

Meta Memory gives AI agents on one computer—or remote Agents using one central server—a shared memory store without turning every conversation into an opaque database. It remembers durable facts and project decisions, keeps source evidence, lets you correct or retire a memory, and continuously consolidates completed work in the background. Household activity, time-bounded person/device state, images, maps, and robot observations have explicit storage and sharing boundaries.

Useful companion documents: [Docker cloud deployment](docs/container-deployment.md), [practical operations](docs/operations.md), [Agent integration](docs/agent-integration.md), [hosted protocol](docs/advanced-http.md), [household and spatial memory](docs/shared-world-memory.md), [upgrade guide](docs/upgrade.md), [architecture](docs/architecture.md), and [troubleshooting](docs/troubleshooting.md).

# 中文

## 先记住这四个命令

绝大多数情况下，只需要下面这条路径：

```powershell
# 第一次：保存配置、接入 Codex，并安装后台整理任务。
meta-memory setup --agents codex

# 每次不确定当前状态时：看项目、Agent、队列和下一步。
meta-memory overview

# 要主动保存一条事实或偏好时。
meta-memory remember --content "这个项目发布前需要先更新变更日志。"

# 需要找回时。
meta-memory search "发布前"
```

`overview` 是日常入口。它会显示当前项目、Agent 是否已接入、后台任务是否已安装、未完成回合/待审核项目，以及可以直接复制执行的下一步命令。

## 5 分钟开始使用

### 1. 安装

需要 Python 3.10 或更高版本。下面是从源码安装的推荐方式：

```powershell
git clone https://github.com/mypengpengli/meta-memory.git
cd meta-memory

python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install .
meta-memory --version
```

macOS / Linux 激活虚拟环境：

```bash
source .venv/bin/activate
```

如果 PowerShell 不允许执行激活脚本，可直接运行 `.\.venv\Scripts\meta-memory.exe`。开发当前仓库时可将最后一条安装命令换成 `python -m pip install -e .`。

### 2. 初始化并接入 Agent

在交互式终端执行：

```powershell
meta-memory setup --agents codex
```

它会询问名称和保存位置，然后会：保存配置、建立本地数据库、安装 Codex Skill 与专属 launcher，并默认安装 Heartbeat 与 Dream 的后台任务。要跳过后台任务可加 `--no-schedule`；以后随时可运行 `meta-memory schedule install`。

也可以一次接入多个内置 Agent：

```powershell
meta-memory setup --agents codex claude-code openclaw
```

`setup` 完成本地文件和 launcher 验证后仍会返回 `NEEDS_ACTION`，这是有意的：
安装器不能冒充宿主宣称 Skill 已经加载。重启对应的 Agent 会话，完成一个普通
对话回合，再检查：

```powershell
meta-memory overview
meta-memory agent status --all --verbose
```

看到 `READY` 表示本地配置、当前版本的 Skill/launcher、一次发生在本次安装之后
的完整 `before/after` 回合，以及已启用的后台任务都已验证。`agent status
--all --verbose` 中 `lifecycle_state: active`、`last_before` 和 `last_after` 会给出
对应证据；它只能证明已经观察到真实回合，不能保证宿主未来永远不会关闭 Skill。
看到 `NEEDS_SETUP` 或 `NEEDS_ACTION` 时，按 Overview 的推荐命令和手工激活说明执行。

### 3. 验证一次写入和找回

```powershell
meta-memory remember --project demo --content "demo 项目当前使用 SQLite。"
meta-memory search --project demo "SQLite"
meta-memory memory recent --project demo
```

`remember` 用于你明确希望长期保存的内容。已加载生成 Skill 的 Agent 会在对话中执行上下文读取与回合写回；如果宿主不支持本地 Skill 或命令执行，请按 [Agent 接入契约](docs/agent-integration.md) 手动集成严格的 `before`/`after` 流程。

## 云服务器 Docker + 本机 Codex

这是让多台电脑或远端 Agent 共用记忆的推荐部署方式。服务器只运行一个 API 和一个
串行 worker；本机 Codex 只安装远端 Skill，不直接接触服务器上的 SQLite。

服务器首次启动：

```bash
git clone https://github.com/mypengpengli/meta-memory.git
cd meta-memory
sh docker/bootstrap.sh              # 生成 Token、宿主 UID/GID 和持久目录
# 公网使用时编辑 .env，填写真实 MEMORY_DOMAIN。
docker compose config --quiet
docker compose up -d --build meta-memory worker
curl --fail http://127.0.0.1:8765/readyz

# 域名已解析并开放 80/443 后，启用自动 HTTPS。
docker compose --profile https up -d
curl --fail https://memory.example.com/readyz
```

本机安装生成的远端 Skill；下面三项身份必须与服务器 `.env` 一致：

```powershell
python -m pip install "git+https://github.com/mypengpengli/meta-memory.git"
meta-memory install-remote-agent `
  --agent-id local-codex `
  --skill-dir "$HOME\.codex\skills" `
  --server-url https://memory.example.com `
  --workspace-id personal-workspace `
  --subject-id person:user `
  --token-env META_MEMORY_TOKEN
setx META_MEMORY_TOKEN "<与服务器相同的值>"
```

重开终端并重启 Codex 后，用安装结果里的 launcher 运行 `recovery` 和 `status`，再完成
一个真实回合。只有 `lifecycle_state: active`、新的 `last_before/last_after` 和
`local_outbox_pending: 0` 才表示端到端接入完成。首次部署、多 Agent 独立 Token、
家庭频道、自动备份、升级、恢复和上线验收见 [Docker 云端部署](docs/container-deployment.md)。

## 日常怎么用

### 与已接入的 Agent 对话

确认宿主已加载生成的 Skill 后，直接正常使用 Agent 即可。它会在回答前读取有限、相关的上下文；在回答完成后保存回合并把可长期复用的内容放入审核或整理流程。安装本身不等于宿主已启用每回合执行；验证方法见 [Agent 接入契约](docs/agent-integration.md)。

遇到这些情况时再打开终端：

| 你想做什么 | 命令 | 结果 |
| --- | --- | --- |
| 看现在是否正常、接下来该做什么 | `meta-memory overview` | 一个可执行的状态面板 |
| 主动记住一条事项 | `meta-memory remember --content "..."` | 创建有来源的长期记忆 |
| 按关键词找回 | `meta-memory search "关键词"` | 返回当前项目的相关记忆和资料 |
| 看最近的长期记忆 | `meta-memory memory recent` | 查看活跃 Claims |
| 继续此前工作 | `meta-memory history "关键词"` | 先返回已完成会话摘要 |
| 看自动建议 | `meta-memory inbox list` | 列出待审核的记忆变更 |
| 发现记忆不对 | `meta-memory memory correct <claim-id> --content "正确内容"` | 保留历史并建立更正 |

先使用 `meta-memory memory show <claim-id>` 看清来源和版本，再执行更正、归档或遗忘：

```powershell
meta-memory memory show <claim-id>
meta-memory memory archive <claim-id>  # 不再默认召回，但保留历史
meta-memory memory forget <claim-id>   # 从活跃及派生记忆中移除
```

### 自动建议要不要审核？

要。自动整理不会直接把不确定的内容当成事实。查看和处理建议：

```powershell
meta-memory inbox list
meta-memory inbox show <proposal-id>
meta-memory inbox approve <proposal-id>
meta-memory inbox reject <proposal-id> --note "这是一次性任务，不需要长期保存。"
```

如果某条被召回的记忆有用、过时或错误，也可留下反馈，让之后的整理更准确：

```powershell
meta-memory memory feedback <claim-id> --type helpful
meta-memory memory feedback <claim-id> --type outdated --note "已经迁移到新流程。"
```

## 项目、历史和资料

### 项目为什么重要

默认的 `--project auto` 会优先使用 Git 根目录；同一个项目的记忆因此会自然聚在一起。非 Git 目录或需要自定义名称时，绑定一次即可：

```powershell
meta-memory project current
meta-memory project set my-project
meta-memory project stats
```

从其他目录操作某个项目时，加 `--cwd <项目目录>`。目录绑定只改变归属，不会删除已有记忆。

### 找回此前会话

`history` 默认只返回已完成会话的简短摘要，适合“继续上次工作”：

```powershell
meta-memory history recent
meta-memory history "部署"
meta-memory history show <session-id>
```

只有确实需要具体上下文时才使用 `--detail`；它会读取受限的已完成历史。未完成回合和未发送的回答草稿不会被共享。

### 导入现有笔记或资料

导入的文件是可检索的来源证据，不会自动变成“用户事实”。支持 Markdown、文本、JSON/JSONL、CSV、YAML 和 HTML：

```powershell
meta-memory import .\notes\architecture.md --project auto
meta-memory import .\notes --recursive --changed-only
meta-memory resource list
meta-memory resource show <resource-id>
meta-memory resource refresh <resource-id>
```

这特别适合把项目说明、会议记录或已有笔记作为回答时可检索的依据。

## 后台整理、Dream 和中断恢复

Heartbeat 默认每 10 分钟处理已完成回合、待写入任务和 Hot Memory；Deep Dream 默认每天 23:30 生成可追溯的总结。没有新内容时显示 `idle` 是正常的，不代表故障。

```powershell
meta-memory dream status
meta-memory dream heartbeat
meta-memory dream deep --scan-days 7 --dry-run
meta-memory dream deep --scan-days 7
```

后台任务状态：

```powershell
meta-memory schedule status
meta-memory schedule install
```

如果 Agent 或终端在回复过程中中断，先看 Overview。它会把待重放的完成记录和未完成回合放在最前面；常用恢复命令如下：

```powershell
meta-memory recovery status
meta-memory recovery replay
meta-memory turn list --unfinished
meta-memory turn reopen <turn-id>
```

## 升级、备份和换电脑

升级当前源码安装后，刷新所有集成和本机计划任务：

```powershell
git pull
python -m pip install --upgrade .
meta-memory agent upgrade-status --all
meta-memory agent sync --all
meta-memory schedule install
meta-memory overview
```

创建和恢复完整备份：

```powershell
meta-memory backup --output "$HOME/meta-memory-backup.zip"
meta-memory restore "$HOME/meta-memory-backup.zip" --destination "$HOME/.meta-memory-restored"
```

恢复后执行 `meta-memory overview` 和 `meta-memory doctor`；如果此设备需要后台整理，再执行 `meta-memory schedule install`。更多版本迁移细节见 [升级指南](docs/upgrade.md)。

## 常见情况，直接这样做

| 现象 | 先执行 | 接下来 |
| --- | --- | --- |
| 不确定有没有接入成功 | `meta-memory overview` | 按第一条推荐操作执行 |
| Agent 更新或仓库移动后似乎没记住 | `meta-memory agent upgrade-status --all` | `meta-memory agent sync --all` |
| 应该记住的内容没有留下 | `meta-memory remember --content "..."` | 用 `memory recent` 确认 |
| 找回了错误的内容 | `meta-memory memory show <claim-id>` | `memory correct` 或 `memory archive` |
| 后台整理看起来没有运行 | `meta-memory schedule status` | `schedule install`，再看 `dream status` |
| 回复过程中发生中断 | `meta-memory recovery replay` | `turn list --unfinished` |
| 远端 Agent 连不上 | 远端 launcher 的 `status` | 检查 HTTPS、Token 环境变量和稳定 workspace id |
| 远端回复待重试 | 远端 launcher 的 `recovery` | 保留原回答文件，不新建 Turn、不改写回答 |

完整排错流程在 [troubleshooting](docs/troubleshooting.md)。命令本身的可发现性入口始终是：

```powershell
meta-memory --help
meta-memory <command> --help
```

## 远端 Agent、家庭机器人与共享记忆

远端 Agent 不直接读写 SQLite。选择一台服务器作为唯一数据所有者，服务器运行
`meta-memory serve`；其他电脑、云 Agent 或机器人安装生成的远端 Skill，通过
HTTPS 使用同一个完整生命周期。

远端宿主除了能够加载 Skill，还必须能执行 launcher、读取 JSON、写 UTF-8 文件、
在同一会话保存 `session_id`、在同一回合保存 `turn_id`，并从环境变量读取 Token。
HTTP、重试、幂等和分块续传由生成客户端负责。

### 服务器端：完整最小路径

生产部署优先使用上面的 Docker Compose；它已经包含持久目录、`/readyz`、唯一
Heartbeat/Dream worker、自动备份、正常停机和可选 Caddy HTTPS。下面的裸 CLI
流程适合不使用 Docker 或需要自行接入 systemd/现有反向代理的环境。

```bash
# 1. 初始化中央存储与唯一后台整理任务。
meta-memory setup --name "Family memory" --maintenance yes --dream yes --non-interactive

# 2. 建立受限家庭共享边界。
meta-memory --json shared init --type household --key home --label "家庭" \
  --restricted --member-agent home-robot --member-agent family-planner

# 3. 从输出复制真实 profile_id、audience_id、channel_id，生成服务器绑定。
PROFILE_ID='paste audience.profile_id here'
AUDIENCE_ID='paste audience.audience_id here'
CHANNEL_ID='paste channel.channel_id here'
meta-memory init-agents-file --output "$HOME/.meta-memory/agents.json" \
  --agent-id home-robot --profile-id "$PROFILE_ID" \
  --workspace-id home-robot-workspace \
  --subject-id person:owner --subject-id person:child \
  --audience-id "$AUDIENCE_ID" --audience-id "$CHANNEL_ID" \
  --token-env META_MEMORY_TOKEN_HOME_ROBOT

# 4. Token 值只放环境变量；agents.json 只保存变量名。
export META_MEMORY_TOKEN_HOME_ROBOT='<one-generated-token-value>'

# 5. 纯服务器使用 server Overview，然后启动唯一服务。
meta-memory overview --server --agents-file "$HOME/.meta-memory/agents.json"
meta-memory serve --agents-file "$HOME/.meta-memory/agents.json" \
  --host 127.0.0.1 --port 8765
```

PowerShell 使用反引号续行和
`$env:META_MEMORY_TOKEN_HOME_ROBOT = '<one-generated-token-value>'`。需要人工复制
示例配置时，Windows 使用
`Copy-Item extras\http\agents.example.json "$HOME\.meta-memory\agents.json"`；示例中的
`profile_id` 必须替换为 `shared init` 输出，不能自行写 `family-profile`。

生产环境推荐由反向代理把 `https://memory.example.com` 转发到本机 8765；也可给
`serve` 传 `--tls-cert/--tls-key`。只有服务器安装 Heartbeat/Dream 计划任务，
不要让多个远端设备各自整理同一中央库。

### 远端 Agent 电脑

```bash
# 先安装到持久 Python 环境；launcher 会固定这个解释器。
python -m pip install "git+https://github.com/mypengpengli/meta-memory.git"

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

# 必须与服务器对应变量使用完全相同的 Token 值。
export META_MEMORY_TOKEN_HOME_ROBOT='<the-same-token-value>'
```

设置 Token 环境变量并重启 Agent，然后用安装结果中的 launcher 执行 `status`。
完成一个真实对话回合后，`lifecycle_state: active`、新的 `last_before/last_after`
（兼容旧名 `last_before_at/last_after_at`）和 `local_outbox_pending: 0` 才表示端到端
可用。

远端 Skill 会要求每个回合在网络请求前预先保存 UUID `turn_id`，并处理稳定
`session_id`、回答发送前的完整缓冲、精确回答 SHA-256、并发回合独立文件和网络
中断 outbox。断网后运行 launcher 的 `recovery`；必须读取 JSON `status`，因为
退出码 0 也可能是 `degraded`、`local_outbox`、`deferred` 或 `needs_action`。保留
原回答，不要新建第二个 Turn 或在同一 Turn 下改写答案。

`channel_id` 可省略：普通 Turn、workspace Claim 和资产仍可使用，但
`shared_context` 为空，不能发布 activity/state/map/spatial。需要共享信息时让
管理员创建真实 channel 并重新安装；不要让 Agent 猜 ID。服务器允许多个
`subject_ids` 时，机器人可显式为例如 `person:child` 记录状态；未在白名单内的
subject 会被拒绝。

### 机器人发现的事情、人物位置和地图

不要把所有机器人日志广播给所有 Agent。内部诊断留在 device/Agent 范围；只有
其他 Agent 真正需要的结果才发布到 household/person channel：

```bash
# 可共享事件：某物损坏。
meta-memory shared publish --channel-id <channel-id> \
  --kind household --summary "冰箱不制冷，需要维修"

# 会变化的短时事实：必须有时间、来源、置信度和过期时间。
meta-memory shared state-set --channel-id <channel-id> \
  --subject-id person:child --state-key last_seen \
  --summary "最后看到孩子在小区游乐场入口" \
  --source-ref robot-camera:event-42 --confidence 0.92 \
  --observed-at <ISO-8601-observed-at> \
  --valid-until <ISO-8601-short-expiry>

# 图片/点云/栅格原始字节独立保存；语义观察只保存说明和链接。
meta-memory asset add room.jpg --media-type image/jpeg
meta-memory map add --channel-id <channel-id> --map-id home-floor-1 \
  --coordinate-frame map --asset-id <asset-id>
meta-memory spatial add --channel-id <channel-id> --map-id home-floor-1 \
  --asset-id <asset-id> --location-text kitchen --caption "水槽下方有积水"
```

远端 Agent 可以直接读取共享结果：

```text
<launcher> shared channels
<launcher> shared feed --limit 20
<launcher> shared states --subject-id person:child --state-key last_seen --limit 20
<launcher> spatial search "water sink" --limit 10
<launcher> spatial get --observation-id <observation-id>
```

远端 launcher 的 `asset upload` 使用分块、哈希、去重和可恢复上传；中断后保留
未修改的原文件，重复同一条上传命令即可续传。实际可用的 `map put` JSON 应包含
稳定 `map_id`、`coordinate_frame`、采集时间和对象类型 `metadata`；识别对象文件
必须是 JSON 数组。其他 Agent 在 `before` 中只收到有界活动、当前未过期状态和
空间语义，确实需要原图时才调用 `asset download`。

Meta Memory 不做图片理解、OCR、物体识别、SLAM、地图融合或路径规划；机器人或
上游模型先完成这些计算，再同步语义结果、来源、时间、置信度和原始资产链接。
完整首次部署、Windows/Bash 命令和 map JSON 见
[Hosted Meta Memory](docs/advanced-http.md)，模型与操作见
[家庭和空间记忆](docs/shared-world-memory.md)。

## 给自定义 Agent 集成者

任何能加载本地 Skill、执行 launcher、保存同一回合 `turn_id` 并写 UTF-8
临时文件的 CLI Agent 都可接入：

```powershell
meta-memory install-agent custom --agent-id my-cli-agent --skill-dir "$HOME/.my-agent/skills" --host-file "$HOME/.my-agent/AGENTS.md"
meta-memory agent verify my-cli-agent
```

严格顺序始终是 `before → draft → after → send`。只有 `after` 返回 `ok` 或
`spooled` 才发送原回答；`spooled` 是可重放的暂态失败，Turn/Agent 不匹配等
语义错误则不能当作 spool，必须先修复。完整的能力判断、路径、custom 无说明
文件接入、验证和恢复说明见 [Agent 接入契约](docs/agent-integration.md)。

## 工作原理（简版）

Meta Memory 明确区分：

```text
原始来源  ≠  长期 Claim
当前状态  ≠  历史记录
用户陈述  ≠  Agent 推断
可检索资料 ≠  可执行指令
```

它不是简单地保存聊天全文：回答前只取少量相关上下文，回答后才把完整回合进入整理流程；不确定的自动提议可以审核，错误内容可以更正或退出召回。SQLite 存储适合一台中心设备上的个人或小团队使用；不要让多台设备同时直接写同一个数据库文件。

# English

## Quick start

Install with Python 3.10+ and connect an Agent:

```bash
git clone https://github.com/mypengpengli/meta-memory.git
cd meta-memory
python -m venv .venv
# Windows: .\.venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
meta-memory setup --agents codex
meta-memory overview
```

`overview` is the home screen. It reports the active project, setup readiness, Agent integration, background schedules, pending work, recent activity, and copyable next commands.

After setup, restart the Agent and complete one normal conversation. `NEEDS_ACTION`
is expected until `agent status --all --verbose` observes a post-install
`before`/`after` lifecycle; only then does Overview report `READY`.

## Everyday commands

```bash
# Save and find an explicit fact.
meta-memory remember --content "The project deploys from main."
meta-memory search "deploys"

# Inspect and correct durable Claims.
meta-memory memory recent
meta-memory memory show <claim-id>
meta-memory memory correct <claim-id> --content "The project deploys from release."

# Review automatic proposals and continue completed work.
meta-memory inbox list
meta-memory history "deployment"

# Index source material without promoting it to a user fact.
meta-memory import notes.md --project auto
meta-memory resource list
```

An installed Agent performs the durable lifecycle only when its host loads the generated Skill. The strict order is `before → draft → after → send`; send the exact response only after `ok` or a deferred `spooled`/remote `local_outbox` completion. See [Agent integration](docs/agent-integration.md) for custom installation, capability checks, and failure handling.

## Hosted remote Agents

For a production server, the shortest supported path is the included Compose
deployment. It runs one API, one serialized maintenance/Dream/backup worker,
persists data/config/backups, exposes `/readyz`, and optionally terminates HTTPS
with Caddy:

```bash
git clone https://github.com/mypengpengli/meta-memory.git
cd meta-memory
sh docker/bootstrap.sh               # token, host uid/gid, persistent directories
# Set the real MEMORY_DOMAIN in .env before enabling HTTPS.
docker compose config --quiet
docker compose up -d --build meta-memory worker
docker compose --profile https up -d
curl --fail https://memory.example.com/readyz
```

See [Docker cloud deployment](docs/container-deployment.md) for the Windows
Codex client, additional Agents, backup/restore, upgrades, and the acceptance
checklist.

Without Docker, use the following manual deployment instead. Do not run this
second API or its local schedules against a store already owned by Compose:

```bash
# Server: retain profile/audience/channel IDs from this JSON result.
meta-memory --json shared init --type household --key home --restricted \
  --member-agent home-robot
PROFILE_ID='paste audience.profile_id here'
AUDIENCE_ID='paste audience.audience_id here'
CHANNEL_ID='paste channel.channel_id here'
meta-memory init-agents-file --output "$HOME/.meta-memory/agents.json" \
  --agent-id home-robot --profile-id "$PROFILE_ID" \
  --workspace-id home-robot-workspace \
  --subject-id person:owner --subject-id person:child \
  --audience-id "$AUDIENCE_ID" --audience-id "$CHANNEL_ID" \
  --token-env META_MEMORY_TOKEN_HOME_ROBOT
export META_MEMORY_TOKEN_HOME_ROBOT='<one-token-value>'
meta-memory overview --server --agents-file "$HOME/.meta-memory/agents.json"
meta-memory serve --agents-file "$HOME/.meta-memory/agents.json"

# Remote computer: set the exact same token value, then restart the Agent host.
python -m pip install "git+https://github.com/mypengpengli/meta-memory.git"
AUDIENCE_ID='paste audience.audience_id here'
CHANNEL_ID='paste channel.channel_id here'
meta-memory install-remote-agent --agent-id home-robot \
  --skill-dir "$HOME/.robot/skills" --server-url https://memory.example.com \
  --workspace-id home-robot-workspace --subject-id person:owner \
  --audience-id "$AUDIENCE_ID" --channel-id "$CHANNEL_ID" \
  --token-env META_MEMORY_TOKEN_HOME_ROBOT
export META_MEMORY_TOKEN_HOME_ROBOT='<the-same-token-value>'
```

The remote Skill manages exact-answer buffering, per-Turn identity, concurrent
receipts, and a durable local outbox. A channel is optional: ordinary Turns and
workspace memory still work without one, but shared context and world writes
do not. Hosted `before` returns bounded shared activity, current time-bounded
state, and spatial semantics only when a real channel is configured. Binary
images/maps use resumable asset upload and are fetched only on demand. Meta
Memory stores upstream perception/map results; it is not a vision, SLAM, or
navigation engine. See [hosted deployment](docs/advanced-http.md) and
[shared-world memory](docs/shared-world-memory.md).

## Operations

```bash
# Inspect or repair a moved/upgraded integration.
meta-memory agent status --all --verbose
meta-memory agent sync --all

# Inspect or run consolidation.
meta-memory dream status
meta-memory dream heartbeat
meta-memory dream deep --scan-days 7 --dry-run

# Back up and restore.
meta-memory backup --output "$HOME/meta-memory-backup.zip"
meta-memory restore "$HOME/meta-memory-backup.zip" --destination "$HOME/.meta-memory-restored"
```

Run `meta-memory --help` for task-oriented commands and `meta-memory COMMAND --help` for examples. See [operations](docs/operations.md), [upgrade](docs/upgrade.md), and [troubleshooting](docs/troubleshooting.md) for the full operational reference.

## License

MIT
