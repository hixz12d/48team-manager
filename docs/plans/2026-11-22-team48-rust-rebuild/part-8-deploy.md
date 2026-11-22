# Part 8：集成、打包与切换准备

**状态**：已完成
**依赖**：part-0 ~ part-7 全部已完成
**所属计划**：[README](./README.md)

## 目标

把两个服务、前端、插件拼起来：生产镜像（一个镜像含两个二进制和嵌入的前端）、Compose、Nginx 配置、`.env.example` 定稿；本机用 Docker 把看板 + 授权桥跑起来联调一遍；写好切换步骤草稿供 `/flow-ship` 使用。本 Part **不碰生产服务器**。

## 可改文件

- `team48/deploy/Dockerfile`（新建，生产镜像）
- `team48/deploy/compose.yaml`（新建）
- `team48/deploy/nginx-48team.conf`（新建，切换后的 Nginx 配置）
- `team48/deploy/cutover.md`（新建，切换步骤草稿；上线后其内容并入 RUNBOOK 再删）
- `team48/.env.example`（定稿：补上各 Part 新增的变量，如 `MOTHER_REFRESH_ENABLED`）
- `team48/scripts/check.sh`（只在需要把生产镜像构建加进检查时改）
- 为联调发现的接线问题做的小修：限于两个服务之间、前后端之间的对接错误，改动写进完成记录；功能缺陷不在本 Part 修，记下来交给用户决定

## 只读参考

- 本计划 [README](./README.md) 的"按常规处理"和"上线注意"
- 各 Part 的完成记录
- 老仓库 `deploy/nginx-48team.conf`、`docker-compose.yml`（域名、证书路径、网络写法）
- 服务器资料库 `C:\Projects\VPS\天理云-美西国际大宽带-8H16G\docs\RUNBOOK.md`（同机服务、网络 `codex-proxy-rs_default`、`team48_net`、防火墙和 Nginx 规则）
- 服务器资料库 `C:\Projects\VPS\天理云-美西国际大宽带-8H16G\AGENTS.md`（硬性规则）

## 要点

- **镜像**：多阶段：Node 构建 `apps/dashboard/web` → Rust 构建两个二进制（release，目标 CPU 不超过 x86-64-v3）→ 运行层用精简 Debian（带 CA 证书），非 root 用户运行；同一个镜像，用不同命令启动两个服务。
- **Compose**：项目名 `team48v2`；服务 `team48-dashboard`（`127.0.0.1:8028`，挂 `./data`，命令 `dashboard serve`）、`team48-bridge`（`127.0.0.1:8029`，无数据卷）；都加入外部网络 `codex-proxy-rs_default`（rs 地址 `http://codex-proxy-rs:8080`）；`restart: unless-stopped`、健康检查 `/health`；不用 `--remove-orphans`。只读取 `.env`。
- **Nginx**：`48team.xiaozhudf2026.foo`：`location /api/ext/` → `127.0.0.1:8029`，`location /` → `127.0.0.1:8028`；超时 60 秒（官方写操作走代理可能慢）；证书路径沿用老配置。
- **本机联调**：用 Docker 起两个服务（rs 地址可指向一个假服务或留空，看板要能在 rs 不可用时正常显示"rs 读取失败"）；验证：登录、看板空状态、母号授权 start 能生成链接、授权桥 ping 鉴权（短令牌 404、错令牌 401、对令牌 200）。不在本机连生产 rs、不调用真实官方写接口。临时文件放系统临时目录，结束删除；联调容器结束后停掉删除。
- **切换步骤草稿 `deploy/cutover.md`**，按顺序写清命令和检查点：
  1. 服务器建 `/opt/team48-v2`，上传代码，写 `.env`（新 `ENCRYPTION_KEY`、`LEGACY_ENCRYPTION_KEY` 取自老 `.env`，`MOTHER_REFRESH_ENABLED=false`），构建镜像。
  2. 起新服务（不切 Nginx），通过 SSH 隧道访问，干跑 `import-legacy` 核对团队和母号。
  3. 切换窗口：确认老项目无进行中任务 → 停老容器 `team48-manager`（`docker stop`，不 down、不删卷）→ 只读备份 `team48.db` → `import-legacy --commit` → `MOTHER_REFRESH_ENABLED=true` 重启看板 → 抽查母号授权状态、一个团队同步。
  4. 换 Nginx 配置，`nginx -t` 后 reload；公网登录页 200；插件 ping 200；插件实际接入一个号到 rs。
  5. 回滚：Nginx 换回老配置、`docker start team48-manager`；新服务停掉。注意回滚前新服务若已刷新过母号令牌，老库里的刷新令牌已作废，需在老项目重新授权母号——写明这一风险。
  6. 收尾提醒：rs 后台"邮箱自动授权"入口单独停用；服务器资料库文档更新。

