# Docker 云端部署与本机 Codex 接入

这份指南把 Meta Memory 部署成一台云服务器上的中央记忆服务，再让本机 Codex、
家庭机器人或其他能加载 Skill 的 Agent 通过 HTTPS 共用它。仓库已经提供镜像、
Compose、后台整理、自动备份、恢复和升级脚本；不需要自己拼装 Python 服务。

```text
本机 Codex / 远端 Agent
  -> HTTPS + Bearer Token
  -> Caddy（可选，也可使用现有反向代理）
  -> meta-memory：唯一 API 实例
  -> /data/store：SQLite、资产、地图和空间观察（/data 是持久卷根）
  -> worker：串行 Heartbeat、Deep Dream 和备份
  -> /backups：可搬走的校验 ZIP
```

## 部署前准备

服务器需要：

- Linux 云服务器，以及 Docker Engine 和 Docker Compose v2；
- 一个指向服务器的域名；公网使用时开放 80/443，API 的 8765 端口保持仅本机；
- 至少能持久保存 `runtime/data`、`runtime/config`、`runtime/backups` 的磁盘；
- Git，用于从源码构建；若以后使用已发布镜像，也可以把升级模式改成 `pull`。

下面所有服务器命令都在仓库根目录运行。Compose 服务名固定为
`meta-memory`（API）、`worker`（整理与备份）和可选的 `caddy`。不要把
`meta-memory` 或 `worker` 扩容到多个副本。

## 1. 首次启动

```bash
git clone https://github.com/mypengpengli/meta-memory.git
cd meta-memory
sh docker/bootstrap.sh
```

这个初始化脚本不会启动服务。它会复制 `.env.example`、生成并静默保存 256-bit
Token、创建三个 `runtime` 持久目录；在 Linux 上还会写入当前 `id -u/id -g`，使
容器写出的备份仍可由部署用户读取。已有 `.env` 和 Token 默认保留；只有显式执行
`sh docker/bootstrap.sh --rotate-token` 才会轮换 Token，之后必须重建 API 并同步更新
远端 Agent。

同时检查这些稳定身份。部署投入使用后不要随意改名：

```dotenv
META_MEMORY_BOOTSTRAP_AGENT_ID=local-codex
META_MEMORY_BOOTSTRAP_WORKSPACE_ID=personal-workspace
META_MEMORY_BOOTSTRAP_SUBJECT_ID=person:user
META_MEMORY_PROFILE_NAME=User
```

`profile_id` 由首次配置的名称生成；后续以 `shared init` 返回的真实值为准，不要自行
另造一个 profile ID。

先让 Compose 展开配置，确认没有缺少变量，然后构建并启动一个 API 和一个 worker：

```bash
docker compose config --quiet
docker compose up -d --build meta-memory worker
docker compose ps
```

首次启动会自动完成以下工作：

1. 在 `/config/config.toml` 创建服务器配置；
2. 在 `/data/store` 初始化数据库并应用全部迁移；
3. 在 `/config/agents.json` 建立首个 Agent 绑定；
4. API 以非 root 用户监听容器内的 `8765`；
5. worker 每 10 分钟执行增量 Heartbeat、每天执行一次 Deep Dream，并自动备份。

宿主机对应目录默认是：

| 宿主目录 | 容器目录 | 内容 |
| --- | --- | --- |
| `runtime/data` | `/data` | `store/` 中的 SQLite/资产，以及 `.container-runtime/` worker 状态 |
| `runtime/config` | `/config` | `config.toml` 和不含 Token 值的 `agents.json` |
| `runtime/backups` | `/backups` | 带校验清单的完整 ZIP 备份 |

容器可以删除和重建，这三个宿主目录不能随意删除。不要让另一个 Compose 项目、
另一台机器或网络共享盘同时直接打开同一份 `runtime/data`。
`/data/store` 这个子目录是恢复契约的一部分：恢复会在同一个 `/data` 卷内建立临时
目录、校验完成后原子替换 `store`。不要把配置中的 store 改回不可替换的挂载点根
`/data`。

