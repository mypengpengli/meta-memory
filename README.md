# Meta Memory

`meta-memory` 是一个面向对话代理的记忆 Skill。  
它的目标不是把所有记忆一次性塞进上下文，而是让代理在对话时按需要加载记忆，在回合结束后再把新信息写回合适的层级。

## 原理

### 1. 按需加载，不全量加载

这套 Skill 默认遵循：

- 先判断当前问题属于什么领域和主题
- 先读索引和摘要，不先读正文
- 只展开少量真正相关的记忆
- 只有确实需要证据时，才继续下钻

默认读取顺序是：

1. 固定记忆
2. 总索引
3. 领域索引
4. 最相关的主题记忆
5. 项目记忆
6. 会话记忆
7. 归档索引

### 2. 不是纯文件夹，而是“树 + 主题网络”

这套 Skill 不把记忆理解成单一总文档，也不建议只做纯树状分类。

因为一条记忆经常会同时属于多个方向，例如：

- 某个部署坑，既属于编程，也属于项目，也属于运维经验
- 某个产品判断，既属于工作，也可能和消费、业务有关

所以推荐结构是：

- 目录负责主归属
- 标签负责横向关联
- `related_topics` 负责主题跳转
- `related_projects` 负责项目关联
- `related_sources` 负责来源追踪

也就是说：

- 文件组织是树
- 召回逻辑是树 + 主题网络混合

### 3. 记忆不是单层，而是分性质处理

当前 Skill 把记忆性质拆成：

- 固定记忆
- 领域记忆
- 主题记忆
- 项目记忆
- 会话记忆
- 候选记忆
- 归档索引

写回时遵循：

1. 先写会话记忆
2. 再把可能有长期价值的信息放进候选池
3. 只有稳定、可复用、已验证时，才提升到长期层

这套规则的目的很简单：

- 每轮可以轻写
- 不要每轮都污染长期记忆

### 4. 领域是入口，主题是细化

当前根领域草案包括：

- 工作
- 学习
- 家庭与子女教育
- 日常生活
- 健康
- 财务与消费
- 人际与关系

其中已经继续细化的有：

- 工作：编程 / 产品 / 业务 / 项目 / 工具
- 学习：方法 / 计划 / 输入 / 训练 / 复盘
- 家庭与子女教育：理念 / 画像 / 习惯 / 陪伴 / 阶段事项
- 日常生活：节奏 / 居住 / 饮食 / 事务 / 物品与设备

## 使用方法

### 1. 作为本地 Skill 安装

把仓库内容放到本地 skills 目录，例如：

```text
C:\Users\<你的用户名>\.codex\skills\memory-orchestrator
```

Skill 入口文件是：

```text
SKILL.md
```

### 2. 在对话中决定该加载哪些记忆

例子：

```text
使用 $memory-orchestrator 判断这次问题应该加载哪些记忆，只读最相关的领域和主题。
```

适合场景：

- 需要调用长期偏好
- 需要调用某个领域经验
- 需要承接上一段工作
- 需要判断该从哪个主题切入

### 3. 在对话后决定信息应该写回哪一层

例子：

```text
使用 $memory-orchestrator 判断这轮新结论应该写到固定记忆、主题记忆、项目记忆，还是只放候选池。
```

适合场景：

- 形成了稳定偏好
- 发现了可复用经验
- 当前项目状态发生变化
- 只是临时结论，应该先留在会话层

### 4. 用它来设计自己的记忆库

例子：

```text
使用 $memory-orchestrator 为我设计 memory-data 的目录结构、记忆分类和索引字段。
```

适合场景：

- 想搭个人长期记忆系统
- 想把工作、学习、生活、家庭等记忆统一整理
- 想设计“不是只靠大上下文硬塞”的记忆调用方式

### 5. 关键参考文件

如果要理解这套 Skill，优先看这些文件：

- `SKILL.md`
- `references/memory-system.md`
- `references/loading-rules.md`
- `references/writeback-rules.md`
- `references/classification-draft.md`
- `references/topic-catalog.md`
- `references/network-memory-model.md`
- `references/slotting-guide.md`

如果要看已经细化过的主题树，再看：

- `references/work-topic-tree.md`
- `references/learning-topic-tree.md`
- `references/family-education-topic-tree.md`
- `references/daily-life-topic-tree.md`
