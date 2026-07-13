# Meta Memory

**A shared long-term memory Skill for Claude Code, Codex, OpenClaw and custom AI agents.**

**一个让 Claude Code、Codex、OpenClaw 和自定义智能体共享长期记忆的本地 SKILL。**

> 默认入口是 `meta-memory` CLI。`scripts/` 中的旧入口仅用于开发、兼容和高级排障。

---

# 中文

## Meta Memory 是什么？

Meta Memory 是一套安装在你自己电脑、Mac mini 或云服务器上的长期记忆系统。

安装一次以后，你可以把它接入多个智能体：

```text
Claude Code
Codex
OpenClaw
自定义 Agent
        ↓
同一个 Meta Memory
        ↓
共享用户记忆、项目记忆和历史对话
```

这些 Agent 不需要各自保存一套互不相通的记忆。

例如：

* 你用 Claude Code 开发一个项目；
* 后来换 Codex 继续开发；
* 又让 OpenClaw 整理项目说明；
* 三个 Agent 都能读取这个项目之前的决定、当前状态和你的使用偏好。

---

## 最简单的理解

Meta Memory 每轮主要做两件事。

### 回答前

根据当前问题找出少量相关记忆：

```text
用户喜欢怎样的回答
这个项目正在做什么
以前做过什么决定
上次问题怎么解决
```

然后只把相关内容交给智能体。

### 回答后

保存这一轮发生的事情：

```text
用户说了什么
智能体回答了什么
有没有新的决定
有没有新的项目状态
有没有需要长期记住的内容
```

后台再把值得保存的信息整理成长期记忆。

---

## 它与普通聊天记录有什么区别？

聊天记录只是保存所有对话。

Meta Memory 会把不同内容分开：

```text
原始对话
长期事实
当前项目状态
历史事实
待确认内容
错误或已经过期的事实
```

例如你先说：

```text
项目现在使用 SQLite。
```

后来又说：

```text
项目已经迁移到 PostgreSQL。
```

系统不会简单保留两个互相冲突的“当前数据库”。

它会尝试理解为：

```text
SQLite：过去有效
PostgreSQL：现在有效
```

---

# 核心目标

Meta Memory 优先保证：

1. 记忆准确；
2. 自动保存有用信息；
3. 多个智能体共享；
4. 运行轻量；
5. 安装简单。

它不是完整的 Agent OS，也不会替代 Claude Code、Codex 或 OpenClaw。

它只专注一件事：

> 给所有智能体提供同一套可靠的长期记忆。

---

# 默认使用方式

Meta Memory 默认运行在一台设备上。

例如：

```text
Mac mini
├── Claude Code
├── Codex
├── OpenClaw
├── Meta Memory
└── 共享记忆数据库
```

所有 Agent 直接执行同一个本地命令：

```bash
meta-memory
```

默认不需要：

* HTTP API；
* MCP；
* Token；
* 多用户权限系统；
* 独立向量数据库；
* 多个常驻 Worker。

远程 API 仍然可以作为高级可选功能使用。

---

# 安装

## 方式一：Python 安装

```bash
pip install meta-memory
```

然后运行：

```bash
meta-memory setup
```

安装程序会询问：

```text
你的名称
记忆保存位置
是否自动整理
是否开启夜间 Dream
需要接入哪些智能体
```

示例：

```text
User: Li Peng
Store: ~/.meta-memory
Automatic maintenance: Yes
Dream: Every night at 23:30
Agents: Claude Code, Codex, OpenClaw
```

安装程序会自动：

* 初始化数据库；
* 创建配置；
* 安装定时整理；
* 安装 Dream；
* 给选中的 Agent 安装 SKILL；
* 添加必要的常驻调用说明；
* 检查安装是否正常。

---

## 方式二：Docker

```bash
docker compose up -d
```

然后：

```bash
docker compose exec meta-memory meta-memory setup
```

Docker 适合云服务器或不希望管理 Python 环境的用户。

本机上的 Agent 最好通过项目提供的 wrapper 调用：

```bash
meta-memory before ...
```

而不需要知道 Meta Memory 实际运行在 Docker 中。

---

# 接入智能体

## 一键接入

```bash
meta-memory install-agent claude-code
meta-memory install-agent codex
meta-memory install-agent openclaw
```

全部安装：

```bash
meta-memory install-agent --all
```

自定义 Agent：