服务器上所有 `meta-memory` CLI 管理命令统一通过 `sh docker/admin.sh ...` 运行。这个
wrapper 会创建一个短生命周期容器并经过正式 entrypoint 降权，因而与 API/worker
使用同一 UID/GID。
不要把文档命令改成裸 `docker compose exec ... meta-memory`：Docker exec 默认使用
镜像 root 用户，可能在 bind mount 中留下后台进程无法读取的 SQLite sidecar、Claim
或备份文件。立即备份、恢复和升级则分别使用本指南给出的 worker、`restore.sh` 和
`upgrade.sh` 命令。

## 2. 判断服务是否真的可用

API 提供两个不需要 Token 的探针：

```bash
curl --fail http://127.0.0.1:8765/healthz
curl --fail http://127.0.0.1:8765/readyz
sh docker/admin.sh --json doctor
```

- `/healthz` 返回 `200 {"status":"ok"}`，只表示进程还活着；
- `/readyz` 只有在 Agent 绑定已加载、数据库可访问且迁移为最新、store 和资产目录
  可写时才返回 `200`；否则返回 `503` 和失败的检查项；
- Docker 的 `healthy` 状态使用 `/readyz`，因此“容器在运行”不等于“可接收记忆”。

每个 HTTP 响应都有 `X-Request-ID`。客户端传入合法的 `X-Request-ID` 时会原样
回显，便于把客户端错误和服务器日志对应起来；否则服务器生成新 ID。API 默认向
标准错误输出一行一个对象的 JSON 访问日志，只记录时间、请求 ID、方法、无查询参数
的路径、状态码和耗时，不记录 Token、请求正文或查询内容：

```bash
docker compose logs --tail 100 meta-memory
docker compose logs --tail 100 worker
```

通常保持访问日志开启。确有需要时可设置 `META_MEMORY_HTTP_ACCESS_LOG=0`；裸 CLI
默认排空请求 10 秒，Compose 显式设为 20 秒并在 30 秒后才允许 Docker 强停，可用
`META_MEMORY_HTTP_SHUTDOWN_TIMEOUT=0..300` 调整，但必须小于 `stop_grace_period`。

## 3. 发布 HTTPS

远端客户端只接受 HTTPS；只有 `localhost` 开发连接允许 HTTP。不要把裸 `8765`
直接暴露到公网。

### 使用仓库自带 Caddy

把域名 A/AAAA 记录指向服务器，在 `.env` 中设置：

```dotenv
MEMORY_DOMAIN=memory.example.com
META_MEMORY_BIND_ADDRESS=127.0.0.1
```

开放 80/443 后启动 HTTPS profile：

```bash
docker compose --profile https up -d --build
docker compose --profile https ps
curl --fail https://memory.example.com/readyz
```

Caddy 会自动申请和续期证书，并把 HTTPS 转发到内部的 `meta-memory:8765`。示例已把
请求体上限设为 `70MB`、读写超时设为 10 分钟。若把
`META_MEMORY_MAX_ASSET_MB` 调大，也要把 `MAX_REQUEST_BODY` 调到更大的值。

### 使用已有 Nginx、Caddy 或云负载均衡

保持 `META_MEMORY_BIND_ADDRESS=127.0.0.1`，将已有网关反向代理到
`http://127.0.0.1:8765`。网关必须：

- 对公网提供有效 HTTPS 证书；
- 允许的请求体大于 `META_MEMORY_MAX_ASSET_MB`；
- 上传读写超时覆盖大文件和较慢网络；
- 不记录 `Authorization` 请求头；
- 不把 `/healthz` 成功误当成 `/readyz` 成功。

## 4. 创建家庭共享频道或新增 Agent

首次自动生成的 Agent 已能使用私有 Turn 和 workspace 记忆，但尚无共享频道。
需要让 Codex、机器人和家庭 Agent 看到同一批家庭事件、状态、图片或地图时，先在
服务器创建受限 audience/channel：

```bash
sh docker/admin.sh --json shared init \
  --type household --key family-home --label "Family home" \
  --restricted --member-agent local-codex --member-agent home-robot
```

保存 JSON 输出中的真实 `audience.profile_id`、`audience.audience_id` 和
`channel.channel_id`。不要把示例占位符当成真实 ID。

