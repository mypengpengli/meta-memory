# 升级到 2.6

2.6 保持现有 `before`、`after`、`remember`、`search`、`history` 和 `correct` 命令可用，
并在第一次访问本地库时自动应用新增迁移。

## 推荐步骤

```bash
python -m pip install --upgrade .
meta-memory agent upgrade-status
meta-memory agent sync --all
meta-memory schedule install
meta-memory overview
```

如果仓库或虚拟环境的位置变化，`agent sync --all` 和 `schedule install` 很重要：它们会重新生成引用当前 Python、配置文件和 Skill 合同版本的本地启动器。

## 需要注意的行为变化

- 自动抽取会优先判断内容是忽略、仅会话保留还是长期候选；明确 `remember` 的内容不受该过滤影响。
- 普通 `history` 返回受限的完成会话摘要。使用 `history show` 或 `--detail` 才读取有限的详细内容。
- Deep Dream 使用 `meta-memory dream deep --scan-days 7`；先加 `--dry-run` 可预览。没有来源或来源未变化的 scope 会返回 `idle`。
- `overview` 是人类优先的状态入口；已有脚本可继续使用 `--json` 和旧 JSON 字段。

## 数据和排程

2.6 增加运行数据保留与可选 compact：它只清理已完成的运行性队列、日志和过期 Dream 报告，不会默认删除原始会话证据或活动 Claim。保留期可通过 `meta-memory config list` 查看和修改。
