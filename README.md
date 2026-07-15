# Meta Memory

**A local-first, shared long-term memory runtime for Claude Code, Codex, OpenClaw, and custom AI agents.**

**一个让 Claude Code、Codex、OpenClaw 和自定义智能体共享长期记忆的本地优先运行时。**

> Meta Memory is CLI-first and runs on a machine you control. The current repository supports Python 3.10+ and is installed from this source repository.

> **2.6:** Start with the practical [操作指南](docs/operations.md) and use the [升级指南](docs/upgrade.md) whenever the installed Agent or Python environment changes.

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

`setup` 默认启用每 10 分钟一次的增量 Heartbeat 和每天一次 Deep Dream；首次安装也可以先用
`--no-schedule` 只初始化数据，确认后再安装平台定时任务。非交互式示例：

```bash
meta-memory setup --name "Li Peng" --maintenance yes --dream yes --no-schedule --non-interactive
meta-memory overview
meta-memory doctor
```

终端中默认输出便于阅读的文本；管道、Agent 和 `--json` 使用稳定 JSON。`overview` 是日常状态入口，`doctor` 用来做健康检查。

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

在安装 Meta Memory 的 Python 环境中执行以下命令来安装对应 Skill：

```bash
meta-memory install-agent codex
meta-memory install-agent claude-code
meta-memory install-agent openclaw
```

一次安装所有**已检测到**的预设 Agent：

```bash
meta-memory install-agent all
```

自定义 Agent 需要明确它的 skills 根目录：

```bash
meta-memory install-agent custom --agent-id hermes \
  --skill-dir ~/.hermes/skills --host-file ~/.hermes/AGENTS.md

# 没有宿主说明文件、但支持 Skills 的 Agent 也可以接入。
meta-memory install-agent custom --agent-id my-agent \
  --skill-dir ~/.my-agent/skills --no-host-file
```

| Agent | 写入的位置 |
| --- | --- |
| Codex | `~/.codex/skills/meta-memory/SKILL.md` 和 `~/.codex/AGENTS.md` |
| Claude Code | `~/.claude/skills/meta-memory/SKILL.md` 和 `~/.claude/CLAUDE.md` |
| OpenClaw | `~/.openclaw/skills/meta-memory/SKILL.md` 和 `~/.openclaw/AGENTS.md` |

安装器会为每个 Agent 写入 Skill、宿主说明和专用 Launcher。例如 Codex 的
Launcher 位于 `~/.meta-memory/bin/meta-memory-codex`（Windows 为 `.cmd`），其中
固定了当前 Python、配置路径和 `agent_id=codex`。`install-agent all` 不会猜测未
安装的宿主；若需要某一个未检测到的 Agent，请显式执行它的安装命令。

安装结果中的 `cli_visible`、`config_visible` 和 `memory_status` 应为可用状态。安装
后重启 Agent 会话；也可直接运行安装结果给出的 Launcher 加 `status` 来核验。

同一系统账户下的 Agent 默认读取同一个配置和记忆库。专用 Launcher 会固定安装时的配置路径；如需使用非默认配置，请先通过同一个 `META_MEMORY_CONFIG` 环境变量，或 `meta-memory --config <path> install-agent <agent>`，为每个 Agent 安装/重装 Launcher，然后使用新生成的 Launcher。

### 集成者：每轮真实调用顺序

对自己实现的 Agent，使用下面的生命周期。默认 `--session auto` 会优先采用宿主
会话 ID，其次复用终端/父进程派生的本地会话；通常不需要手写会话 ID。

```bash
# 回答前：用户请求先原子落盘，再返回 turn_id、hot_context 和 context
meta-memory before \
  --project auto \
  --session auto \
  --query-file request.txt

# 生成完整回答草稿并写入 response.txt；发送给用户前保存同一份草稿
meta-memory after --turn <before 返回的 turn_id> --assistant-file response.txt

# 未启用定时任务时，手动处理排队的整理工作
meta-memory maintain
```