然后更新第一个绑定；`--audience-id` 可重复，既要列 audience，也要列 channel：

```bash
sh docker/admin.sh --json init-agents-file \
  --output /config/agents.json --replace-agent \
  --agent-id local-codex --profile-id '<真实-profile-id>' \
  --workspace-id personal-workspace --subject-id person:user \
  --audience-id '<真实-audience-id>' --audience-id '<真实-channel-id>' \
  --token-env META_MEMORY_TOKEN
docker compose restart meta-memory
```

服务器只在启动时加载 `agents.json`。只修改绑定文件时执行
`docker compose restart meta-memory`；修改 `.env` 中的 Token 后，普通 restart 不会
更新已有容器的环境，必须执行 `docker compose up -d --force-recreate meta-memory`。

新增 Agent 时应使用新的 `agent_id`、稳定 workspace 和独立 Token。例如在 `.env`
增加 `META_MEMORY_TOKEN_HOME_ROBOT=<另一个高熵值>`，并把同名 `token_env` 写入新绑定。
Compose 会把 `.env` 中的 Token 变量提供给 API，但 `agents.json` 只保存变量名。
完整示例是：

```bash
ROBOT_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
if grep -q '^META_MEMORY_TOKEN_HOME_ROBOT=' .env; then
  sed -i "s|^META_MEMORY_TOKEN_HOME_ROBOT=.*$|META_MEMORY_TOKEN_HOME_ROBOT=$ROBOT_TOKEN|" .env
else
  printf '\nMETA_MEMORY_TOKEN_HOME_ROBOT=%s\n' "$ROBOT_TOKEN" >> .env
fi
unset ROBOT_TOKEN

sh docker/admin.sh --json init-agents-file \
  --output /config/agents.json \
  --agent-id home-robot --profile-id '<真实-profile-id>' \
  --workspace-id home-robot-workspace \
  --subject-id person:user --subject-id person:child \
  --audience-id '<真实-audience-id>' --audience-id '<真实-channel-id>' \
  --token-env META_MEMORY_TOKEN_HOME_ROBOT

docker compose up -d --force-recreate meta-memory
curl --fail http://127.0.0.1:8765/readyz
```

这段命令重复执行时会替换现有变量，不会向 `.env` 追加同名行；替换 Token 后必须把
新值同步到对应 Agent，并强制重建 API 容器。

再把 `.env` 中这个新 Token 的值设置到机器人主机的同名环境变量。通过 `/readyz`
和日志确认所有绑定都已加载；不要让两个 Agent 共用同一个 Token。

如果机器人可为多名家庭成员服务，可以重复 `--subject-id`；只有列在服务端绑定中的
subject 才能由该 Agent 写入。内部传感器诊断不要发布到家庭频道；其他 Agent 确实
需要的事件使用 activity，会变化的位置/设备状态使用带过期时间的 state，图片和
地图则使用 asset/map/spatial。

## 5. 在本机安装 Codex 远端 Skill

本机需要 Python 3.10+ 和同版本 Meta Memory，但不需要本地运行服务器、worker、
Heartbeat 或 Dream：

```powershell
python -m venv "$HOME\.venvs\meta-memory"
& "$HOME\.venvs\meta-memory\Scripts\Activate.ps1"
python -m pip install --upgrade pip
python -m pip install "git+https://github.com/mypengpengli/meta-memory.git"

meta-memory install-remote-agent `
  --agent-id local-codex `
  --skill-dir "$HOME\.codex\skills" `
  --server-url https://memory.example.com `
  --workspace-id personal-workspace `
  --subject-id person:user `
  --audience-id '<真实-audience-id>' `
  --channel-id '<真实-channel-id>' `
  --token-env META_MEMORY_TOKEN

