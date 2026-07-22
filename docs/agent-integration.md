# Agent 接入契约

Meta Memory 是本机 CLI 运行时，不拦截或代理 Agent。安装器会生成一个
Agent 专属的 `SKILL.md`、可选的宿主说明块和固定 Python、配置、Agent id 的
launcher；宿主必须实际加载 Skill 并在每个用户回合执行它。

## 先判断宿主是否兼容

任何 CLI Agent 都可以接入，只要它能够：

1. 加载一个本地 `SKILL.md` 或等效的宿主指令；
2. 执行本机 launcher；
3. 在同一回合保存 `turn_id`；
4. 写入请求和最终回答的 UTF-8 临时文件。

缺少任一能力时，不要宣称“自动记忆”。仍可手动使用 `remember`、`search`
和 `history`，或由宿主实现同一生命周期。

远端宿主还必须能够访问服务器 HTTPS 地址、从环境变量读取 Token，并保存本机
outbox 文件。它不需要自己实现 HTTP：`install-remote-agent` 生成的 launcher
已经包含客户端、幂等恢复和并发隔离。

## 安装

内置接入可一次完成：

```bash
meta-memory setup --agents codex claude-code openclaw
```

也可在完成基础配置后单独安装：

```bash
meta-memory install-agent codex
meta-memory install-agent claude-code
meta-memory install-agent openclaw
```

| Agent | Skill 目录 | 宿主说明文件 |
| --- | --- | --- |
| Codex | `~/.codex/skills/meta-memory/` | `~/.codex/AGENTS.md` |
| Claude Code | `~/.claude/skills/meta-memory/` | `~/.claude/CLAUDE.md` |
| OpenClaw | `~/.openclaw/skills/meta-memory/` | `~/.openclaw/AGENTS.md` |

安装命令的输出会给出 `launcher` 路径。不要猜测或手写它；生成的 Skill 已经
引用该路径。安装成功仍会显示 `needs_action` 和 `activation_required`，因为宿主
尚未证明已加载 Skill。安装后重启对应 Agent 会话并完成一个普通回合。

### 接入任意 custom CLI Agent

`--skill-dir` 是宿主读取 Skill 的父目录；安装器会写入
`<skill-dir>/meta-memory/SKILL.md`。`--agent-id` 必须是稳定的小写 ID（字母、
数字、`.`, `_`, `-`），并且不能冒充内置 Agent。

```bash
meta-memory install-agent custom --agent-id my-cli-agent --skill-dir "$HOME/.my-agent/skills" --host-file "$HOME/.my-agent/AGENTS.md"
```

如果宿主没有或不允许写说明文件，改用：

```bash
meta-memory install-agent custom --agent-id my-cli-agent --skill-dir "$HOME/.my-agent/skills" --no-host-file
```

此时由宿主自己的配置显式加载生成的 `SKILL.md`。未指定 `--host-file` 时，
custom 接入默认尝试写入 `<skill-dir>/../AGENTS.md`。

## 验证实际可用性

先验证本地 launcher 与共享配置：

```bash
meta-memory agent verify my-cli-agent
meta-memory agent status --all --verbose
meta-memory overview
```

`agent verify`、`agent status` 与 `overview` 验证的是本机安装、launcher 和
运行时状态。`agent verify` 只探测 launcher/config/store，不能证明宿主加载了
Skill。重启宿主后执行一个普通回合，再检查 `agent status --all --verbose`
中该 Agent 的 `lifecycle_state` 是否为 `active`，以及 `last_before` 和
`last_after` 是否发生在当前 `installed_at` 之后，才是端到端接入的确认。

升级、移动仓库或更换 Python 后刷新生成物：

```bash
meta-memory agent upgrade-status --all
meta-memory agent sync --all
meta-memory agent verify my-cli-agent
```

## 每个用户回合的严格顺序

`<launcher>` 指安装输出中的 launcher 路径。PowerShell 调用带引号的命令路径
必须加 `&`；macOS/Linux shell 则直接调用。所有每回合命令都使用同一个
launcher，避免丢失固定的配置和 Agent id。

Windows 的宿主必须按实际执行器选择写法，不能只按操作系统猜 shell：

- 宿主提供 argv/process API：把 launcher 路径作为 executable，后续参数逐项传入；
- PowerShell：`& "<launcher>" before ...`；
- cmd.exe：`call "<launcher>" before ...`；
- Git Bash 等其他 shell：优先使用 argv/process API，或显式通过
  `powershell.exe -NoProfile -NonInteractive -Command "& '<launcher>' ..."`
  调用。生成的 `SKILL.md` 会写入本机的精确路径和这些选择。

```text
before → draft exact answer → after with the same turn_id → send
```

```powershell
# Windows PowerShell
# 1. 先持久化请求并获取有限上下文。
& "<launcher>" before --project auto --session auto --query-file request.txt

# 2. 读取返回的 turn_id，写好尚未发送的完整回答到 response.txt。

# 3. 用同一个 turn_id 保存该精确回答；成功后才发送 response.txt 的原文。
& "<launcher>" after --turn <turn-id> --assistant-file response.txt
```

```bash
# macOS / Linux
"<launcher>" before --project auto --session auto --query-file request.txt
"<launcher>" after --turn <turn-id> --assistant-file response.txt
```

