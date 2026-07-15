# Meta Memory

> Local, durable memory for AI agents — with inspectable evidence, reviewable changes, and a CLI that tells you what to do next.

Meta Memory gives your local AI agents a shared memory store without turning every conversation into an opaque database. It remembers durable facts and project decisions, keeps source evidence, lets you correct or retire a memory, and continuously consolidates completed work in the background.

Useful companion documents: [practical operations](docs/operations.md), [upgrade guide](docs/upgrade.md), [architecture](docs/architecture.md), and [troubleshooting](docs/troubleshooting.md).

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

它会询问名称和保存位置，然后会：保存配置、建立本地数据库、安装 Codex Skill，并默认安装 Heartbeat 与 Dream 的后台任务。要跳过后台任务可加 `--no-schedule`；以后随时可运行 `meta-memory schedule install`。

也可以一次接入多个内置 Agent：

```powershell
meta-memory setup --agents codex claude-code openclaw
```

完成后重启对应的 Agent 会话，再检查：

```powershell
meta-memory overview
meta-memory agent status --all --verbose
```

看到 `READY` 表示配置、Agent 和已启用的后台任务都已就绪。看到 `NEEDS_SETUP` 或 `NEEDS_ACTION` 时，不需要猜原因：复制 Overview 的第一条推荐命令即可。

### 3. 验证一次写入和找回

```powershell
meta-memory remember --project demo --content "demo 项目当前使用 SQLite。"
meta-memory search --project demo "SQLite"
meta-memory memory recent --project demo
```

`remember` 用于你明确希望长期保存的内容。正常接入的 Agent 会在对话中自动完成上下文读取与回合写回；不需要每次手动调用 `before`/`after`。

## 日常怎么用

### 与已接入的 Agent 对话

直接正常使用 Agent 即可。Meta Memory 会在回答前读取有限、相关的上下文；在回答完成后保存回合并把可长期复用的内容放入审核或整理流程。

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

完整排错流程在 [troubleshooting](docs/troubleshooting.md)。命令本身的可发现性入口始终是：

```powershell
meta-memory --help
meta-memory <command> --help
```

## 给自定义 Agent 集成者

普通用户不需要使用这一节。自定义 Agent 需要遵循真实的回合生命周期：先 `before`，把返回的 `turn_id` 保留到同一轮完成，再在把回答发给用户前调用 `after`。

```powershell
# 回答前：持久化用户请求并读取有限相关上下文。
meta-memory before --project auto --session auto --query-file request.txt

# 将上一步返回的 turn_id 和即将发送的完整回答写回。
meta-memory after --turn <turn-id> --assistant-file response.txt
```

`after` 只负责可靠落盘和排队，不在用户等待时执行重型整理。重复同一个 `turn_id` 和回答是安全的；暂时无法写入时会进入 spool，随后由 `maintain`、Heartbeat 或 `recovery replay` 处理。

要安装一个自定义 Agent：

```powershell
meta-memory install-agent custom --agent-id hermes --skill-dir ~/.hermes/skills --host-file ~/.hermes/AGENTS.md
```

若宿主没有说明文件，改用 `--no-host-file`。安装器会生成固定 Python、配置和 Agent id 的 launcher；升级或移动环境后用 `meta-memory agent sync --all` 刷新。

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

Connected Agents normally perform the durable `before`/`after` lifecycle automatically. Custom integrations can use `meta-memory before --query-file request.txt`, retain the returned `turn_id`, then call `meta-memory after --turn <turn-id> --assistant-file response.txt` before sending that exact response.

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