setx META_MEMORY_TOKEN "<与服务器 .env 完全相同的值>"
```

`setx` 只影响之后启动的进程。关闭并重新打开终端，再重启 Codex，使它重新加载
环境变量和 `$HOME\.codex\skills\meta-memory-remote\SKILL.md`。使用安装输出中的
精确 launcher 路径执行：

```text
<launcher> recovery
<launcher> status
```

随后在 Codex 中完成一个真实对话回合，再检查：

- `lifecycle_state` 为 `active`；
- `last_before` 和 `last_after` 晚于安装时间；
- `local_outbox_pending` 和 `local_outbox_corrupt` 都为 `0`。

`status` 成功只证明连接和身份正常，不能代替一次真实 `before -> after`。网络中断时
远端 Skill 会保留请求、完整回答和原 `turn_id`；恢复联网后执行 `recovery`，不要为
同一回答另建 Turn，也不要改写已经缓冲的原文。

## 6. 后台整理与自动备份

`worker` 是单一串行调度器，避免同一任务重叠：

- `META_MEMORY_HEARTBEAT_INTERVAL_SECONDS=600`：增量整理间隔；
- `META_MEMORY_DREAM_TIME=23:30`：按容器 `TZ` 每天成功执行一次 Deep Dream；
- `META_MEMORY_BACKUP_INTERVAL_SECONDS=86400`：完整备份间隔；
- `META_MEMORY_BACKUP_RETENTION_DAYS=30`：按时间清理；
- `META_MEMORY_BACKUP_RETENTION_COUNT=14`：即使仍在期限内也只保留最新数量。

worker 的上次成功状态保存在 `/data/.container-runtime/worker`，重启不会造成当天 Deep Dream
重复成功执行。Compose 给 API 30 秒停机宽限（应用先排空 20 秒），给可能正在执行
Dream/大备份的 worker 5 分钟；升级或恢复时应等待它安全停止。修改 `.env` 后运行：

```bash
docker compose up -d worker
docker compose logs --tail 100 worker
```

手工立即执行一次备份：

```bash
docker compose run --rm --no-deps worker meta-memory-backup
ls -lh runtime/backups/
```

每次会生成同名的一组三个文件：`.zip` 包含一致的 SQLite 快照、活动资产、
`config.toml` 和内部校验清单；`.agents.json` 保存当时的非秘密 Agent 绑定；
`.manifest.json` 绑定前两者的文件名和 SHA-256。三者会成组保留和清理。Token 值与
`.env` 永远不会进入备份，因此仍需把 Token 恢复资料另行保存。应把整组三个文件
定期复制到另一块磁盘或对象存储；只留在同一块云盘上不能抵抗磁盘损坏。

## 7. 恢复

先列出 `runtime/backups` 中的文件，只把文件名传给脚本：

```bash
ls -1 runtime/backups/meta-memory-*.zip
sh docker/restore.sh meta-memory-YYYYMMDDTHHMMSSZ.zip
```

恢复脚本会：

1. 停止 worker，校验 ZIP、Agent sidecar 与外层 SHA-256，并确认 `.env` 已设置它引用
   的每个 Token 变量；旧版 ZIP 没有 sidecar 时会明确警告并保留当前绑定；
2. 为当前线上数据再做一次不触发保留清理的保护性备份；
3. 停止 API，原子恢复 `/data/store`，并恢复匹配的 `agents.json`；
4. 清除旧的 Dream/backup 成功标记，让恢复数据立即重新整理和备份；
5. 重启服务，等待 `/readyz`，最后运行 `doctor`。

恢复成功后仍要检查：

```bash
docker compose ps
curl --fail http://127.0.0.1:8765/readyz
sh docker/admin.sh --json overview \
  --server --agents-file /config/agents.json
