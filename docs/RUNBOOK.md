# 运维手册

## 环境一览

| 环境 | 地址 | 服务器上的目录 | 访问方式 | 凭据在哪 |
|---|---|---|---|---|
| 生产 | `https://48team.xiaozhudf2026.foo`（Nginx → `127.0.0.1:8018` → 容器 8008） | 代码 `/opt/team48`，数据 `/data/team48` bind 到 `/opt/team48/data` | `ssh root@156.238.254.8`（速维云-美国-8H8G-三网） | SSH 密码：本机 `VPS.local.md`（已 gitignore）或 `C:/Projects/VPS/速维云-美国-8H8G-三网/机子登录.txt`；应用配置：VPS `/opt/team48/.env` |
| 本地 | `http://127.0.0.1:8008` | 仓库根，数据 `data/` | 见 [ARCHITECTURE.md](ARCHITECTURE.md#本地运行) | 本地 `.env` |

- Compose 项目 `team48`、容器 `team48-manager`、网络 `team48_net`；外挂现有网络 `sub2api_sub2api-network`，容器内访问 Sub2API 用 `http://sub2api-canary:8080`（宿主机 `http://127.0.0.1:8101`）。
- HME 服务在同机容器 `icloud-hme`（`http://icloud-hme:8081`），部署见 `../icloud-hme/docs/RUNBOOK.md`。
- Nginx 站点配置在仓库 `deploy/nginx-48team.conf`，独立 server_name，不写进 Sub2API 的站点文件。
- 生产 `.env` 必须覆盖 `SECRET_KEY`、`ADMIN_PASSWORD`（代码默认值是弱占位），并设 `SESSION_COOKIE_SECURE=True`；模板 `deploy.env.example`。

### 目录与端口隔离（硬约束）

| 对象 | 能不能动 |
|---|---|
| `/opt/sub2api`（含 `source-main`、Compose、`.env`、data、postgres、redis） | 禁止 |
| 容器 `sub2api`（8100 备）、`sub2api-canary`（8101 主）、`sub2api-postgres`、`sub2api-redis` | 禁止重启 / 重建 / 改 Nginx |
| 端口 8100、8101、8080、8200、8317 | 不占用 |
| `/opt/team48`、容器 `team48-manager` | 只在这里构建和启停 |

禁止 `docker compose down`、`--remove-orphans`，禁止在 `/opt/sub2api` 里执行任何 compose。Sub2API 只通过 HTTP Admin API 使用。

### 代码拉取

仓库私有。VPS 用只读 Deploy Key（`/root/.ssh/team48_deploy`，SSH 别名 `github.com-team48`，remote `git@github.com-team48:hixz12d/48team-manager.git`）只 `git pull`，不在生产目录提交。推送只在本机（`gh` 已登录 `hixz12d`）。不要把本机 `gh` 登录态或带 repo 权限的 token 拷到 VPS。`.env`、`data/` 不进仓库。

## 部署

未经明确批准不部署，不在生产做真实踢人、邀请或改席位的测试。

1. 本机推送 `origin/main`。 —— 确认：`git status` 干净，`git log origin/main -1` 是目标提交。
2. 登录 VPS，确认没有进行中的任务（任务页无 running / queued，尤其是凭据写入、团队同步、轮转）。宿主机没有 `sqlite3` 命令，要查库用 `python3` 只读打开 `file:data/team48.db?mode=ro`，任务表是 `operations`（列 `type`、`state`）。
3. 备份（`<tag>` 用提交短 SHA）：
   ```bash
   cd /opt/team48
   mkdir -p -m 700 deploy/releases/<tag>
   python3 -c "import sqlite3; s=sqlite3.connect('data/team48.db'); d=sqlite3.connect('deploy/releases/<tag>/team48.db'); s.backup(d); print(d.execute('pragma integrity_check').fetchone()[0])"
   cp .env deploy/releases/<tag>/.env && chmod 600 deploy/releases/<tag>/*
   docker tag team48-manager:local team48-manager:rollback-<tag>
   ```
   —— 确认：`integrity_check` 输出 `ok`，`docker images | grep rollback-<tag>` 存在。
4. 拉取并只重建自己：
   ```bash
   git pull --ff-only origin main
   docker compose -p team48 -f deploy/docker-compose.yml up -d --build --no-deps team48
   ```
   新表 / 新列由启动时 bootstrap 自动补，无单独迁移命令。
5. 健康检查（见下）。 —— 确认：全部通过。

涉及 Sub2API 合约的功能：两个 Sub2API 副本必须返回同一 `instance_id` 和期望 revision；认证恢复要求其迁移 249，交回刷新权要求迁移 250 且所有消费 RT 的副本已升级。Sub2API 走它自己的发布流程，本项目不动它。

## 回滚

1. 界面上先关闭自动轮转、自动重授权、自动额度检测，等排队中的同步 / 额度任务跑完或取消。
2. 切回旧镜像：
   ```bash
   cd /opt/team48
   docker tag team48-manager:rollback-<tag> team48-manager:local
   git checkout <上一版提交>
   docker compose -p team48 -f deploy/docker-compose.yml up -d --no-deps team48
   ```
3. 默认保留新增的表、列和审计记录（旧代码会忽略它们），不删库。只有数据确实被写坏时，才停容器后用 `deploy/releases/<tag>/team48.db` 覆盖 `data/team48.db`。
4. 注意：已发生过刷新权交回的，不能直接回滚到不认识刷新归属的旧版本；回滚也撤销不了已经发生的官方授权、RT 消费或成员变动。

## 健康检查

- `docker compose -p team48 -f deploy/docker-compose.yml ps`：`team48-manager` 为 `running (healthy)`，只绑 `127.0.0.1:8018`。
- `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8018/health`（以及 `/login`、`/static/js/app.js`）：期望 200。
- 公网 `https://48team.xiaozhudf2026.foo/login`：期望 200。
- 登录后 `GET /api/runtime/status`：心跳新鲜；`GET /api/quota/runtime`：开关、排队数正常。
- `docker logs team48-manager --since 3m`：无报错。

## 日志与排障

- 日志：`docker compose -p team48 -f deploy/docker-compose.yml logs --tail 100`；Nginx `/var/log/nginx/48team.{access,error}.log`。
- HME 连通干跑（不开号）：`docker exec team48-manager python -c "import socket; print(socket.gethostbyname('icloud-hme'))"`、`docker logs team48-manager --since 30m | grep -i hme`。

| 现象 | 原因 | 处理 |
|---|---|---|
| 轮转任务停在 `partial` / `manual_required` | 手机 / 人机验证、写入结果未知、旧号清理失败 | 人工处理后在任务详情点"继续轮转"；不要重新发起，不要靠归档绕过 |
| 邀请 `write_outcome_unknown` | 官方写请求超时或 5xx | 先同步成员 / 邀请核对结果，不要直接重发 |
| 邀请 422 `not a valid SeatType` | 席位接口值错 | 设置里 `invite_seat_wire_*` 应为 `default` / `prolite` |
| `sync_oauth_unsupported` | Sub2API revision 不在 3–5 或副本版本不一致 | 等两个副本一致，不绕过能力检查 |
| `instance_mismatch` | Sub2API 数据库实例变了 | 核实连接和绑定后再试 |
| `auth_recovery_unsupported` | Sub2API 无 revision 4/5 | 升级 Sub2API 并完成迁移 249 |
| Sub2API 同步 partial / pending | 缓存或调度传播未完成 | 点"重试未完成步骤"；快照超过 120 秒先"重新核对状态" |
| 交回刷新权卡住 / 部分完成 | 远端有在途刷新 / 清理 ack 失败 | "继续交回 Team" / "确认交接收尾"；不要删票据或归属行 |
| `/api/ext` 返回 404 / 401 | 令牌未配置或不足 24 位 / 两端令牌不一致 | 改 `/opt/team48/.env` 的 `EXTENSION_API_TOKEN` 后重建，本机重新打包插件 |
| `phone_verification_required` / `registration_home_not_ready` | 注册或 OAuth 需要手机 / 主页未就绪 | 保留同一账号续接，不要重新注册或领新别名 |
| Codex 导出被拒 | 某账号缺 AT、将过期或待授权 | 先刷新或授权该账号 |

## 常用操作

- **首次启用自动轮转**：先保存 `关闭 + 仅选中团队 + 空列表`，核对后再选范围开启。
- **切换注册流程**：`/opt/team48/.env` 改 `BROWSER_SIGNUP_FLOW=extension`（回退改 `legacy`）后重建。手动轮转不受影响，固定用 extension。
- **启用 Chromix**：下载并按 SHA256 校验归档，解压到 `/app/data/browsers/chromix/`，设 `BROWSER_ENGINE=chromix` 和 `BROWSER_EXECUTABLE` 后重建。首次会新建档案，可能需重新登录；回退改回 `chromium` 即用旧档案。冒烟：`python -m scripts.browser_environment_smoke --engine chromix --browser-executable <path> --headed`。
- **启用插件接口**：`python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成令牌 → 写 `/opt/team48/.env` 的 `EXTENSION_API_TOKEN` → 重建 → 本机重新打包插件。
- **打包插件（本机）**：`python scripts/build_signup_extension.py --local-config --unpack`（只用本机 `private-config.mjs`）；首次不带 `--local-config` 会经 SSH 只读 VPS 上的 Cloudflare 配置。`--team48-token <令牌>` / `--no-team48` 写入 / 移除令牌。产物在 `dist/`（gitignore）。安装说明见 `extensions/chatgpt-signup/README.md`。
- **单席位补位（本机）**：`python -m scripts.signup_one --workspace-id N --role member` 只显示预检，加 `--confirm` 才执行；短信用 `--sms-stdin` 从标准输入传，不写在命令参数里。
- **HubStudio 本机观察**：`scripts/hubstudio_probe.py --port <CDP端口>` 只读探测；`scripts/hubstudio_record.py record|mark|stop --port <p> --out dist/hubstudio-record-<时间>` 录时间线，用 `stop` 结束，不要强杀。端口每次重新识别。
