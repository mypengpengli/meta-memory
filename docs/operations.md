# Meta Memory 实际操作指南

这份文档面向准备日常使用 Meta Memory 的人。它以“要完成什么”组织命令；
需要机器可读输出时，把全局开关放在子命令前，例如
`meta-memory --json overview`，不要写成 `meta-memory overview --json`。

## 第一次使用

```bash
meta-memory setup --agents codex
meta-memory overview
```

`overview` 应显示 `ready`、`needs_action` 或 `degraded`，并给出下一条可执行的命令。
首次在一个真实仓库工作时，先从该仓库根目录绑定一个易读名称：

```bash
meta-memory project set meta-memory
meta-memory project current
```

不要先把重要信息写到 `--project demo`，再期待 `--project auto` 自动读到它们；两者可能是不同工作区。

## 日常对话如何记忆

已接入且宿主已加载生成 Skill 的 Agent 会在回答前运行 `before`、在答复发出前运行 `after`。安装检查不等于宿主每回合已执行；用一次真实回合后的 `agent status --all --verbose` 确认，并见 [Agent 接入契约](agent-integration.md)。

明确、稳定而且希望长期保留的信息，可以直接写入：

```bash
meta-memory remember --project auto --content "这个项目修改完成后直接提交并推送到 origin/main。"
meta-memory memory recent --project auto
```

系统会把普通任务请求、催促和确认保留为会话上下文，而不是自动塞进长期记忆。对自动候选有疑问时，使用收件箱：

```bash
meta-memory inbox list
meta-memory inbox show <id>
meta-memory inbox approve <id>
meta-memory inbox reject <id>
```

## 查找、更正和遗忘

```bash
meta-memory memory search --project auto "提交"
meta-memory memory show <claim-id>
meta-memory memory correct <claim-id> --content "新规则内容"
meta-memory memory archive <claim-id>
meta-memory memory forget <claim-id>
meta-memory memory export --project auto --format markdown
```

`correct` 保留来源和版本链；`archive` 会停止默认召回但保留历史；`forget` 是明确从活动记忆中移除。
需要只查看可更正的长期 Claim 时，使用 `--claims-only`，避免把 Dream 或资源结果的 ID 传给更正命令。

## 继续上一次工作或切换 Agent

```bash
meta-memory history recent --project auto
meta-memory history search --project auto "检索优化"
meta-memory history show <session-id> --last 8
```

当新的 Agent 收到“继续、上次、之前做过、另一个 Agent”等请求时，`before` 会自动带入少量已完成的工作区摘要。其他 Agent 的详细原文仍需显式查看，避免不必要地扩大上下文。

## 项目与资料

```bash
meta-memory project list
meta-memory project rename <old> <new>
meta-memory project stats --project auto

meta-memory import ./notes --recursive --changed-only --project auto
meta-memory resource list --project auto
meta-memory resource refresh <resource-id>
meta-memory resource remove <resource-id>
```

资料是检索证据，不会直接变成用户偏好或项目事实。导入后可用于回答问题，但不会混入 Claim 管理。

## Dream、整理和恢复

```bash
meta-memory dream heartbeat
meta-memory dream deep --scan-days 7 --dry-run
meta-memory dream deep --scan-days 7
meta-memory dream list
meta-memory dream show latest

meta-memory recovery status
meta-memory recovery replay
meta-memory turn list --unfinished
meta-memory turn touch <turn-id>
```

Heartbeat 只处理新增或 dirty 的工作区；没有新来源时返回 `idle`。Deep Dream 是带来源的工作复盘，不替代事实 Claim。长时间任务可用 `turn touch` 延长租约，避免正常工作被错误标为 abandoned。

## 配置、定时和升级

```bash
meta-memory config list
meta-memory config describe dream.heartbeat_interval_minutes
meta-memory config set dream.heartbeat_interval_minutes 10 --apply

meta-memory schedule status
meta-memory agent status --all
meta-memory agent upgrade-status
meta-memory agent sync --all
```

所有影响调度器的配置都会在 `--apply` 时刷新本地任务；`schedule status` 会显示安装结果、最近运行、下一次预计运行和日志位置。

## 遇到“似乎没记住”时

先执行：

```bash
meta-memory overview
meta-memory recovery status
meta-memory inbox list
```

优先根据 `next_action` 处理，不要靠重复发送同一段对话来赌后台是否会整理成功。

## 远端 Agent、家庭状态和空间资产

服务器是唯一 SQLite 写入者；远端电脑只运行生成的 Skill/launcher：

```bash
# 服务器：记录返回的 profile_id、audience_id 和 channel_id。
meta-memory --json shared init --type household --key home --restricted \
  --member-agent home-robot

# 用上一步真实 ID 生成绑定文件；subject-id 和 audience-id 可重复。
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

# 远端电脑：环境变量必须使用服务器上的同一个 Token 值。
AUDIENCE_ID='paste audience.audience_id here'
CHANNEL_ID='paste channel.channel_id here'
meta-memory install-remote-agent --agent-id home-robot \
  --skill-dir "$HOME/.robot/skills" --server-url https://memory.example.com \
  --workspace-id home-robot-workspace --subject-id person:owner \
  --audience-id "$AUDIENCE_ID" --channel-id "$CHANNEL_ID" \
  --token-env META_MEMORY_TOKEN_HOME_ROBOT
export META_MEMORY_TOKEN_HOME_ROBOT='<the-same-token-value>'
```

远端日常检查使用生成 launcher 的 `status` 和 `recovery`。服务器上的 Overview
使用 `--server --agents-file ...` 后会检查服务端配置和 shared activity、current
state、asset、map、spatial observation；普通 `overview` 检查的是本地 Agent，纯
服务器没有本地 Skill 时显示 `NEEDS_ACTION` 并不代表 HTTP 服务坏了。

PowerShell 用反引号换行，并用
`$env:META_MEMORY_TOKEN_HOME_ROBOT = '<the-same-token-value>'` 设置 Token；要手工
复制示例文件时使用 `Copy-Item`，不是 `cp`。完整 Windows 与 Bash 流程见
[Hosted Meta Memory](advanced-http.md)。

家庭共享只发布有用摘要。人物位置等易变事实用 `shared state-set` 并设置过期时间；
图片/视频/点云先用 `asset` 保存，地图用稳定 `map_id` 建版本，最后用 `spatial`
记录可检索说明和链接。远端可直接读取：

```text
<launcher> shared channels
<launcher> shared feed --limit 20
<launcher> shared states --subject-id person:child --limit 20
<launcher> spatial search "water sink" --limit 10
<launcher> spatial get --observation-id <observation-id>
```

不配置 channel 时，普通 Turn 和 workspace 记忆仍可使用，但 `shared_context` 为空，
也不能发布 activity/state/map/spatial；请重新安装真实 channel，不要让 Agent 猜 ID。
上传中断时保留未修改的原文件并重复同一条 `asset upload` 命令续传。Meta Memory
只存上游 Agent 得到的图片说明、OCR、对象和地图结果，不执行视觉识别、SLAM 或
路径规划。完整模型与操作见 [家庭和空间记忆](shared-world-memory.md)。
