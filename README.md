# Meta Memory

`meta-memory` 是一个面向对话代理的“分层记忆 Skill”，不是普通的提示词集合。

它的目标是：

- 在对话中按需要加载记忆，而不是一次性塞进全部上下文
- 把记忆按领域、主题、项目、会话来组织
- 允许一条记忆同时关联多个主题、多个项目、多个来源
- 在回答结束后，把新信息先写入会话记忆或候选池，再决定是否沉淀为长期记忆

这个仓库当前只放 **Skill 本体**，不放你的真实个人记忆正文数据库。

## 当前状态

当前这版已经是一个可用的 Skill 骨架，而不是空壳。

已完成：

- `SKILL.md` 已定义读取和写回原则
- 已定义“领域 + 记忆性质 + 主题网络”三轴模型
- 已提供 7 个根领域草案
- 已提供每个根领域下的第一版典型主题清单
- 已提供“这句话应该放哪”的放置指南
- 已提供基础脚本骨架，可用于后续索引、候选规范化、维护
- Skill 结构已通过校验

当前还不是最终版：

- 还没有填入真实长期记忆内容
- 还没有完整的 SQLite 索引 schema 文档
- 主题清单仍然只是第一版草案，还没有经过长期使用筛选
- 还没有把“自动整理频率”和“升降级规则”细化成最终执行策略

## 这套 Skill 的核心思路

这套 Skill 不把记忆理解为单一总文档，而是理解为一个分层、分域、可路由的系统。

### 三个维度

1. 领域

- 工作
- 学习
- 家庭与子女教育
- 日常生活
- 健康
- 财务与消费
- 人际与关系

2. 记忆性质

- 固定记忆
- 领域记忆
- 主题记忆
- 项目记忆
- 会话记忆
- 候选记忆
- 归档索引

3. 主题网络

- 标签
- 关联主题
- 关联项目
- 关联来源

也就是说：

- 文件组织是树
- 召回逻辑是树 + 主题网络混合

## 仓库结构

```text
meta-memory/
├── SKILL.md
├── README.md
├── agents/
│   └── openai.yaml
├── assets/
│   └── templates/
│       └── memory-note.template.md
├── references/
│   ├── memory-system.md
│   ├── loading-rules.md
│   ├── writeback-rules.md
│   ├── classification-draft.md
│   ├── network-memory-model.md
│   ├── slotting-guide.md
│   └── memory/
│       ├── index.md
│       ├── fixed-memory.md
│       ├── work-index.md
│       ├── learning-index.md
│       ├── family-education-index.md
│       ├── daily-life-index.md
│       ├── health-index.md
│       ├── finance-index.md
│       ├── relationships-index.md
│       ├── project-current.md
│       ├── session-current.md
│       └── archive-index.md
└── scripts/
    ├── _common.py
    ├── init_memory_store.py
    ├── reindex_memory.py
    ├── normalize_candidates.py
    ├── merge_duplicates.py
    ├── score_memories.py
    └── run_maintenance.py
```

## 最重要的几个文件

### `SKILL.md`

Skill 入口。  
这里定义了：

- 什么时候应该触发这个 Skill
- 读取顺序
- 写回顺序
- 使用时的硬规则

### `references/memory-system.md`

解释这套记忆系统到底有哪些层和性质。

### `references/classification-draft.md`

解释当前采用的根领域草案，以及每个领域下建议的标准子槽位。

### `references/topic-catalog.md`

给出 7 个根领域下面的第一版典型主题清单，适合作为后续扩展的默认主题地图。

### `references/network-memory-model.md`

解释为什么不能只做树状分类，而要做“树 + 图关系”的混合结构。

### `references/slotting-guide.md`

这是最实用的文档之一。  
它专门回答：

- 一句话到底该放到哪里
- 应该进固定记忆、主题记忆、项目记忆还是会话记忆
- 什么时候只该进候选池

## 这个 Skill 怎么用

### 用途 1：在对话中决定该加载哪些记忆

例子：

```text
使用 $memory-orchestrator 判断这次问题应该加载哪些记忆，只读最相关的领域和主题。
```

### 用途 2：在对话结束后决定信息写回哪一层

例子：

```text
使用 $memory-orchestrator 判断这轮新结论应该写到固定记忆、主题记忆、项目记忆，还是只放候选池。
```

### 用途 3：设计或重构自己的记忆库

例子：

```text
使用 $memory-orchestrator 为我设计 memory-data 的目录结构和索引字段。
```

### 用途 4：判断一条记忆应该归到哪个领域

例子：

```text
使用 $memory-orchestrator 判断“晚上 11 点后不要高强度工作”应该归到哪个领域和哪种记忆性质。
```

## 默认读取逻辑

当前 Skill 采用的默认读取顺序是：

1. 固定记忆
2. 总索引
3. 领域索引
4. 最相关的主题记忆
5. 项目记忆
6. 会话记忆
7. 归档索引

注意：

- 不是每次都全部读取
- 先读摘要，后读正文
- 先看主归属，再决定是否沿关联主题继续展开

## 默认写回逻辑

当前 Skill 采用的默认写回顺序是：

1. 先写会话记忆
2. 再把潜在长期内容放入候选池
3. 只有稳定、可复用、已验证时，才提升到长期层

所以它强调的是：

- 每轮可以轻写
- 不要每轮都污染长期记忆

## 关于“分类”和“主题网络”

这个仓库的一个重要判断是：

**不要把记忆系统做成纯文件夹系统。**

因为一条记忆经常同时属于多个方向。

例如：

- 某个部署坑既属于编程，也属于项目，也属于运维经验
- 某个产品判断既属于工作，也可能和消费、业务有关

所以推荐做法是：

- 目录负责主归属
- 标签和关联字段负责横向连接

## 适合谁

这套 Skill 适合：

- 想做长期个人记忆系统的人
- 想把工作、学习、生活、家庭等记忆统一整理的人
- 想做“不是只靠大上下文硬塞”的记忆调用方式的人

## 当前限制

当前仓库还是框架优先：

- 更适合先讨论和设计记忆体系
- 不适合作为已经完全自动化的最终记忆产品

如果你想直接开始落地，下一步最自然的是：

1. 定义真实的 `memory-data/` 目录
2. 把 7 个根领域下面的第一批真实记忆填进去
3. 再决定哪些部分交给索引层和数据库

## 安装方式

如果你要把它作为本地 Skill 使用，放到你的 Codex skills 目录即可，例如：

```text
C:\Users\<你的用户名>\.codex\skills\memory-orchestrator
```

或者直接把当前仓库内容作为一个 Skill 根目录使用。

## 当前建议的下一步

如果继续演进，我建议顺序是：

1. 继续筛选和修订 7 个根领域下面的典型主题清单
2. 再填第一批真实记忆示例
3. 再补索引字段和 SQLite schema
4. 最后才做自动整理、升降级和召回评分
