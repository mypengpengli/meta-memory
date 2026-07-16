# 升级到 2.7

2.7 保持现有 `before`、`after`、`remember`、`search`、`history` 和 `correct`
命令可用。此次升级重点修复源码目录之外的冷启动，并更新 Agent Skill 接入合同；
现有本地库会在第一次访问时继续自动应用新增迁移。

## 推荐步骤

```bash
python -m pip install --upgrade .
meta-memory agent upgrade-status
meta-memory agent sync --all
meta-memory schedule install
meta-memory overview
meta-memory agent status --all --verbose
```

`agent sync --all` 对 2.7 是必要步骤：它会重新生成引用当前 Python、配置文件和
新版 Skill 合同的本地 launcher。仓库、虚拟环境或 Python 位置变化后也应再次执行；
后台任务启用时再运行 `schedule install` 刷新其 launcher。

## 需要注意的行为变化

- 自动抽取会优先判断内容是忽略、仅会话保留还是长期候选；明确 `remember` 的内容不受该过滤影响。
- 普通 `history` 返回受限的完成会话摘要。使用 `history show` 或 `--detail` 才读取有限的详细内容。
- Deep Dream 使用 `meta-memory dream deep --scan-days 7`；先加 `--dry-run` 可预览。没有来源或来源未变化的 scope 会返回 `idle`。
- `overview` 是人类优先的状态入口；已有脚本可继续使用 `--json` 和旧 JSON 字段。
- 安装文件、launcher 验证和宿主实际执行回合现在是三个独立状态。重启 Agent 并完成
  一个普通回合后，使用 `agent status --all --verbose` 确认 `last_before` 与
  `last_after` 均已更新，才表示端到端自动接入生效。
- 任意支持本地 Skill、命令执行、回合内保存 `turn_id` 和 UTF-8 临时文件的 CLI
  Agent，都可以使用 `install-agent custom` 接入；具体步骤见
  [Agent 接入契约](agent-integration.md)。

## 数据和排程

运行数据保留与可选 compact 只清理已完成的运行性队列、日志和过期 Dream 报告，
不会默认删除原始会话证据或活动 Claim。保留期可通过
`meta-memory config list` 查看和修改。