```bash
meta-memory install-agent custom \
  --skill-dir /path/to/agent/skills
```

安装命令会：

1. 安装或链接 Meta Memory SKILL；
2. 写入共享数据目录；
3. 添加每轮调用说明；
4. 测试 Agent 是否可以执行 `meta-memory`；
5. 不创建新的独立数据库。

所有 Agent 使用同一套记忆。

---

# 新增一个 Agent 需要做什么？

通常只需要运行：

```bash
meta-memory install-agent <agent-name>
```

例如：

```bash
meta-memory install-agent codex
```

不需要：

* 创建新用户；
* 创建新 Token；
* 创建新数据库；
* 复制旧记忆；
* 配置向量数据库；
* 启动新服务。

---

# 普通用户只需要理解三个概念

## 用户

表示这些记忆属于谁。

例如：

```text
Li Peng
```

用户记忆可以包括：

* 回答语言；
* 写作偏好；
* 长期习惯；
* 长期目标；
* 稳定个人信息。

这些记忆默认可以被所有已接入 Agent 读取。

---

## 项目

表示当前正在处理的事情。

例如：

```text
meta-memory
company-ai
novel
cloud-server
family-health
```

项目记忆可以包括：

* 当前状态；
* 技术栈；
* 项目决定；
* 任务进度；
* 项目问题；
* 历史解决方案。

不同项目的状态不会默认混在一起。

---

## 会话

表示当前这一次对话或任务。

例如：

```text
claude-code:2026-07-13:001
codex:2026-07-13:002
```

会话用于：

* 保存原始对话；
* 继续当前任务；
* 查找上次讨论；
* 避免不同对话互相混合。

通常由 Agent 自动生成，用户不需要手工管理。

---

# 项目如何确定？

Meta Memory 默认自动判断项目。

判断顺序：

1. 当前 Git 仓库；
2. 当前工作目录绑定；
3. Agent 显式提供；
4. 默认项目。

例如你在：

```text
/home/li/projects/meta-memory
```

目录中使用 Claude Code，Meta Memory 可以自动使用项目：

```text
meta-memory
```

手工绑定当前目录：

```bash
meta-memory project set meta-memory
```

查看当前项目：

```bash
meta-memory project current
```

---

# 智能体每轮会做什么？

接入后的 Agent 应遵循固定流程。

## 回答前

运行：

```bash
meta-memory before \
  --project auto \
  --session <session-id> \
  --query-file request.txt
```

Meta Memory 会自动判断需要搜索到什么程度。

普通问题可能只读取：

* 用户核心偏好；
* 当前项目状态；
* 精确关键词结果。

涉及历史时可能进一步搜索：

* 以前的长期记忆；
* 旧会话；
* 历史项目决定；
* 某个时间点的状态。

返回结果主要有：

```text
hot_context
context
```

智能体只应把这两部分作为记忆上下文。

---

## 回答后

运行：

```bash
meta-memory after \
  --project auto \
  --session <session-id> \
  --user-file request.txt \
  --assistant-file response.txt
```

这个命令会快速完成：

* 保存用户消息；
* 保存 Assistant 回复；
* 记录 Agent 来源；
* 创建后台整理任务。

它不会让用户等待 Dream 或大型整理。

---

## 用户明确说“记住”

运行：

```bash
meta-memory remember \
  --project auto \
  --session <session-id> \
  --content "Meta Memory 默认使用 SQLite。"
```

显式 Remember 会优先进入长期记忆，并保证下一次可以读取。

---

## 用户说旧记忆不对

运行：

```bash
meta-memory correct \
  --memory <memory-id> \
  --content "项目现在已经迁移到 PostgreSQL。"
```

系统会判断这是：

* 原事实错误；
* 状态发生变化；
* 需要用户进一步确认。

旧记忆不会被无痕覆盖，来源和历史仍然可以追溯。

---

# 搜索深度

Meta Memory 不会每轮扫描全部历史。

它会自动选择搜索深度。

## 轻量搜索

每轮默认执行：

```text
核心用户记忆
当前项目状态
FTS 关键词检索
```

不调用 LLM，速度优先。

## 普通搜索

问题明显涉及以前的内容时：

```text
长期 Claim
Chunk
相关主题
相关实体
近期 Session
```

## 深层搜索

只有真正需要时：

```text
旧会话原文
历史时间点
跨项目用户偏好
原始证据
```

这保证了：

