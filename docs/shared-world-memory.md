# 家庭、机器人与空间共享记忆

Meta Memory 在普通项目 Claim 之外提供一条显式的“共享世界”路径。它用于让家庭
机器人、计划 Agent 和其他设备交换有用结果，同时避免把原始聊天、每个工具调用或
连续传感器流广播给所有 Agent。

## 先选择正确的数据类型

| 信息 | 存储类型 | 常见范围 | 生命周期 |
| --- | --- | --- | --- |
| 稳定用户偏好 | Claim（`remember`） | profile/user | 直到更正 |
| 项目决定 | Claim | workspace | 直到取代 |
| 机器人内部诊断 | Agent/device 私有记录 | 单设备 | 按运维需要 |
| 发现冰箱损坏 | shared activity；必要时再加 state | household | 事件历史/修复前 |
| 孩子最后出现位置 | temporal state | person + household | 几分钟，不是永久事实 |
| 房间说明、OCR、识别对象 | spatial observation | household/project | 当前或有时限 |
| 图片、视频、点云、占用栅格 | binary asset | 按绑定范围按需读取 | 资产保留期 |
| 地图拓扑、坐标系和版本 | map version | household/project | 不可变版本历史 |

普通 Claim、Turn 和共享 channel 是不同边界。Agent 只能完成自己的 Turn；共享
channel 只包含主动发布的活动、当前状态和空间语义，不会自动复制其他 Agent 的
完整对话或未发送草稿。

## Audience、channel 与家庭主体

推荐默认使用受限家庭 channel，只把确实需要参与的 Agent 加入：

```bash
meta-memory --json shared init --type household --key home \
  --label "Family home" --restricted \
  --member-agent home-robot --member-agent family-planner
```

保存输出中的 `audience.profile_id`、`audience.audience_id` 和
`channel.channel_id`。以后可显式增删 Agent 或 subject：

```bash
meta-memory shared grant --audience-id <audience-id> \
  --member-type agent --member-id another-agent
meta-memory shared grant --audience-id <audience-id> \
  --member-type subject --member-id person:child
meta-memory shared revoke --audience-id <audience-id> \
  --member-type agent --member-id retired-agent
meta-memory shared channels
```

若所有 Agent 不应看到相同信息，请建立不同 channel，例如：

- `household:home`：家电故障、家庭日程、公共房间观察；
- `person:owner-care`：只允许指定照护 Agent；
- `device:home-robot`：电池、温度、传感器异常等内部诊断；
- `project:renovation`：房屋改造资料与测量结果。

### 一个机器人服务多个家庭成员

服务器 agents 文件中的 `subject_ids` 是 Token 可代表或查询的白名单。远端安装的
`--subject-id` 是普通对话的默认主体。比如默认服务家长、但允许记录孩子状态：

```text
server subject_ids: ["person:owner", "person:child"]
remote default:      person:owner
one state command:   --subject-id person:child
```

远端 `before` 的普通项目记忆仍以本次主要 subject/workspace 为准；共享上下文可
包含该 Token 白名单内、且属于当前 audience/channel 的家庭主体记录。Agent 不得
从姓名自行创造 ID，也不得覆盖固定 workspace/audience/channel。另一个家庭或
服务角色需要不同 Token、Agent ID 或更窄的 channel。

## Activity：发生过的事件

Activity 是带时间与来源的精简事件。它适合“机器人发现冰箱不制冷”，不适合
“冰箱当前仍然坏着”这类会变化的当前值。

本地服务器命令：

```bash
meta-memory shared publish --channel-id <home-channel-id> \
  --kind household --summary "Refrigerator is not cooling" \
  --source-ref robot:diagnostic-42 --confidence 0.98 \
  --occurred-at <ISO-8601-observed-at>

meta-memory shared feed --channel-id <home-channel-id> --limit 20
```

远端 launcher：

```text
<launcher> activity --session-id <conversation-id> \
  --kind household --summary "Refrigerator is not cooling" \
  --source-ref robot:diagnostic-42 --confidence 0.98 \
  --occurred-at <ISO-8601-observed-at>
<launcher> shared feed --limit 20
```

## State：会变化的当前值

State 以 `channel + subject + state_key` 表示一个当前值。更新会取代旧值；迟到的
旧观察不会覆盖更晚观察。未来 `valid_from` 的状态会等到生效时间才成为当前值，
过期或 superseded 项目仅在历史查询中可见。

```bash
meta-memory shared state-set --channel-id <home-channel-id> \
  --subject-id person:child --state-key last_seen \
  --summary "Last seen at playground entrance" \
  --source-ref robot-camera:event-43 --confidence 0.92 \
  --observed-at <ISO-8601-observed-at> \
  --valid-until <ISO-8601-short-expiry>

meta-memory shared states --channel-id <home-channel-id> \
  --subject-id person:child --state-key last_seen
meta-memory shared states --channel-id <home-channel-id> \
  --subject-id person:child --state-key last_seen --include-history
```

远端写入使用 `<launcher> state ...`，读取使用：

```text
<launcher> shared states --subject-id person:child --state-key last_seen --limit 20
```

