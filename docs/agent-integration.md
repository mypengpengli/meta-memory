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