## 步骤

1. 写 Dockerfile、compose、Nginx 配置，定稿 `.env.example`。
2. 本机构建镜像并联调，修接线问题。
3. 写 `deploy/cutover.md`。
4. 跑检查，清理本机联调容器和临时文件。

## 完成标准

- [x] 本机 `docker build` 成功，两个服务能起来，`/health` 200
- [x] 联调检查点全部通过（写进完成记录）
- [x] `deploy/cutover.md` 每一步有命令和预期结果
- [x] 基本检查通过：`bash scripts/check.sh`

## 完成记录

- Claude 自己做。新建 `deploy/Dockerfile`（Node 构建前端 → Rust release 构建两个二进制，`target-cpu=x86-64-v2`，BuildKit 缓存 → `debian:bookworm-slim` 运行层，带 CA 证书 / curl / tzdata，uid 10001 非 root；默认命令 `dashboard serve`）、`deploy/Dockerfile.dockerignore`（计划外新增：只作用于这个 Dockerfile 的构建上下文忽略表，排除 target、node_modules、dist、extension、data、.env、数据库、.git）、`deploy/compose.yaml`（项目 `team48v2`，`team48-dashboard` 127.0.0.1:8028 挂 `../data:/data`，`team48-bridge` 127.0.0.1:8029 无数据卷，同一镜像 `team48:local`，外部网络 `codex-proxy-rs_default`，`restart: unless-stopped`，curl `/health` 健康检查，只读 `../.env`）、`deploy/nginx-48team.conf`（`/api/ext/` → 8029 读超时 100 秒，其余 → 8028，超时 60 秒，证书路径沿用老配置）、`deploy/cutover.md`。`.env.example` 补上 `MOTHER_REFRESH_ENABLED`（样例值 false）、`COOKIE_SECURE`。`scripts/check.sh` 未改。
- 接线小修：授权桥补 `GET /health`（验收 part-6 时已加，见 part-6 记录）。
- 本机联调（假配置、rs 地址不可达、不连生产、不调官方写接口），全部通过：
  1. `docker build` 成功，镜像 171MB；两个容器起来，`/health` 都 200。
  2. 看板首页 200（标题"团队看板"），任意前端路径回退 index.html 200。
  3. 未登录 `/api/me` 401 + `unauthorized`；错密码 `bad_credentials`；正确登录后 `/api/me` 正常。
  4. `/api/board` 空状态：`teams: []`，`rs.error = "无法连接 codex-rs，请检查地址和网络"`。
  5. 母号授权 start 生成桌面版授权链接与 ticket。不存在的团队同步返回 404 `not_found`。
  6. 授权桥：短令牌 404 `extension api disabled`、无 / 错令牌 401、正确令牌（scheme 小写也行）200 且返回 `rs_direct: true`；rs 不可达时 start 返回 `codex_unreachable`；坏邮箱 `email_required`；不存在的 ticket `callback_expired`；空请求体 422 `invalid_request`。
  7. `docker compose -p team48v2 -f deploy/compose.yaml config` 校验通过。
  联调容器、网络、临时配置已删除；本机保留了镜像 `team48:local` 和检查用镜像 `team48-dev:1.99.1`、Docker 卷 `team48-cargo-*` / `team48-target`（检查缓存）。
- 发现但未修的功能问题：无。已知限制：rs 不给额度重置时间（part-2），看板悬停显示"重置时间：未知"。
- 未做：真实官方接口和真实 rs 的端到端测试（按规则留到切换窗口在服务器做，见 cutover.md 第 3、4 步）。
- ship 时写进新仓库文档：
  - PRODUCT.md：两个服务各自做什么 / 不做什么（素材：dashboard、bridge README 和计划 README 的对齐结论）、插件用法（extension/README.md）、术语（母号、团队、rs、授权桥）。
  - RUNBOOK.md：环境一览（`/opt/team48-v2`、Compose `team48v2`、容器与端口、网络 `codex-proxy-rs_default`、数据 `/opt/team48-v2/data/team48.db`、`.env` 在服务器上 600）、部署命令、健康检查、日志（`docker logs team48-dashboard / team48-bridge`）、回滚、`MOTHER_REFRESH_ENABLED` 规则、老库导入用法；内容来自 `deploy/cutover.md`，并入后删除 cutover.md。
  - ARCHITECTURE.md 的 deploy 目录说明补上 Dockerfile / compose / nginx。