时间必须使用含时区的真实 ISO-8601，例如
`YYYY-MM-DDTHH:MM:SS+08:00` 或 `YYYY-MM-DDTHH:MM:SSZ`。人物位置只表示“在某时
观察到”，不表示现在一定仍在那里；应使用分钟级短有效期。Heartbeat 会整理
到期状态，也可以立即执行 `meta-memory shared expire`。

## Binary asset：原始图片、视频和地图字节

原始媒体不会进入 prompt 或普通 JSON event。它被流式写入
`<store>/assets/objects`，按 SHA-256 去重；SQLite 只保存元数据、作用域和链接。

服务器本地：

```bash
meta-memory asset add room.jpg --media-type image/jpeg \
  --metadata-file asset-metadata.json
meta-memory asset list --media-type image/jpeg
meta-memory asset show <asset-id>
meta-memory asset export <asset-id> --output room-copy.jpg
```

`asset-metadata.json` 必须是对象：

```json
{
  "capture_device": "home-robot-front-camera",
  "purpose": "room survey"
}
```

远端：

```text
<launcher> asset upload --file room.jpg --media-type image/jpeg \
  --metadata-file asset-metadata.json
<launcher> asset list --media-type image/jpeg --limit 20
<launcher> asset get --asset-id <asset-id>
<launcher> asset download --asset-id <asset-id> --output room-copy.jpg
```

远端上传按分块校验并保留本地 receipt。断线后保持源文件路径与内容不变，重复
同一条 `asset upload --file ...` 命令即可续传；不要先改名或改写文件。不同范围
上传相同字节可以复用底层对象，但各自的 channel/workspace/Agent 可见性与元数据
不会因此互相覆盖。

## Map：稳定 ID 下的不可变版本

`map_id` 代表一个逻辑地图；每次更新创建递增、不可变版本，并保留 predecessor。
`coordinate_frame` 必填。同一个 `map_id` 不能跨 channel 改绑。

服务器本地：

```bash
meta-memory map add --channel-id <home-channel-id> \
  --map-id home-floor-1 --coordinate-frame map \
  --asset-id <asset-id> --name "Home first floor" \
  --captured-at <ISO-8601-capture-time> --metadata-file map-metadata.json
meta-memory map list --channel-id <home-channel-id>
meta-memory map show home-floor-1
```

远端 `map put` 使用 JSON 对象：

```json
{
  "map_id": "home-floor-1",
  "coordinate_frame": "map",
  "name": "Home first floor",
  "asset_id": "<asset-id>",
  "captured_at": "<ISO-8601-capture-time>",
  "metadata": {
    "format": "occupancy-grid",
    "resolution_m": 0.05
  }
}
```

```text
<launcher> map put --payload-file map-manifest.json
<launcher> map list
<launcher> map get --map-id home-floor-1
```

如果地图没有对应二进制资产，可省略 `asset_id`；仍必须提供 `map_id`、
`coordinate_frame`，并保持 `metadata` 为对象。

## Spatial observation：可检索的空间语义

空间观察保存 caption、OCR、对象数组、位置、时间、来源、置信度、可见范围，以及
可选 map/asset 链接。它不把原图自动塞进其他 Agent 的上下文。

对象文件必须是数组：

```json
[
  {"label": "water", "confidence": 0.94, "region": "under-sink"}
]
```

服务器本地：

```bash
meta-memory spatial add --channel-id <home-channel-id> \
  --map-id home-floor-1 --asset-id <asset-id> \
  --location-id kitchen-sink --location-text "Kitchen, under sink" \
  --caption "Water visible under the sink" \
  --objects-file objects.json --confidence 0.94 \
  --observed-at <ISO-8601-observed-at> \
  --valid-until <ISO-8601-expiry-if-temporary>

meta-memory spatial search "water sink" --channel-id <home-channel-id>
meta-memory spatial list --channel-id <home-channel-id>
```

远端：

```text
<launcher> observe --session-id <conversation-id> \
  --content "Water visible under the kitchen sink" \
  --source-ref robot-camera:event-44 \
  --observed-at <ISO-8601-observed-at> \
  --map-id home-floor-1 --asset-id <asset-id> \
  --location-id kitchen-sink --location-text "Kitchen, under sink" \
  --objects-file objects.json --confidence 0.94

<launcher> spatial list --limit 20
<launcher> spatial search "water sink" --limit 10
<launcher> spatial get --observation-id <observation-id>
```

默认观察 visibility 是 channel；workspace 或 Agent visibility 可进一步缩小范围。
资产读取还会再次检查其独立作用域。远端 `before` 只返回有界语义；只有任务确实
需要原始字节时才执行 `asset download`。

## 系统不会替机器人完成什么

Meta Memory 不进行视觉理解、OCR、物体检测、视频分析、SLAM、地图融合、定位、
避障或路径规划。机器人、传感器流程或上游模型先完成这些计算，再把可共享结果与
证据链接写入系统。它保存“观察到什么、何时、由谁、置信度多少、原始资产在哪”，
不是实时感知控制器，也不会把过期的 `last_seen` 当作当前真相。
