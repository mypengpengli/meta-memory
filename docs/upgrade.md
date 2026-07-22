# 升级到 2.8

2.8 新增远端 Agent、家庭/人物/设备共享状态、二进制资产、版本化地图和空间语义。
现有本地 Agent、Claim、Turn、备份和命令保持兼容；数据库会依次通过迁移
`024`、`025`、`026` 前进，不会把现有 Claim 改写成新 channel 数据。当前最新
迁移是 `026`。

## 推荐步骤

```bash
git pull
python -m pip install --upgrade .
meta-memory doctor
meta-memory agent upgrade-status --all
meta-memory agent sync --all
meta-memory schedule install
meta-memory overview
```

`doctor` 应确认最新迁移为 `026`。Overview 会新增 shared activity、current state、
asset、map 与 spatial observation 计数。原来的本地 Skill contract 不变，但建议
sync，确保安装记录与当前包版本一致。

## 启用远端模式

远端服务不会因升级自动暴露。管理员需要显式完成：

1. `meta-memory shared init` 创建 audience/channel；
2. 用 `meta-memory init-agents-file` 建立私有 agents 文件，并从上一步复制真实
   `profile_id`、`audience_id`、`channel_id`；
3. 为每个 Agent 设置不同 Token 环境变量；
4. 用 `meta-memory serve` 启动唯一中央服务并通过 HTTPS 发布；
5. 在远端电脑运行 `install-remote-agent` 并重启宿主；
6. 完成一个真实 Turn，再确认远端 `status` 为 active。

完整流程见 [Hosted Meta Memory](advanced-http.md)。不要让多个设备通过共享盘直接
打开同一个 SQLite 文件；它们应全部调用中央服务。Heartbeat/Dream 也只在服务器
安装一次。

纯服务器请用下面的检查方式；普通 Overview 会检查本地 Agent Skill，因此在没有
本地 Agent 的服务器上可能有意显示 `NEEDS_ACTION`：

```bash
meta-memory overview --server --agents-file "$HOME/.meta-memory/agents.json"
```

## 数据、备份和资产

资产字节存放在 store 下的 `assets/objects`，所以普通 `meta-memory backup` 会连同
SQLite 和资产一起备份。上传中的分块位于 `assets/uploads`；完成后只保留幂等完成
凭据。恢复后执行：

```bash
meta-memory doctor
meta-memory overview
meta-memory shared expire
```

如果使用远端 Agent，升级客户端包后重新运行 `install-remote-agent` 以刷新 Skill
和 launcher；Token 值仍只留在环境变量，不会写入生成文件。

## 2.7 升级行为仍适用

- Agent 文件安装、launcher 验证和真实 host lifecycle 是三件独立事实；
- `before → draft → after → send` 顺序不变；
- 自动建议仍进入可审核 inbox；
- Heartbeat 默认做增量整理，Deep Dream 保持可追溯；
- 已完成跨 Agent 摘要只在延续意图下有界召回。
