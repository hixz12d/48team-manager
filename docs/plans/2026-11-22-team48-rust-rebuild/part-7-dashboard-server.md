# Part 7：看板后端

**状态**：已完成
**依赖**：part-1、part-2 已完成
**所属计划**：[README](./README.md)

## 目标

实现 `apps/dashboard` 的服务端：管理员登录、`GET /api/board`、成员同步、邀请 / 撤回 / 踢人、改团队名和到期日、删除本地团队、母号授权（生成链接 + 粘贴回调）、母号令牌定时续期、定时读 rs 账号列表并和团队成员匹配；把前端构建产物嵌进二进制并提供页面。

## 可改文件

- `team48/apps/dashboard/src/server.rs`（part-0 占位，本 Part 实现）
- `team48/apps/dashboard/src/` 下新建的模块（如 `auth.rs`、`board.rs`、`teams.rs`、`mother_auth.rs`、`refresh.rs`、`rs_cache.rs`、`db.rs`、`web.rs`），或放进 `team48/apps/dashboard/src/app/` 目录
- `team48/apps/dashboard/Cargo.toml`
- `team48/apps/dashboard/migrations/`（只允许**新增** `0002_*.sql`，不改 `0001_init.sql`）
- `team48/apps/dashboard/README.md`（新建）
- 不得改：`src/main.rs`、`src/legacy_import.rs`（及 `src/legacy_import/`）、`web/**`

## 只读参考