* 上下文不会太大；
* 回答不会太慢；
* 旧内容不会无缘无故干扰当前任务。

---

# 自动记忆

Meta Memory 默认开启自动记忆。

这不代表把每句话都当成正式事实。

## 通常会自动长期保存

用户明确表达的：

* 长期偏好；
* 项目决定；
* 当前状态；
* 确定结果；
* 重要约束；
* 明确纠正。

例如：

```text
以后给我中文回答。
这个项目不使用 Docker。
数据库已经迁移到 PostgreSQL。
```

---

## 通常会先保存为候选

以下内容不会立即作为强事实：

* 用户猜测；
* 临时计划；
* 尚未确认的判断；
* Assistant 推断；
* 自动生成的总结；
* 单次情绪；
* 预测。

后续有更多证据或用户确认时，再升级为正式记忆。

---

## Assistant 回复不是用户事实

Meta Memory 会保存 Assistant 回复，方便回看和追踪。

但 Assistant 自己说出的内容不会自动变成：

* 用户身份；
* 用户偏好；
* 真实世界事实。

这能减少 Agent 自己“编出一条记忆，然后以后又把它当真”的问题。

---

# 后台自动整理

默认安装一个轻量定时任务。

例如每五分钟执行：

```bash
meta-memory maintain
```

它会处理：

```text
新对话
→ Session Card
→ 原子记忆
→ 去重
→ 冲突检查
→ 长期 Claim
→ 更新索引
→ 更新下一会话的 Hot Memory
```

这个任务不是大型常驻服务。

没有 Agent 使用时，它几乎不消耗资源。

---

# Dream

Dream 用来做更高层的整理。

默认每天夜间运行：

```bash
meta-memory dream
```

Dream 不只是简单总结当天聊天。

它会观察一段时间内的记忆，尝试发现：

* 稳定用户偏好；
* 项目阶段总结；
* 多次重复的方法；
* 重要决定；
* 尚未解决的冲突；
* 经常被提到的主题。

Dream 可以生成：

```text
用户摘要
项目摘要
流程候选
未解决问题
```

Dream 产生的推论会保留来源，并标记为系统推断。

它不会：

* 删除原始证据；
* 无来源地改写事实；
* 自动执行外部操作；
* 直接修改其他 Agent 的 Skill；
* 把所有推断当成确定事实。

手工运行：

```bash
meta-memory dream
```

查看最近 Dream：

```bash
meta-memory dream show
```

---

# 查询以前的对话

搜索长期记忆：

```bash
meta-memory search \
  --project meta-memory \
  "项目数据库是什么？"
```

搜索历史会话：

```bash
meta-memory history \
  --project meta-memory \
  "之前是怎么解决 UFW 问题的？"
```

查看当前项目摘要：

```bash
meta-memory project show meta-memory
```

---

# 查看和纠正记忆

列出最近记忆：

```bash
meta-memory memories recent
```

搜索：

```bash
meta-memory memories search "PostgreSQL"
```

查看来源：

```bash
meta-memory memories show <memory-id>
```

标记有用：

```bash
meta-memory memories helpful <memory-id>
```

标记错误：

```bash
meta-memory memories incorrect <memory-id>
```

标记过期：

```bash
meta-memory memories outdated <memory-id>
```

---

# 数据保存在什么地方？

默认目录：

```text
~/.meta-memory/
├── config.toml
├── data/
│   ├── db/
│   │   └── memory.sqlite
│   ├── memories/
│   ├── hot/
│   ├── sessions/
│   ├── archive/
│   └── resources/
└── backups/
```

SQLite 保存：

* 原始事件；
* 会话；
* Claims；
* 来源关系；
* 检索索引；
* 后台任务。

Markdown 保存人能阅读的记忆投影。

---

# 数据库和 Markdown 谁是准的？

Claims 和原始证据是系统中的权威数据。

Markdown 是人类可读投影。

不要直接编辑：

```text
hot/
```

如果手工修改普通记忆 Markdown，应执行：

```bash
meta-memory reindex
```

更推荐通过：

```bash
meta-memory correct
meta-memory remember
```

修改长期记忆。

---

# 多个智能体能做什么？

多个 Agent 可以共享：

* 用户长期偏好；
* 同一项目的当前状态；
* 历史决定；
* 以前的解决方案；
* 会话记录；
* 已确认的流程；
* Dream 项目摘要。

例如：

