# 参考入口

默认不要一次读很多参考文件。
正常回答时优先依赖运行时返回的 `context_markdown`，不是这些参考文档。

只有在运行时上下文不够、或需要人工审计时，才从这里选 1 份最相关的参考继续读。

先按目的选文件：

- 想知道“当前问题该读哪份顶层记忆”：`memory/index.md`
- 想知道“回答前怎么按问题类型取记忆，以及什么时候停止展开”：`loading-rules.md`
- 想知道“新信息该写到哪一层”：`writeback-rules.md`
- 想知道“整个系统分了哪些层，各层负责什么”：`memory-system.md`

只在上面 4 份仍然不够时，再继续读下面的细节文件：

- `topic-catalog.md`
  - 想补主题命名或 topic 规范时
- `slotting-guide.md`
  - 想更细地判断该落哪一层时
- `classification-draft.md`
  - 想看分类启发式时
- `network-memory-model.md`
  - 想看关联字段和来源网络时
- 各类 `*-topic-tree.md`
  - 只有在要细分某个领域时再读

停止规则：

- 已经知道下一步该读哪份记忆或该写回哪一层时，立即停止继续读参考
- 不要把 `reference-map.md -> loading-rules.md -> memory/index.md` 当成固定全读链路