```

若备份来自另一台服务器，脚本会从同名 sidecar 恢复匹配的 `agents.json`；运行前
仍须在新服务器 `.env` 中设置相同变量名对应的 Token 值，否则恢复会在改动数据前
失败并列出缺少的变量。

## 8. 升级与回滚

从源码部署时，推荐流程是：

```bash
git pull --ff-only
sh docker/upgrade.sh
```

脚本先让正在运行的旧版本创建强制升级前备份，再构建或拉取新镜像；只有这些步骤
成功后才停止唯一 worker 并重建 API/worker，随后等待 `/readyz` 并运行 `doctor`。
备份或构建失败都不会替换正在运行的容器。如果 Caddy 原本正在运行，脚本还会拉取
固定版本并强制重建它，使新的 Caddyfile 真正加载；未启用 HTTPS 的部署不会被启动。
源码构建默认刷新基础镜像；若镜像仓库暂时不可达但固定基础镜像已经在本机缓存，可
仅为本次运行使用 `META_MEMORY_UPGRADE_PULL_BASE=false sh docker/upgrade.sh`。这不会
跳过升级前备份、应用镜像重建、`/readyz` 或 `doctor`。

如果改用镜像仓库，在 `.env` 固定明确版本，不要使用不可追踪的浮动标签：

```dotenv
META_MEMORY_IMAGE=ghcr.io/example/meta-memory:2.8.1
```

然后运行：

```bash
META_MEMORY_UPGRADE_MODE=pull sh docker/upgrade.sh
```

若升级后功能异常：

1. 记下升级脚本生成的升级前 ZIP 文件名；
2. 把 `META_MEMORY_IMAGE` 或 Git 工作树切回上一已知可用版本；
3. 不要让旧镜像直接继续写已迁移的新数据库；
4. 使用 `sh docker/restore.sh <升级前备份.zip>` 恢复；
5. 检查 `/readyz`、`doctor`，再让远端 Agent 执行 `recovery`。

数据库迁移可能是单向的，因此“只换回旧镜像但保留新数据库”不算完整回滚。

## 9. 上线验收清单

正式让 Codex 或机器人依赖云端记忆前，逐项完成：

- [ ] `docker compose config --quiet` 成功；
- [ ] `meta-memory` 和 `worker` 各只有一个副本；
- [ ] `/healthz` 为 200，`/readyz` 为 200 且四项 checks 都是 `ok`；
- [ ] 公网域名使用有效 HTTPS，公网无法直接访问裸 8765；
- [ ] 本机 launcher `status` 身份、workspace、subject、audience/channel 正确；
- [ ] 一个真实回合留下新的 `last_before`、`last_after`，outbox 为 0；
- [ ] 停止 API 时完成一个测试 Turn，重启后 `recovery` 能清空原 outbox；
- [ ] 上传测试图片，创建 map/spatial 记录，重启容器后仍可检索和下载同一字节；
- [ ] 手工备份成功，并已把同名 ZIP/agents/manifest 三件套和 Token 恢复资料复制到异机；
- [ ] 通过一次测试恢复，确认 `/readyz` 与 `doctor` 重新成功。

仓库的 `Docker hosted-service E2E` CI 会在真实镜像中自动覆盖初始化、探针、Turn、
断网 outbox、重启持久化、恢复、资产、地图和空间检索；它不能替代你对域名、云防火墙
和真实本机 Codex 进程的最后验收。

## 常见问题

### 容器是 running，但 `/readyz` 返回 503

查看返回的具体 check，再查看 `docker compose logs meta-memory`。常见原因是 `.env`
中没有 `agents.json` 所引用的 Token 环境变量、迁移未完成，或 bind mount 不可写。
不要把 healthz 的成功当作修复。

### 修改 `agents.json` 后仍然 403

确认 Agent 的 workspace、subject、audience/channel 均在绑定白名单内，并执行
`docker compose restart meta-memory`。如果还新增或修改了 `.env` Token，则改用
`docker compose up -d --force-recreate meta-memory`。API 不会在请求中热加载绑定文件，
Docker restart 也不会重新注入环境变量。

### 图片上传被 413 或超时

同时检查 `META_MEMORY_MAX_ASSET_MB`、`MAX_REQUEST_BODY` 和外部反向代理的请求体、
读取、写入超时。客户端会分块并保留上传凭据，但代理仍必须允许每个请求通过。

### 宿主目录权限错误

默认入口会创建目录、修复 bind mount 所有权，然后以非 root UID/GID 运行。若使用
NFS、只读挂载或禁止 `chown` 的文件系统，请先在宿主机创建目录并把 `.env` 中的
`META_MEMORY_UID`、`META_MEMORY_GID` 改为实际拥有者，再重建容器。

### 可以运行两个 API 做高可用吗

不可以对同一个 SQLite 数据目录这样做。当前适合个人、家庭或小团队的一台权威
服务器；需要高可用数据库、多节点写入或对象存储时，应先迁移存储架构，而不是直接
把 Compose `replicas` 改成 2。