```text
Claude Code 完成数据库迁移
→ Meta Memory 保存结果
→ Codex 下次可以直接知道已经迁移
→ OpenClaw 可以据此更新项目文档
```

---

# 多个智能体不能保证什么？

Meta Memory 无法强制所有 Agent 一定正确调用 SKILL。

因此安装程序会同时尝试：

* 安装 SKILL；
* 写入全局常驻调用说明；
* 安装宿主支持的 Hook。

如果某个 Agent 不遵循调用规则，它可能：

* 不读取记忆；
* 不保存当前对话；
* 错过自动整理。

Meta Memory 也不能保证：

* LLM 每次抽取都完全正确；
* 用户说的所有内容本身都真实；
* 自动 Dream 的每条推断都正确；
* 多台独立设备可以同时直接写同一个 SQLite；
* 记忆可以代替密码管理器；
* 记忆可以代替完整文件知识库。

---

# 使用限制

当前默认 SQLite 模式适合：

* 一台中心设备；
* 大约几个到十几个 Agent；
* 多读、适量并发写；
* 个人和家庭使用；
* 项目开发；
* 长期个人记忆。

不适合：

* 多台服务器同时直接写 SQLite；
* 数百个并发写入 Agent；
* 大规模多租户 SaaS；
* 通过网盘实时同步数据库。

---

# 多设备怎么使用？

推荐：

```text
所有 Agent 都在一台中心设备运行
```

或者：

```text
远程设备通过高级 HTTP 模式访问中心设备
```

不推荐：

```text
设备 A 一份 SQLite
设备 B 一份 SQLite
然后通过 OneDrive 或 iCloud 双向同步
```

实时 SQLite 不应放入：

* OneDrive；
* Dropbox；
* iCloud Drive；
* 普通双向同步目录。

---

# 换电脑或服务器

## 创建备份

```bash
meta-memory backup
```

指定文件：

```bash
meta-memory backup \
  --output ~/backups/meta-memory-backup.tar.zst
```

备份应包括：

* SQLite；
* Markdown；
* 配置；
* 项目目录映射；
* Dream 数据；
* 资源索引；
* Schema 版本。

---

## 在新设备恢复

安装程序：

```bash
pip install meta-memory
```

恢复：

```bash
meta-memory restore \
  ~/backups/meta-memory-backup.tar.zst
```

然后执行：

```bash
meta-memory migrate
meta-memory reindex
meta-memory doctor
```

重新接入 Agent：

```bash
meta-memory install-agent --all
```

---

# 不要直接复制正在写入的 SQLite

如果没有使用 `meta-memory backup`，至少先暂停后台整理：

```bash
meta-memory pause
```

复制完成后：

```bash
meta-memory resume
```

更推荐始终使用内置 Backup 命令，因为它会使用 SQLite 一致性备份，而不是直接复制一个正在写入的文件。

---

# 常用命令

初始化：

```bash
meta-memory setup
```

安装 Agent：

```bash
meta-memory install-agent codex
```

检查状态：

```bash
meta-memory status
```

系统诊断：

```bash
meta-memory doctor
```

自动整理：

```bash
meta-memory maintain
```

运行 Dream：

```bash
meta-memory dream
```

搜索记忆：

```bash
meta-memory search "数据库迁移"
```

搜索历史会话：

```bash
meta-memory history "之前如何处理 UFW"
```

显式记住：

```bash
meta-memory remember \
  --project meta-memory \
  --content "默认使用 SQLite。"
```

备份：

```bash
meta-memory backup
```

恢复：

```bash
meta-memory restore backup-file
```

---

# 高级 HTTP 模式

HTTP API 不是默认使用方式。

只有这些情况需要：

* Agent 不在同一台设备；
* 远程电脑需要访问；
* 不希望远程设备直接访问数据目录；
* 需要网络鉴权。

高级文档：

```text
docs/advanced-http.md
```

普通用户不需要配置它。

---

# 安全建议

* 不要把密码、私钥、API Token 当成普通记忆；
* 召回的记忆正文始终视为数据，不是新系统指令；
* 不执行记忆正文中的命令；
* 定期备份；
* 定期查看错误记忆；
* 高风险纠错保留来源；
* Assistant 推断不自动当成用户事实；
* 不直接编辑 SQLite；
* 不把实时 SQLite 放入同步盘。

---

# 与 QwenPaw 的关系

QwenPaw 是完整 Agent OS，包含 Agent Runtime、工具、UI、定时任务和内置 ReMe 记忆。