- 新仓库 `docs/contracts/dashboard-api.md`（接口唯一依据）
- 新仓库 `crates/openai`、`crates/rs-client`、`crates/kit` 的 README 与公开接口
- 老仓库 `C:\Projects\VPS\天理云-美西国际大宽带-8H16G\48team-manager\`：
  - `app/application/workspace_sync.py`、`app/application/workspaces.py`（同步落库、失败保留旧快照、团队名优先级）
  - `app/application/member_lifecycle.py`、`app/application/invitation_flow.py`（邀请 / 踢人 / 撤回的前置检查和结果核对）
  - `app/application/tokens.py`（母号令牌何时刷新、刷新后覆盖保存、401 判定）
  - `app/application/console_actions.py` 中账号重新授权相关函数（生成链接、粘贴回调）
  - `app/application/extension_rs.py` 第 223～231 行附近（rs 账号与本地身份的匹配方式）

## 要点

- **登录**：`ADMIN_USERNAME` / `ADMIN_PASSWORD` 常量时间比较；签名 Cookie 会话（`SESSION_SECRET`，HttpOnly、SameSite=Lax、生产 Secure）；除登录和静态页面外所有 `/api/*` 要求登录。
- **看板 `GET /api/board`**：从库里读团队、母号、成员；从内存 rs 缓存给每个成员匹配 rs 账号：`rs.workspace_id == team.official_workspace_id` 且（`rs.user_id == member.official_user_id` 或小写邮箱相等），匹配不到 `not_in_rs`，rs 缓存为空 `unknown`；周限窗口取 `window_seconds ≥ 604800 × 95%` 的那条，5 小时窗口有才给；缓存超过 3 分钟 `stale = true`。`days_left` 按北京时间算。
- **rs 缓存**：启动后每 60 秒 `list_accounts` 一次存内存；失败保留上次结果并记录错误和时间，不清空。
- **成员同步 `POST /api/teams/{id}/sync`**：用母号令牌读 `list_members` + `list_invites`；成功后在一个事务里整体替换该团队 `members`、更新 `seats_used`、`last_synced_at`、`last_sync_state = ok`；失败（含零件报"总数 > 0 但解析 0 条"）只记 `failed` 和原因，**保留旧名单**。不做定时同步。
- **写操作**：同一团队用内存互斥锁防重入（第二个请求直接返回"该团队有操作进行中"）；调用零件；`WriteOutcome::Unknown` 时立即回读成员 / 邀请列表，把核对结果放进响应，`code = write_outcome_unknown`；成功后同步更新本地名单（或直接触发一次同步）。**踢人不碰 rs**。
- **母号授权**：`start` 生成 PKCE + state + 桌面版链接（版本号从 rs 读并缓存 30 分钟，读不到用 `CODEX_DESKTOP_APP_VERSION`），会话存内存（有效期参考老代码）；`complete` 换票（走母号代理）→ 解析身份 → `list_workspaces` 找出该母号能管理的 Team → 新建 / 更新 `mothers`（加密保存令牌和代理）和 `teams`（新团队名用官方名）→ 返回新增 / 更新列表。母号邮箱与令牌邮箱不一致时拒绝。
- **母号续期**：每 30 分钟检查一次，按老 `tokens.py` 的时机刷新；刷新成功立即覆盖保存新的刷新令牌（会轮换）；401 / 刷新被拒记 `needs_reauth`；网络错误、5xx 不改授权状态只记错误。环境变量 `MOTHER_REFRESH_ENABLED`（默认 `true`；新老并跑测试期间设 `false`，见 README 上线注意），本 Part 顺手在 dashboard README 写明。
- **页面**：用 rust-embed 嵌入 `web/dist`，非 `/api` 路径回退到 `index.html`；`/health` 返回 200。
- 日志不打印令牌、代理密码、回调地址；邮箱只留前 3 位。

## 步骤

1. 实现配置、数据库连接与迁移执行、会话登录。
2. 实现 rs 缓存与 `GET /api/board` 匹配逻辑。
3. 实现同步、写操作、团队编辑 / 删除。
4. 实现母号授权与续期任务。
5. 嵌入前端、`/health`；写 README；跑检查。

## 完成标准

- [x] 合约里每个接口都已实现，字段与合约一致
- [x] 同步失败时旧名单保留；写操作结果未知时返回回读核对结果
- [x] `MOTHER_REFRESH_ENABLED=false` 时绝不调用刷新接口
- [x] 基本检查通过：`bash scripts/check.sh`

## 完成记录

- 由 Gemini 施工、Claude 验收。改动：`apps/dashboard/src/server.rs`（配置、共享状态、路由、后台任务）与新建 `src/server/` 下 `api.rs`（统一响应 / 400 参数提取）、`auth.rs`（常量时间登录、签名 Cookie、`/api/*` 鉴权）、`db.rs`（建库、外键、WAL、手工迁移器——与 `legacy_import/write.rs` 校验和一致，没开 sqlx `macros`）、`rs_cache.rs`（60 秒 rs 轮询 + 桌面版版本号 30 分钟缓存）、`board.rs`（看板匹配、北京时间剩余天数）、`teams.rs`（团队互斥锁、同步、邀请 / 撤回 / 踢人、改名 / 到期日、删本地团队）、`mother_auth.rs`（两步授权与团队发现）、`refresh.rs`（30 分钟巡检，距过期 ≤ 2 小时刷新）、`web.rs`（嵌入前端、`/health`）、`README.md`。没加迁移文件，Cargo.toml 未改。
- 新增环境变量：`MOTHER_REFRESH_ENABLED`（默认 true）、`COOKIE_SECURE`（默认 true，本地明文 HTTP 调试设 false）。
- Claude 核对：写操作 `Unknown` 时立即回读名单、返回 `write_outcome_unknown` + `verify`，不重发；写操作遇 401 也不重发（开关开时顺手刷新令牌，让用户重新提交）；`MOTHER_REFRESH_ENABLED=false` 时巡检任务不启动，各处调用 `refresh` 前都判断开关；同步在事务里整体替换 `members`，失败保留旧名单。
- 检查：`bash scripts/check.sh` 全量通过（cargo check + clippy、前端 build、插件 node --check）。
- ship 时写进文档：dashboard README 已写环境变量和上线提醒；`.env.example` 由 part-8 补上两个新变量。

