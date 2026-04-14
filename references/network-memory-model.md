# 记忆网络模型

想长期记住一个人，不能只靠树状分类。

因为同一条记忆经常同时属于多个方向：

- 一次创业失败，既是事件，也是财务风险偏好变化的原因
- 某个人际冲突，既是关系记忆，也会影响当前状态
- 一个长期项目，既关联目标，也关联工作领域经验

所以这套系统必须是“主对象树 + 关系网络”的混合模型。

## 一、树结构负责什么

树结构负责：

- 人能看懂
- 文档好管理
- 大致知道这条记忆的主归属

也就是说，一条记忆仍然要有一个主目录归属，例如：

- `profile`
- `states`
- `events`
- `relationships`
- `goals`
- `domains`

## 二、网络关系负责什么

网络关系负责：

- 关联人物
- 关联事件
- 关联目标
- 关联来源
- 版本替代关系
- 召回时的横向联想

## 三、每条记忆建议都带这些字段

- `subject_id`
  - 主对象，例如某个人
- `subject_name`
  - 展示名称
- `memory_kind`
  - `profile` / `state` / `event` / `relationship` / `goal` / `domain` / `session` / `candidate` / `archive`
- `domain`
  - 相关领域，例如 `work`
- `topic`
  - 主主题
- `tags`
  - 标签
- `start_at`
  - 生效起点
- `end_at`
  - 失效时间，仍有效可留空
- `confidence`
  - 可信度
- `status`
  - `active` / `historical` / `pending` / `superseded`
- `source`
  - 来源
- `related_people`
  - 关联人物
- `related_events`
  - 关联事件
- `supersedes`
  - 覆盖的旧记忆
- `replaced_by`
  - 被谁替代

## 四、这样做的好处

表面上仍然可以按目录组织，
但索引层可以表达：

- 画像和状态的差异
- 事件和结果的因果关系
- 某个关系对象如何影响多个状态
- 一条记忆是否已经失效

## 五、第一版怎么落地最稳

第一版不要急着做复杂图数据库。

先这样就够：

- 目录负责主归属
- Markdown frontmatter 负责时间和关联字段
- SQLite 负责建立轻量索引

这样既可移植，也方便以后扩展。