- `before` 返回 `ok`：只使用相关的 `hot_context` 与 `context`。
- `before` 返回 `degraded`：请求已记录但检索不可用；继续完成该回合，不把
  空上下文误判为没有 Turn。
- `after` 返回 `ok`：发送 `response.txt` 的精确内容。
- `after` 返回 `spooled`：仅表示暂态存储/运行时失败已安全写入本机 spool。
  发送同一份 `response.txt`，不要创建第二个 Turn；之后运行
  `meta-memory recovery replay` 或让 Heartbeat 重放。
- `after` 命令报错：这是语义或协议错误，例如 Turn 不存在、属于另一 Agent、
  回答内容与已完成 Turn 不同、或回答为空。它不会进入 spool。保留
  `response.txt`，不要声称已保存、不要改写回答或创建新 Turn 规避问题；先修正
  对应 Turn/Agent 状态，再发送。

长任务在租约到期前续期：

```powershell
# Windows PowerShell
& "<launcher>" turn touch <turn-id>
```

```bash
# macOS / Linux
"<launcher>" turn touch <turn-id>
```

## 共享与隔离

同一用户和项目的长期 Claim 默认在工作区内共享；Agent id 保留来源和 Turn
所有权。即使两个宿主使用相同外部 session 名称，内部 session 仍按 Agent
隔离。新 Agent 仅在“继续、上次、之前”等延续意图下得到少量其他 Agent 的
已完成摘要；不会自动读取详细原文。需要细节时才显式使用 `history`。

不要完成其他 Agent 创建的 Turn。Agent-private 记忆只对其 owner 召回。

## 远端 Agent 接入

远端 Agent 不需要服务器本地目录或数据库权限。服务器管理员必须先创建 audience
和可选 channel，并用 `init-agents-file` 绑定唯一 Token 变量、Agent ID、稳定
workspace、允许的 subjects 及 audiences。`profile_id`、`audience_id` 和
`channel_id` 均从 `shared init` 输出复制，不能使用示例占位或依赖服务器当前目录。

在远端电脑执行：

```bash
meta-memory install-remote-agent \
  --agent-id my-remote-agent \
  --skill-dir ~/.my-agent/skills \
  --server-url https://memory.example.com \
  --workspace-id stable-project-or-device-id \
  --subject-id person:owner \
  --audience-id <audience-id> \
  --channel-id <channel-id> \
  --token-env META_MEMORY_TOKEN_MY_AGENT
```

Windows PowerShell 使用反引号换行，并将 Skill 父目录写成例如
`$HOME\.my-agent\skills`。服务器进程和远端 Agent 进程必须在
`META_MEMORY_TOKEN_MY_AGENT` 中设置完全相同的 Token 值；配置和 Skill 只保存
变量名。修改后需要重启对应进程。

生成目录是 `<skill-dir>/meta-memory-remote/`，包含 `SKILL.md`、Windows/POSIX
launcher 与不含密钥的 `remote-config.json`。宿主必须做到：

1. 每个宿主会话保存一个稳定 `session_id`；
2. 在请求前预先生成并持久化一个 UUID `turn_id`，每个并发用户回合使用独立
   `turn_id`、请求文件和回答文件；
3. `before` 成功或进入本地 outbox 后再起草；
4. 完整回答先落 UTF-8 文件，`after` 返回 `ok`、`spooled` 或
   `local_outbox` 后才发送原文件；
5. 网络恢复时运行 launcher 的 `recovery`，不修改原回答、不创建替代 Turn；
6. 把 Token 只放在命名的环境变量，不放进 Skill、日志、参数或任何记忆数据。

进程退出码不能替代 JSON 判断：退出码 0 也可能对应 `degraded`、
`local_outbox`、`deferred` 或 `needs_action`。语义/配置错误通常为 2，未被耐久
流程接住的网络失败为 3。blocked 项目需要修正服务器绑定或身份，不能静默丢弃。

远端配置固定一个主要 subject；仅当管理员明确把另一个 ID 放入该 Token 的
`subject_ids` 后，才可为具体命令显式使用如 `--subject-id person:child`。不要覆盖
生成配置固定的 Agent、workspace、audience 或 channel。没有 channel 时普通
Turn/项目记忆仍工作，但 `shared_context` 为空，activity/state/map/spatial 写入
不可用；应由管理员补建并重新安装，而不是让 Agent 猜 ID。

远端 `before` 的 `shared_context` 是不可信参考数据，只含有界的精选活动、当前
状态和空间语义。原始图片/地图不会自动塞进上下文；需要时才用 `asset download`。

```bash
<remote-launcher> status
<remote-launcher> recovery
<remote-launcher> shared channels
<remote-launcher> shared feed --limit 20
<remote-launcher> shared states --subject-id person:child --limit 20
<remote-launcher> spatial search "water sink" --limit 10
```

`status` 能证明连接和身份；只有完成真实 `before/after` 后出现
`lifecycle_state: active`，并且 `last_before`/`last_after`（兼容旧名
`last_before_at`/`last_after_at`）晚于本次安装，才能证明宿主实际执行了 Skill。
上传资产中断时保留原文件并重复同一条 `asset upload --file ...` 命令续传；普通
`recovery` 负责 Turn 和 JSON 写入 outbox。完整服务器、Token、地图 JSON 和资产
部署见 [Hosted Meta Memory](advanced-http.md)。