`before` 已经保存用户原话，因此 Agent 即使中断，用户请求也不会丢失。`after`
只快速保存 Assistant 草稿并入队，不会在用户等待时进行大型整理；相同 `turn_id`
和相同草稿可以安全重试。若 `after` 遇到暂时的 SQLite/文件错误，运行时会写入
本地 Spool，下一次 `maintain` 会自动重放。旧的 `after --session --user-file`
参数暂时兼容，但会返回 `legacy_after_arguments` 警告。

---

## 项目、会话和作用域

普通用户只需要理解三个概念：

| 概念 | 用途 | 例子 |
| --- | --- | --- |
| 用户 | 跨 Agent 的长期偏好、稳定习惯和身份相关信息 | “以后默认中文回答” |
| 项目 | 某件事的状态、技术决策和历史方案 | `meta-memory`、`company-ai` |
| 会话 | 一段连续对话或任务的原始记录 | `codex:2026-07-14:001` |

`--project auto` 时，Meta Memory 先以当前 Git 根目录（没有 Git 时为当前目录）作为工作区；如果该目录已经绑定项目名，就使用绑定。未绑定 Git 项目会优先使用 `origin` 远端地址生成稳定标识，普通克隆到新电脑后仍可找回同一项目；没有远端时使用“目录名 + 本机路径指纹”以避免两个同名目录串记忆。

自动会话默认 8 小时无活动后轮换。需要排查或主动切换时再使用：

```bash
meta-memory session current --project auto
meta-memory session new --project auto
meta-memory session close --project auto
```

若宿主设置了 `META_MEMORY_HOST_SESSION_ID`（或你显式传入非 `auto` 的 `--session`），它是权威会话 ID；`session new` 不会替宿主改写它，只会轮换终端/父进程派生的本地自动会话。

在项目根目录中绑定名称：

```bash
meta-memory project set meta-memory
meta-memory search --project auto "SQLite"
```

从别的目录操作某个项目时，传入 `--cwd`：

```powershell
meta-memory search --cwd "D:\work\meta-memory" --project auto "SQLite"
```

`search`、`history` 和 `before` 的输出都会带回实际解析到的 `project`；`before` 还会返回 `project_root`，可用于核对当前作用域。

---

## 常见操作

| 目的 | 命令 | 结果 |
| --- | --- | --- |
| 看当前状态与下一步 | `meta-memory overview` | Agent、队列、Heartbeat、Deep Dream 和明确的下一步 |
| 做健康检查 | `meta-memory doctor` | 迁移、FTS、阻塞 Claim 等检查 |
| 让对话入队后立即整理 | `meta-memory maintain --max-jobs 20` | 处理原始事件、候选和投影 |
| 显式保存一条记忆 | `meta-memory remember --project auto --content "…"` | 带来源立即写入并更新投影 |
| 搜索记忆与来源证据 | `meta-memory search --project auto "关键词"` | 返回 Claim、Dream 或资源证据；只有 `page_role: claim` 的 ID 可用于纠正 |
| 搜索/继续历史工作 | `meta-memory history recent --project auto` / `history search "关键词"` | 默认返回完成会话摘要；需要时再 `history show` |
| 审核自动记忆候选 | `meta-memory inbox list` | 查看、批准或拒绝待处理提案 |
| 生成近期 Dream 报告 | `meta-memory dream deep --scan-days 7 --dry-run` | 先预览，再生成来源可追溯的报告 |
| 导入已有资料 | `meta-memory import ./notes --recursive --changed-only --project auto` | 保存原始资料证据和资源卡 |
| 备份本地记忆库 | `meta-memory backup` | 生成一致性 `.zip` 备份 |
| 安装或检查定时任务 | `meta-memory schedule install` / `meta-memory schedule status` | 安装或显示当前平台的维护/Dream 任务 |

### 导入已有笔记、资料或导出文件

