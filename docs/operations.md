# Meta Memory 实际操作指南

这份文档面向准备日常使用 Meta Memory 的人。它以“要完成什么”组织命令；
所有命令都可附加 `--json` 获取机器可读输出。

## 第一次使用

```bash
meta-memory setup
meta-memory install-agent codex
meta-memory agent sync --all
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