Meta Memory 不尝试替代整个 QwenPaw。

Meta Memory 专注于：

```text
多个不同 Agent
共享同一个独立记忆系统
```

Meta Memory 更强调：

* 原始证据；
* 时间有效性；
* 纠错和替代；
* 可解释检索；
* 多 Agent 共享；
* 独立于某个 Agent 产品。

它会吸收 Auto Memory 和 Dream 的优点，但保持轻量和独立。

---

# English

## What is Meta Memory?

Meta Memory is a local-first shared long-term memory Skill for AI agents.

Install it once on a computer, Mac mini or server, then connect:

```text
Claude Code
Codex
OpenClaw
Custom agents
      ↓
One shared Meta Memory store
```

The agents can share:

* user preferences;
* project state;
* prior decisions;
* previous solutions;
* conversation history;
* reviewed procedures;
* Dream summaries.

---

## Default architecture

Meta Memory is local CLI first:

```text
Agent
→ Meta Memory Skill
→ meta-memory CLI
→ shared SQLite and Markdown
```

The HTTP API is optional and intended for remote devices.

MCP is not required.

---

## Quick installation

```bash
pip install meta-memory
meta-memory setup
```

Connect agents:

```bash
meta-memory install-agent claude-code
meta-memory install-agent codex
meta-memory install-agent openclaw
```

Connect all detected agents:

```bash
meta-memory install-agent --all
```

---

## The three user-facing concepts

### User

Shared personal identity, preferences and durable habits.

### Project

Project-specific state, decisions, progress and history.

### Session

One current conversation or task.

Internal concepts such as claims, projections and leases are hidden from normal users.

---

## Per-turn lifecycle

Before answering:

```bash
meta-memory before \
  --project auto \
  --session <session-id> \
  --query-file request.txt
```

After answering:

```bash
meta-memory after \
  --project auto \
  --session <session-id> \
  --user-file request.txt \
  --assistant-file response.txt
```

Explicit memory:

```bash
meta-memory remember \
  --project auto \
  --session <session-id> \
  --content "The project currently uses SQLite."
```

---

## Automatic memory

Meta Memory records conversations as evidence first.

Clear user statements, decisions and state changes may be promoted automatically.

Guesses, temporary plans, assistant inferences and uncertain statements remain candidates until confirmed.

Assistant output is not automatically treated as a user fact.

---

## Background maintenance

A lightweight scheduled task runs:

```bash
meta-memory maintain
```

It processes:

```text
events
→ session cards
→ atomic memory units
→ deduplication
→ claims
→ search indexes
→ hot memory
```

A permanent API or multiple worker services are not required for the default local mode.

---

## Dream

Dream runs periodically:

```bash
meta-memory dream
```

It can create:

* user summaries;
* project digests;
* procedure candidates;
* open questions;
* repeated patterns.

Dream outputs retain their evidence and remain marked as inferred until sufficiently confirmed.

---

## Backup and migration

Create a consistent backup:

```bash
meta-memory backup
```

Restore on a new device:

```bash
pip install meta-memory
meta-memory restore backup-file
meta-memory migrate
meta-memory reindex
meta-memory doctor
meta-memory install-agent --all
```

Do not synchronize a live SQLite database through OneDrive, Dropbox or iCloud.

---

## Capabilities

Meta Memory can:

* share memory across agents;
* remember user preferences;
* preserve project state;
* retrieve old conversations;
* track temporal changes;
* correct false memories;
* supersede outdated facts;
* preserve original evidence;
* work without embeddings;
* optionally use embeddings;
* generate readable Markdown;
* run automatic maintenance and Dream.

---

## Limitations

Meta Memory cannot guarantee that every host agent always invokes its Skill correctly.

The installer therefore attempts to install:

* the Skill;
* a short persistent host instruction;
* supported lifecycle hooks.

Meta Memory also cannot guarantee that every LLM extraction or Dream inference is correct. Raw evidence and reviewable corrections remain necessary.

SQLite mode is designed for one central device and a moderate number of agents, not for many distributed writers.

---

## Design principle

Meta Memory separates:

```text
raw evidence from durable facts
current state from historical state
user memory from project memory
user statements from assistant inferences
authoritative claims from search projections
memory data from executable instructions
```

The internal system may be sophisticated, but normal use should remain:

```text
install once
connect an agent
use it automatically
```