`import` 支持 `.md`、`.txt`、`.json`、`.jsonl`、`.csv`、`.yaml`、`.yml`、`.html` 和 `.htm`：

```bash
meta-memory import ./notes/architecture.md --project meta-memory
```

导入内容被保存为**可追溯的来源证据**，不是自动写成“用户事实”。可用 `meta-memory resource list/show/refresh/remove/export` 管理它们；导入文件不会自动提升为长期记忆，避免把文档里的旧结论或第三方内容误认为你的偏好和当前项目状态。
每个文件会使用由内容 Hash 派生的 `resource:<hash>` 合成会话，因此相同文件重试不会重复导入。

### 查看和纠正错误记忆

先搜索并复制结果中 `page_role` 为 `claim` 的 Claim ID；`resource:<...>` 是来源证据，不能直接作为 `correct --memory` 的参数：

```bash
meta-memory search --project meta-memory "SQLite"
meta-memory correct \
  --memory <claim-id> \
  --content "项目现在已迁移到 PostgreSQL，SQLite 是过去的方案。"
```

`correct` 会立刻保存替换证据并让新 Claim 可检索；旧 Claim 会标为 `corrected`
或 `superseded`，而不是被无痕覆盖。命令返回的 `new_claim_id` 可以用于核对下
一次检索结果。

---

## 自动整理与 Dream

自动整理和 Dream 默认均为启用状态；`setup` 会按配置安装平台定时任务，也可稍后
手动执行 `meta-memory schedule install`。

- Windows：Task Scheduler；
- macOS：LaunchAgents；
- Linux：crontab。

Heartbeat 默认每 10 分钟运行一次，只处理有新增或 dirty 的工作区；Deep Dream 默认在 23:30 运行。它们都可以随时手动执行：

```bash
meta-memory dream heartbeat
meta-memory dream deep --scan-days 7 --dry-run
meta-memory dream deep --scan-days 7
```

自动整理会把原始对话组织为结构化会话状态、原子记忆、候选、Claim 和检索投影。普通任务、催促和确认会留在会话层；稳定偏好、项目决策和可复用流程才会成为长期候选。Dream 只生成带来源的推断报告；无来源或来源未变化时返回 `idle`，不会产生空报告或空提示词节点。

`memory_mode` 位于配置的 `[behavior]` 区段，可取 `manual`、`conservative` 或
`automatic`（默认）。显式 `remember` 和 `correct` 同步生效；自动模式只会提升有
用户来源、已验证、低风险的内容。Assistant 推断、Agent 观察和导入资料保留来源及
范围，不会伪装成用户偏好。

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

当前可移植备份格式是 `.zip`，其中包含 `manifest.json`、`checksums.sha256`、`config.toml` 和完整 `store/`。SQLite 会通过一致性快照写入，WAL/SHM 等临时文件不会被打包。旧版仅含 `store/` 的归档仍可用于兼容恢复，但不会携带本机配置映射。

恢复到一个空目录：

```bash
meta-memory restore "$HOME/meta-memory-backup.zip" \
  --destination "$HOME/.meta-memory-restored"
```

恢复会先校验 Manifest、校验和与 SQLite 完整性，再把归档中的配置写到当前配置位置，并将 `[storage].path` 更新为恢复目标。只有在确认目标内容可以被替换时才使用 `--force`。恢复后运行：

