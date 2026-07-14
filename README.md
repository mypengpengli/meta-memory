# Meta Memory

**A local-first, shared long-term memory runtime for Claude Code, Codex, OpenClaw, and custom AI agents.**

**一个让 Claude Code、Codex、OpenClaw 和自定义智能体共享长期记忆的本地优先运行时。**

> Meta Memory is CLI-first and runs on a machine you control. The current repository supports Python 3.10+ and is installed from this source repository.

[中文](#中文) · [English](#english)

---

# 中文

## 先跑起来：5 分钟完成一次可验证的安装

Meta Memory 的默认模式是**同一台设备上的本地 CLI**：不需要 HTTP 服务、Token、向量数据库或常驻 Worker。先把本地流程跑通，再决定是否启用定时整理和 Dream。

### 1. 安装

准备：

- Python 3.10 或更高版本；
- Git；
- 一个本机终端（Windows PowerShell、macOS Terminal 或 Linux shell 均可）。

当前仓库推荐从 GitHub 源码安装，不要把 `pip install meta-memory` 当作本仓库的安装方式。
Windows 如果没有 `python` 命令，可把下面的 `python` 替换为 `py`（或指定的 `py -3.10` 及更高版本）。

```bash
git clone https://github.com/mypengpengli/meta-memory.git
cd meta-memory
python -m venv .venv
```

激活虚拟环境（二选一）：

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```bash
# macOS / Linux
source .venv/bin/activate
```

安装并确认 CLI 可用：

```bash
python -m pip install --upgrade pip
python -m pip install .
meta-memory --help
```

若 PowerShell 的执行策略不允许激活虚拟环境，可以直接使用虚拟环境中的可执行文件；后文的 `meta-memory` 都替换为该路径即可。

```powershell
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\meta-memory.exe --help
```

开发仓库本身时，可把最后一条安装命令改为 `python -m pip install -e .`。

### 2. 初始化本机记忆库

交互式初始化最适合首次使用：

```bash
meta-memory setup
```

它会保存姓名、记忆库位置和是否启用定时整理/Dream。默认配置文件为 `~/.meta-memory/config.toml`，默认记忆库为 `~/.meta-memory/data`。

第一次建议先不启用定时任务，确认流程正常后再开启。非交互式示例：

```bash
meta-memory setup --name "Li Peng" --maintenance no --dream no --no-schedule --non-interactive
meta-memory status
meta-memory doctor
```

所有公开命令都会输出 JSON。`status` 用来查看数据位置、待处理任务和总体状态；`doctor` 用来做健康检查。

### 3. 写入并找回第一条记忆

下面这组命令不依赖任何 Agent，能直接验证存储、索引和检索：

```bash
meta-memory remember --project demo --content "demo 项目当前使用 SQLite。"
meta-memory search --project demo "SQLite"
```

第二条命令的 JSON 中应出现非空的 `results`，每项会带有可用于纠错的 `id`。默认本地检索偏向具体关键词，因此优先搜索 `SQLite`、`UFW`、项目名等有辨识度的词，而不是过于宽泛的问题。

---

## 日常使用：普通用户与集成者分别怎么做

### 普通用户

完成一次 `setup` 和 Agent 接入后，平时直接与 Codex、Claude Code 或 OpenClaw 对话即可。已接入并遵循 Meta Memory Skill 的 Agent 会在回答前读取相关上下文、回答后记录本轮对话。

你仍可以在需要时明确表达：

```text
记住：这个项目不能使用 Docker。
以前关于数据库的记忆不对，现在已经迁移到 PostgreSQL。
```

第一句应触发 `remember`；第二句应触发 `correct`。它们都保留来源，而不是静默改写历史。

### 接入 Agent

先确认 Agent 所在的终端也能执行 `meta-memory --help`，然后安装对应 Skill：

```bash
meta-memory install-agent codex
meta-memory install-agent claude-code
meta-memory install-agent openclaw
```

一次写入三个预设位置：

```bash
meta-memory install-agent all
```

自定义 Agent 需要明确它的 skills 根目录：

```bash
meta-memory install-agent custom --skill-dir /path/to/agent/skills
```

| Agent | 写入的位置 |
| --- | --- |
| Codex | `~/.codex/skills/meta-memory/SKILL.md` 和 `~/.codex/AGENTS.md` |
| Claude Code | `~/.claude/skills/meta-memory/SKILL.md` 和 `~/.claude/CLAUDE.md` |
| OpenClaw | `~/.openclaw/skills/meta-memory/SKILL.md` 和 `~/.openclaw/AGENTS.md` |

安装器只会写入 Skill 和一段宿主说明：它不会检测 Agent 是否已安装、不会安装 Hook，也不会替 Agent 配置 Python 路径。因此建议在安装后重启 Agent 会话，并在 Agent 实际使用的终端执行一次 `meta-memory --help`。

同一系统账户下的 Agent 默认读取同一个配置和记忆库。如需使用非默认配置，可让所有 Agent 使用同一个 `META_MEMORY_CONFIG` 环境变量或全局 `--config <path>` 参数。

### 集成者：每轮真实调用顺序

对自己实现的 Agent，使用下面的生命周期。`session` 必须在同一段对话中保持稳定。

```bash
# 回答前：只把 JSON 里的 hot_context 和 context 当作可参考记忆
meta-memory before \
  --project auto \
  --session conversation-20260714-001 \
  --query-file request.txt

# 正常生成回答后：保存双方原话，并把整理工作放入队列
meta-memory after \
  --project auto \
  --session conversation-20260714-001 \
  --user-file request.txt \
  --assistant-file response.txt

# 未启用定时任务时，手动处理排队的整理工作
meta-memory maintain
```

`before` 不会记录用户问题；它返回有边界的 `hot_context` 和 `context`。`after` 只快速保存原始事件并入队，不会在用户等待时进行大型整理。若没有启用定时任务，必须由你手动运行 `maintain`。

---

## 项目、会话和作用域

普通用户只需要理解三个概念：

| 概念 | 用途 | 例子 |
| --- | --- | --- |
| 用户 | 跨 Agent 的长期偏好、稳定习惯和身份相关信息 | “以后默认中文回答” |
| 项目 | 某件事的状态、技术决策和历史方案 | `meta-memory`、`company-ai` |
| 会话 | 一段连续对话或任务的原始记录 | `codex:2026-07-14:001` |

`--project auto` 时，Meta Memory 先以当前 Git 根目录（没有 Git 时为当前目录）作为工作区；如果该目录已经绑定项目名，就使用绑定；否则使用目录名，最后才回退到默认项目。

在项目根目录中绑定名称：

```bash
meta-memory project set meta-memory
meta-memory search --project auto "SQLite"
```

从别的目录操作某个项目时，传入 `--cwd`：

```powershell
meta-memory search --cwd "D:\work\meta-memory" --project auto "SQLite"
```

`search`、`history` 和 `before` 的输出都会带回实际解析到的 `project` 与 `project_root`，可用于核对当前作用域。

---

## 常见操作

| 目的 | 命令 | 结果 |
| --- | --- | --- |
| 看当前状态 | `meta-memory status` | 数据目录、待处理任务、健康摘要 |
| 做健康检查 | `meta-memory doctor` | 迁移、FTS、阻塞 Claim 等检查 |
| 让对话入队后立即整理 | `meta-memory maintain --max-jobs 20` | 处理原始事件、候选和投影 |
| 显式保存一条记忆 | `meta-memory remember --project auto --content "…"` | 带来源立即写入并更新投影 |
| 搜索结构化记忆 | `meta-memory search --project auto "关键词"` | 返回 Claim 结果及 ID |
| 搜索历史原话 | `meta-memory history --project auto "关键词"` | 返回会话中的历史消息 |
| 生成近期 Dream 报告 | `meta-memory dream --scan-days 7` | JSON 的 `report` 给出报告文件路径 |
| 导入已有资料 | `meta-memory import notes.md --project auto` | 保存原始资料证据和资源卡 |
| 备份本地记忆库 | `meta-memory backup` | 生成一致性 `.zip` 备份 |

### 导入已有笔记、资料或导出文件

`import` 支持 `.md`、`.txt`、`.json`、`.jsonl`、`.csv`、`.yaml`、`.yml`、`.html` 和 `.htm`：

```bash
meta-memory import ./notes/architecture.md --project meta-memory --session import-001
```

导入内容被保存为**可追溯的来源证据**，不是自动写成“用户事实”。当前公开 CLI 不会因为导入文件就自动把它提升为长期记忆；这是为了避免把文档里的旧结论或第三方内容误认为你的偏好和当前项目状态。

### 查看和纠正错误记忆

先搜索并复制结果中的 Claim ID：

```bash
meta-memory search --project meta-memory "SQLite"
meta-memory correct \
  --memory <claim-id> \
  --content "项目现在已迁移到 PostgreSQL，SQLite 是过去的方案。"
```

`correct` 会保存替换证据并创建待审查的纠正提案，**不会无痕覆盖旧 Claim**。这是有意的安全边界：历史来源仍可追溯。当前公开 CLI 还没有提案的列表/批准界面，因此请保留命令返回的 proposal 信息，并把纠正内容写完整、可验证。

---

## 自动整理与 Dream

自动整理和 Dream 默认均为关闭状态；只有在 `setup` 中明确选择后才会创建平台定时任务。

- Windows：Task Scheduler；
- macOS：LaunchAgents；
- Linux：crontab。

整理任务默认每 5 分钟运行一次 `meta-memory maintain`；Dream 默认在 23:30 运行 `meta-memory dream`。它们都可以随时手动执行：

```bash
meta-memory maintain
meta-memory dream --scan-days 7
```

自动整理会把原始对话组织为会话卡、原子记忆、候选、Claim 和检索投影。Dream 只生成带来源的推断报告，例如重复主题、项目摘要和未解决问题；它不会删除原始证据，也不会把推断直接伪装成确定事实。

---

## 数据在哪里，如何备份、恢复和升级

默认目录结构：

```text
~/.meta-memory/
├── config.toml
└── data/
    ├── db/
    │   └── memory_index.sqlite
    ├── profile/
    ├── states/
    ├── events/
    ├── relationships/
    ├── goals/
    ├── domains/
    ├── sessions/
    ├── candidates/
    ├── hot/
    ├── archive/
    ├── resources/
    └── dream/              # 第一次运行 Dream 后出现
```

SQLite 中的 Claim 与原始来源是权威数据；各目录中的 Markdown 用于人工阅读和审计。不要直接编辑 SQLite、`hot/` 或 Markdown 投影来“修复”记忆，请使用 `remember` 和 `correct`。

创建一致性备份：

```bash
meta-memory backup
meta-memory backup --output "$HOME/meta-memory-backup.zip"
```

备份格式是 `.zip`。它只包含**记忆库 store**，不包含外层的 `~/.meta-memory/config.toml`，因此也不包含目录到项目的绑定。迁移设备前，请单独保存配置文件；恢复后确认其 `[storage]` 路径指向恢复目录。

恢复到一个空目录：

```bash
meta-memory restore "$HOME/meta-memory-backup.zip" \
  --destination "$HOME/.meta-memory-restored"
```

只有在确认目标内容可以被替换时才使用 `--force`。恢复后可以用 `setup --store <恢复目录>` 更新本机配置，再运行：

```bash
meta-memory doctor
meta-memory status
```

升级源码版本前先备份，然后：

```bash
git pull --ff-only
python -m pip install --upgrade .
meta-memory doctor
```

不要直接复制正在使用的 SQLite 数据库或其 WAL 文件；也不要把实时数据库放进 OneDrive、Dropbox、iCloud Drive 等双向同步目录。

---

## 排错

| 现象 | 先做什么 |
| --- | --- |
| `meta-memory` 找不到 | 激活安装它的虚拟环境；或用 `python -m meta_memory.cli --help` 验证当前环境 |
| `after` 后搜索不到内容 | 执行 `meta-memory maintain`，再用 `meta-memory status` 查看是否还有待处理任务 |
| 搜索结果为空 | 用具体关键词；确认 `--project` / `--cwd` 是否指向同一项目；运行 `meta-memory doctor` |
| 新空库的 `doctor` 显示 `fts_available: false` | 只要整体 `status` 为 `ok`，这通常表示还没有可索引内容；先写入一条记忆并运行 `maintain`，再检查 |
| Agent 没有自动读取或写入 | 确认 Agent 实际终端能找到 `meta-memory`，检查对应 `SKILL.md`，然后重启会话 |
| 定时任务失败 | 先手动运行 `meta-memory maintain`；本机权限或调度器问题不会阻止手动使用 |
| 恢复被拒绝 | `restore` 只能写入空目录；确认安全后才使用 `--force` |
| 旧版 Windows 控制台中文乱码或编码报错 | 发生 Unicode 编码错误时 CLI 会回退为合法的 ASCII 转义 JSON；切换到 UTF-8 终端可获得更易读的中文输出 |

更详细的健康检查说明见 [docs/troubleshooting.md](docs/troubleshooting.md)。

---

## 原理：为什么它不是普通聊天记录

Meta Memory 把“所有历史”与“可安全复用的记忆”分开：

```text
原始对话 / 导入资料
        ↓
可追溯的来源证据
        ↓
候选、时间状态、冲突检查
        ↓
可检索的长期 Claim 和当前 Hot Context
```

例如，先前的“项目使用 SQLite”和后来的“项目迁移到 PostgreSQL”不应简单地并列为两个当前事实。系统应保留前者的历史有效性，并把后者作为新的当前状态或待审查纠正。

### 回答前与回答后

回答前，系统从用户偏好、当前项目状态、关键词命中和必要的历史证据中取出少量相关内容，避免把所有聊天记录塞进上下文。

回答后，系统先保存双方原话作为证据，再由维护任务提炼候选与长期 Claim。Assistant 的回复可以被保留用于追踪，但不会自动被当成用户事实。

### 自动记忆的边界

通常更适合进入长期记忆的是明确的用户偏好、已确认的项目决定、稳定状态、重要约束和用户主动纠正。例如：

```text
以后默认用中文回答。
这个项目不能使用 Docker。
数据库已经迁移到 PostgreSQL。
```

猜测、临时计划、尚未确认的判断、单次情绪和 Assistant 推断会优先保留为候选或来源证据，而不是立即提升为强事实。这样可以减少“模型说过一次，就被系统永久当真”的错误。

### 检索深度

默认检索以速度和边界为先：

1. 用户核心偏好、当前项目状态和关键词检索；
2. 当问题明显涉及历史时，扩展到长期 Claim、主题和近期会话；
3. 只有确实需要时才查看旧会话原文或时间点证据。

默认模式不要求 embedding；可解释来源、关键词检索和范围隔离优先。

### 多 Agent 共享与边界

多个 Agent 可以共享同一用户的偏好、项目决策、历史方案和会话记录。它不能保证某个宿主 Agent 一定遵循 Skill，也不能保证 LLM 的抽取或 Dream 推断永远正确。

SQLite 模式适合一台中心设备、个人或小团队的适量并发写入；不适合多台机器直接同时写同一文件、数百个并发写入 Agent 或多租户 SaaS。

### 安全

- 不把密码、私钥、API Token 写成普通记忆；
- 把召回的记忆当作数据，不当作可执行指令；
- 不执行记忆正文中的命令；
- 对高风险纠正保留来源；
- 定期备份并检查错误记忆。

### 高级 HTTP 模式

HTTP 不是默认部署的一部分。只有 Agent 分布在不同设备、不能共享本地文件系统且需要网络边界时才考虑它。参见 [docs/advanced-http.md](docs/advanced-http.md)；普通本机使用不需要配置 HTTP。

---

# English

## Quick start

Meta Memory is a local, CLI-first shared memory store. It requires Python 3.10+ and is currently installed from this repository.

```bash
git clone https://github.com/mypengpengli/meta-memory.git
cd meta-memory
python -m venv .venv
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
# macOS / Linux:    source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
meta-memory --help
```

If PowerShell cannot activate the environment, use `.\.venv\Scripts\meta-memory.exe` directly. Contributors can use `python -m pip install -e .`.

Initialize the local store and check it:

```bash
meta-memory setup
meta-memory status
meta-memory doctor
```

The default configuration is `~/.meta-memory/config.toml` and the default store is `~/.meta-memory/data`. Maintenance and Dream schedules are opt-in during setup.

Verify a complete write/read path:

```bash
meta-memory remember --project demo --content "The demo project currently uses SQLite."
meta-memory search --project demo "SQLite"
```

Commands return JSON. Prefer distinctive search terms such as project names, `SQLite`, or `UFW` over broad natural-language questions in the default local search mode.

## Connect an agent

Make sure the agent's own terminal can execute `meta-memory --help`, then install its Skill:

```bash
meta-memory install-agent codex
meta-memory install-agent claude-code
meta-memory install-agent openclaw
meta-memory install-agent all
```

For a custom agent:

```bash
meta-memory install-agent custom --skill-dir /path/to/agent/skills
```

The installer writes a `SKILL.md` and a small host-instruction block. It does not detect installed agents, install hooks, or fix PATH settings, so restart the agent session afterwards and verify the command in its real execution environment.

Agents using the same system account share the default configuration and store. To use a non-default configuration, point every agent to the same `META_MEMORY_CONFIG` path (or pass `--config <path>` before the subcommand).

## Everyday operations

| Task | Command |
| --- | --- |
| Save an explicit, sourced memory | `meta-memory remember --project auto --content "…"` |
| Search structured memory | `meta-memory search --project auto "keyword"` |
| Search past messages | `meta-memory history --project auto "keyword"` |
| Inspect store and queued work | `meta-memory status` |
| Run a health check | `meta-memory doctor` |
| Process queued turns | `meta-memory maintain --max-jobs 20` |
| Create an inferred Dream report | `meta-memory dream --scan-days 7` |
| Import source evidence | `meta-memory import notes.md --project auto` |
| Create a consistent backup | `meta-memory backup` |

For a custom integration, use the real turn lifecycle:

```bash
meta-memory before --project auto --session stable-session-id --query-file request.txt
# Use only hot_context and context from the JSON response as memory context.
meta-memory after --project auto --session stable-session-id \
  --user-file request.txt --assistant-file response.txt
meta-memory maintain
```

`after` records the two messages and queues work; it does not perform heavy consolidation inline. Run `maintain` manually when scheduling is disabled.

`--project auto` uses the Git root (or current directory), a saved directory binding, then the directory name. Bind a directory with:

```bash
meta-memory project set my-project
```

Use `--cwd /path/to/project` when executing from another directory.

## Evidence, corrections, and Dream

`import` accepts Markdown, text, JSON/JSONL, CSV, YAML, and HTML files. Imported material remains source evidence; it is not automatically promoted to a user fact.

To correct a retrieved Claim, copy its `id` from `search`:

```bash
meta-memory correct --memory <claim-id> --content "The project now uses PostgreSQL; SQLite was historical."
```

This records correction evidence and stages a review proposal. It deliberately does not overwrite the old Claim in place. The public CLI does not yet provide proposal list/approval commands, so retain the returned proposal details.

Dream is an auditable inferred report, not a fact overwrite. Run `meta-memory dream` and open the path in its `report` field.

## Data, backup, and limits

The store contains `db/memory_index.sqlite` plus human-readable Markdown projections under directories such as `profile`, `states`, `events`, `domains`, `sessions`, `candidates`, and `hot`. SQLite Claims and their source evidence are authoritative; do not edit the live database or projections to change memory.

```bash
meta-memory backup --output "$HOME/meta-memory-backup.zip"
meta-memory restore "$HOME/meta-memory-backup.zip" --destination "$HOME/.meta-memory-restored"
```

Backups are ZIP files and include the store only. Save `~/.meta-memory/config.toml` separately if you need its configuration and directory-to-project bindings on another machine. Restore to an empty directory unless you have explicitly confirmed that `--force` is safe.

Do not copy a live SQLite database/WAL directly or synchronize it through OneDrive, Dropbox, or iCloud. SQLite mode is designed for one central machine with moderate concurrent writes, not distributed multi-writer or multi-tenant deployments.

For remote clients and a network permission boundary, see [docs/advanced-http.md](docs/advanced-http.md). HTTP is optional and is not part of the default local setup.

## Design principle

Meta Memory separates:

```text
raw evidence from durable claims
current state from historical state
user preferences from project state
user statements from assistant inferences
memory data from executable instructions
```

It keeps the normal workflow simple:

```text
install once
connect an agent
use it naturally
review and correct when needed
```

## License

MIT