```bash
meta-memory doctor
meta-memory status
meta-memory schedule install  # 需要恢复本机定时任务时
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
| Agent 没有自动读取或写入 | 在安装 Meta Memory 的环境中重新执行 `install-agent <agent>`；确认返回的专用 Launcher 可运行 `status`，检查对应 `SKILL.md`，然后重启会话 |
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

多个 Agent 可以共享同一用户的偏好、项目决策和结构化项目记忆。接续意图（如“继续、上次、之前做过”）会自动带入少量已完成、过滤后的工作区摘要；原始会话和跨 Agent 的详细历史仍需显式 `history show`/`--detail`，不会自动注入当前上下文。带 `Agent-observed` 标记的项目记忆来自可追溯的工具/Agent 证据，不等同于用户事实。它不能保证某个宿主 Agent 一定遵循 Skill，也不能保证 LLM 的抽取或 Dream 推断永远正确。

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

The default configuration is `~/.meta-memory/config.toml` and the default store is `~/.meta-memory/data`. Maintenance and Dream are enabled by default; use `--no-schedule` during setup if you want to verify the local data flow before installing platform tasks.

Verify a complete write/read path:

```bash
meta-memory remember --project demo --content "The demo project currently uses SQLite."
meta-memory search --project demo "SQLite"
```

Commands return JSON. Prefer distinctive search terms such as project names, `SQLite`, or `UFW` over broad natural-language questions in the default local search mode.

## Connect an agent

From the Python environment where Meta Memory is installed, install the Agent Skill:

```bash
meta-memory install-agent codex
meta-memory install-agent claude-code
meta-memory install-agent openclaw
meta-memory install-agent all
```

For a custom agent:

```bash
meta-memory install-agent custom --agent-id hermes \
  --skill-dir ~/.hermes/skills --host-file ~/.hermes/AGENTS.md
meta-memory install-agent custom --agent-id my-agent \
  --skill-dir ~/.my-agent/skills --no-host-file
```

The installer writes a `SKILL.md`, a small host-instruction block, and an Agent-specific launcher that pins the Python executable, config path, and agent id. `install-agent all` installs only detected built-in hosts; explicitly name a host to install it even when detection is unavailable. Check `cli_visible`, `config_visible`, and `memory_status` in the JSON result, then restart the Agent session.

Use lowercase `agent-id` values of 1–64 characters from `a-z`, `0-9`, `.`, `_`, and `-`; reserved/built-in impersonating names are rejected. To check an integration later without signing in to the host Agent:

```bash
meta-memory agent status --verbose
meta-memory agent status --all
meta-memory agent verify hermes
```

Agents using the same system account share the default configuration and store. An Agent launcher pins the config path used when it was installed. For a non-default configuration, use the same `META_MEMORY_CONFIG` value (or `meta-memory --config <path> install-agent <agent>`) to install or reinstall every Agent launcher, then use those regenerated launchers.

After an upgrade, repository move, or Python environment rebuild, run `meta-memory agent upgrade-status`, `meta-memory agent sync --all`, and `meta-memory schedule install` so every launcher and local task uses the current contract.

## Everyday operations

| Task | Command |
| --- | --- |
| Save an explicit, sourced memory | `meta-memory remember --project auto --content "…"` |
| Search structured memory | `meta-memory search --project auto "keyword"` |
| Inspect/manage a Claim | `meta-memory memory recent/show/archive/forget` |
| Review automatic proposals | `meta-memory inbox list/show/approve/reject` |
| Search completed session summaries | `meta-memory history --project auto "keyword"` |
| Read bounded completed-history detail when necessary | `meta-memory history --project auto "keyword" --detail` |
| Inspect store and queued work | `meta-memory status` |
| Run a health check | `meta-memory doctor` |
| Process queued turns | `meta-memory maintain --max-jobs 20` |
| Consolidate newly completed work now | `meta-memory dream heartbeat` |
| Run daily deep synthesis now | `meta-memory dream deep --scan-days 7` |
| Inspect Dream scheduling/state | `meta-memory dream status` |
| Import source evidence | `meta-memory import notes.md --project auto` |
| Inspect imported sources | `meta-memory resource list/show/refresh/remove` |
| Create a consistent backup | `meta-memory backup` |

For a custom integration, use the real turn lifecycle. `before` durably records the user request before retrieval, so use its returned `turn_id` and save the completed answer before sending that exact draft to the user:

```bash
meta-memory before --project auto --session auto --query-file request.txt
# Use only hot_context and context from the JSON response as memory context.
meta-memory after --turn <turn-id> --assistant-file response.txt
meta-memory maintain
```

`after` records the assistant reply and queues work; it does not perform heavy consolidation inline. Repeating the same turn id and reply is safe. Temporary completion failures are spooled and replayed by `maintain`. The old `after --session --user-file` form remains as a warned compatibility path.

The normal `history` query shares only completed, filtered session summaries within the same profile/workspace/subject; use `--detail` only to continue concrete prior work or when the summary is insufficient. Started Turns and assistant drafts are never shared, and a Turn may only be completed by the Agent that created it. Tool-backed project evidence is shown as `Agent-observed`; it is traceable operational evidence, not a user statement.

`before` can return an advisory `unfinished_previous_turn` warning after the configured five-minute threshold. Continue the current answer; for work that lasts a long time, renew its lease with `meta-memory turn touch <turn-id>`. The heartbeat first replays matching spool entries and only abandons inactive Turns after the configured threshold; `turn reopen` and `turn complete` provide an auditable late path. A `project_identity_mismatch` warning never merges projects automatically—check the directory binding with `meta-memory project set` instead.

`--project auto` uses the Git root (or current directory), then a saved directory binding. Unbound Git repositories use a stable `origin` remote fingerprint, so a normal clone can recall the same project after a portable restore even when its checkout directory has a different name. Other directories use a basename plus path fingerprint to avoid same-name collisions. Bind a directory with:

```bash
meta-memory project set my-project
```

Use `--cwd /path/to/project` when executing from another directory.

## Evidence, corrections, and Dream

`import` accepts Markdown, text, JSON/JSONL, CSV, YAML, and HTML files. Imported material remains source evidence; it is not automatically promoted to a user fact.

To correct a retrieved Claim, copy the `id` only from a result whose `page_role` is `claim`. `resource:<...>` and `dream:<...>` results are evidence/digests, not correctable Claims:

```bash
meta-memory correct --memory <claim-id> --content "The project now uses PostgreSQL; SQLite was historical."
```

This records correction evidence, makes the replacement Claim readable immediately, and preserves the old Claim as corrected or superseded rather than overwriting it in place. Keep the returned `new_claim_id` for auditing.

Dream has a lightweight heartbeat (every 10 minutes by default) and a daily deep synthesis. The heartbeat replays spool work, processes completed Turns, updates Claims/session summaries/Hot Memory, and returns `idle` without a full scan when nothing is dirty. The deep phase is an auditable inferred report, not a fact overwrite:

```bash
meta-memory dream heartbeat
meta-memory dream deep --scan-days 7 --dry-run
meta-memory dream deep --scan-days 7
meta-memory dream status
meta-memory config set dream.heartbeat_interval_minutes 10 --apply
```

Valid heartbeat intervals are 1–10080 minutes. `config set ... --apply` refreshes the scheduler immediately; without it, the response explicitly states that a schedule refresh is required.

## Data, backup, and limits

The store contains `db/memory_index.sqlite` plus human-readable Markdown projections under directories such as `profile`, `states`, `events`, `domains`, `sessions`, `candidates`, and `hot`. SQLite Claims and their source evidence are authoritative; do not edit the live database or projections to change memory.

```bash
meta-memory backup --output "$HOME/meta-memory-backup.zip"
meta-memory restore "$HOME/meta-memory-backup.zip" --destination "$HOME/.meta-memory-restored"
```

Current portable backups are verified ZIP files containing `manifest.json`, `checksums.sha256`, `config.toml`, and the complete store. Restore validates the archive and SQLite snapshot, writes the archived configuration to the active config path, and repoints its storage path to `--destination`. Legacy store-only archives remain restorable for compatibility, but do not carry the configuration/project mapping. Restore to an empty directory unless you have explicitly confirmed that `--force` is safe; then run `meta-memory doctor`, `meta-memory status`, and `meta-memory schedule install` if this machine needs local schedules.

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
