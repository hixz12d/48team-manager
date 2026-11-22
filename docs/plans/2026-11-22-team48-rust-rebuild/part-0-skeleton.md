# Part 0：新仓库骨架与公共约定

**状态**：已完成
**依赖**：无
**所属计划**：[README](./README.md)

## 目标

在 `C:\Projects\VPS\天理云-美西国际大宽带-8H16G\team48\`（下称新仓库，下文路径都相对它）建好能编译通过的空骨架：Cargo workspace、三个零件库的公开类型与函数签名（函数体先 `todo!()`）、两个服务的入口、看板数据库结构、两份接口合约、配置样例、Docker 里的检查脚本、Vue 前端脚手架。之后 5 个窗口可以在互不重叠的文件里并行施工。

## 可改文件

全部为新建：

- `.gitignore`、`.env.example`、`AGENTS.md`、`CLAUDE.md`（只有一行 `@AGENTS.md`）、`README.md`
- `Cargo.toml`（workspace）、`rust-toolchain.toml`
- `crates/kit/**`（Cargo.toml、src/lib.rs、src/crypto.rs、src/config.rs）
- `crates/openai/Cargo.toml`、`crates/openai/src/lib.rs`（只放公开类型与签名，子模块文件由 part-1 建）
- `crates/rs-client/Cargo.toml`、`crates/rs-client/src/lib.rs`（同上，子模块由 part-2 建）
- `apps/dashboard/Cargo.toml`、`apps/dashboard/src/main.rs`、`apps/dashboard/src/server.rs`（占位）、`apps/dashboard/src/legacy_import.rs`（占位）、`apps/dashboard/migrations/0001_init.sql`
- `apps/dashboard/web/`：`package.json`、`vite.config.ts`、`tsconfig*.json`、`index.html`、`src/main.ts`、`src/App.vue`（占位页）、`src/api/types.ts`（按看板合约写的 TS 类型）、`dist/.gitkeep`
- `apps/bridge/Cargo.toml`、`apps/bridge/src/main.rs`、`apps/bridge/src/server.rs`（占位）
- `docs/ARCHITECTURE.md`（目录地图 + 零件 / 服务边界 + 基本检查命令）
- `docs/contracts/bridge-api.md`、`docs/contracts/dashboard-api.md`
- `deploy/dev.Dockerfile`、`scripts/check.sh`

## 只读参考

- 老仓库 `C:\Projects\VPS\天理云-美西国际大宽带-8H16G\48team-manager\`（下称老仓库）：
  - `app/core/crypto.py`（Fernet 密钥派生方式）
  - `app/web/routes/extension.py`、`app/application/extension_rs.py`（插件接口的请求 / 响应 / 错误格式）
  - `app/persistence/models/identity.py`（老表结构，帮助设计新表）
  - `docs/contracts/openai-invite.md`（席位类型）
- rs 仓库 `..\codex-proxy-rs\backend\Cargo.toml`、`rust-toolchain.toml`（依赖版本风格、工具链 `1.99.1`）
- 本计划 [README](./README.md) 的对齐结论

## 要点

- **工具链**：`rust-toolchain.toml` 与 rs 一致（channel `1.99.1`，components clippy、rustfmt）。依赖：axum 0.8、tokio、sqlx（crates.io 版，sqlite + runtime-tokio，只用运行时 `query` / `query_as`，不用 `query!` 宏）、serde、serde_json、thiserror、tracing、wreq（浏览器 TLS 指纹客户端，选最新稳定版并确认能编译）、fernet、rust-embed、clap。公共依赖版本统一写在 workspace `[workspace.dependencies]`。
- **kit 零件**：`crypto::SecretBox::from_key(&str)`，密钥派生与老项目 `app/core/crypto.py` 完全一致（SHA-256 后 urlsafe base64 作为 Fernet key），提供 `encrypt(&str) -> String` / `decrypt(&str) -> Result<String>`；本 Part 就实现完整（很小）。`config` 提供读环境变量的小工具（必填缺失报清楚的错）。
- **openai 零件公开接口**（lib.rs 定义类型和签名，函数体 `todo!()`，由 part-1 实现；字段名可参考老代码，但签名一旦定下 part-1 不得改公开形状，确需改动在完成记录说明）：
  - `ProxyUrl`、`OfficialClient::new(proxy: Option<ProxyUrl>, device_id: String) -> Result<Self>`
  - OAuth：`Pkce::generate()`、`AuthEntry { Desktop, Legacy }`、`build_authorize_url(entry, &AuthorizeParams{email, app_version, pkce, state}) -> String`、`parse_callback(url: &str, expected_state: &str) -> Result<CallbackCode>`（`localhost` 与 `127.0.0.1` 视为同一回环主机）、`OfficialClient::exchange_code(code, verifier) -> Result<TokenSet>`、`OfficialClient::refresh(refresh_token) -> Result<TokenSet>`
  - `TokenSet { access_token, refresh_token: Option, id_token: Option, expires_at }`、`Identity::from_tokens(&TokenSet) -> Result<Identity{ email, user_id, account_id, plan_type }>`、`TEAM_PLAN_TYPES` / `is_team_plan(&str) -> bool`
  - 团队：`list_workspaces(at)`（母号可管理的团队，对应老代码 `/accounts/check`）、`list_members(at, account_id) -> Result<Vec<Member>>`、`list_invites(at, account_id) -> Result<Vec<Invite>>`、`invite(at, account_id, email, SeatType{Standard, Premium}) -> WriteOutcome`、`revoke_invite(at, account_id, email) -> WriteOutcome`、`remove_member(at, account_id, user_id) -> WriteOutcome`
  - `WriteOutcome { Done(serde_json::Value), Unknown { reason }, Rejected { code, message } }`；`OpenAiError` 区分网络错误、401、403、429（带 Retry-After）、5xx、解析失败
- **rs-client 零件公开接口**（同上，由 part-2 实现）：`RsClient::new(base_url, admin_key) -> Result<Self>`；`list_accounts() -> Result<Vec<RsAccount>>`（字段：`id`、`email`、`enabled`、`status`、`status_reason`、`workspace_id`、`user_id`、`windows: Vec<QuotaWindow{ window_seconds, used_percent, limit_reached, resets_at }>`）；`account_detail(id)`；`find_by_email(email) -> Result<Vec<RsAccount>>`；`pick_proxy() -> Result<ProxyChoice{ id, url }>`；`proxy_for_account(id) -> Result<Option<ProxyChoice>>`；`import_account(&ImportRequest) -> Result<String>`（返回远端 ID）；`restore_settings(id, &AccountSettings)`；`desktop_app_version() -> Result<String>`。
- **看板数据库 `0001_init.sql`**（SQLite，时间一律存 UTC 文本或毫秒整数，统一一种）：
  - `mothers`：id、email（唯一）、official_user_id、proxy_url_enc、access_token_enc、refresh_token_enc、id_token_enc、token_expires_at、auth_state（`ok` / `expired` / `needs_reauth` / `unknown`）、last_refresh_at、last_refresh_error、device_id、created_at、updated_at
  - `teams`：id、official_workspace_id（唯一）、name、mother_id（外键）、seat_limit、seats_used、expires_on（日期，可空）、last_synced_at、last_sync_state（`ok` / `failed`）、last_sync_error、created_at、updated_at
  - `members`：id、team_id、email、official_user_id、role、seat_type、status（`active` / `invited`）、invited_at、joined_at、synced_at；每次同步成功整体替换该团队的行
  - 不建任务表、不建设置表（配置走环境变量）。
- **看板接口合约 `docs/contracts/dashboard-api.md`**（part-3 前端与 part-7 后端都按它做，写清每个字段）：
  - 认证：`POST /api/login {username,password}`、`POST /api/logout`、`GET /api/me`；未登录一律 401。
  - `GET /api/board` → `{ teams: [Team], rs: { fetched_at, stale, error } }`；`Team = { id, name, official_workspace_id, seat_limit, seats_used, expires_on, days_left, last_synced_at, sync_state, sync_error, mother: { id, email, auth_state, last_refresh_at }, members: [Member], invites: [Invite] }`；`Member = { email, official_user_id, role, seat_type, joined_at, rs: { match: "matched" | "not_in_rs" | "unknown", account_id, enabled, status, weekly: Window | null, five_hour: Window | null, stale } }`；`Window = { used_percent, limit_reached, resets_at }`。
  - 写操作：`POST /api/teams/{id}/sync`、`POST /api/teams/{id}/invites {email, seat:"standard"|"premium"}`、`DELETE /api/teams/{id}/invites {email}`、`DELETE /api/teams/{id}/members/{user_id}`、`PATCH /api/teams/{id} {name?, expires_on?}`、`DELETE /api/teams/{id}`（只删本地）。
  - 母号授权：`POST /api/mother-auth/start {email, proxy_url?, mother_id?}` → `{ticket, authorize_url}`；`POST /api/mother-auth/complete {ticket, callback_url}` → `{mother, teams_added: [...], teams_updated: [...]}`。
  - 返回约定：业务结果 HTTP 200 + `{ok: true|false, code?, message?, ...}`；结果未知 `code = "write_outcome_unknown"` 并附回读核对结果；参数错误 400；`{code, message}` 中文 message。
- **授权桥合约 `docs/contracts/bridge-api.md`**：照抄老仓库 `extension.py` + `extension_rs.py` 当前行为：Bearer 令牌校验规则（少于 24 位全部 404、错误令牌 401）、`GET /api/ext/ping` 返回体（保留 `rs_direct: true`）、`POST /api/ext/rs/start {email}`、`POST /api/ext/rs/complete {ticket, callback_url}` 的成功与各错误码响应字段，逐字段写清。插件靠这些字段工作，不得改名。
- **配置 `.env.example`**：看板 `ADMIN_USERNAME`、`ADMIN_PASSWORD`、`SESSION_SECRET`、`ENCRYPTION_KEY`、`DATABASE_PATH`、`RS_BASE_URL`、`RS_ADMIN_KEY`、`DASHBOARD_BIND`（默认 `0.0.0.0:8028`）；授权桥 `EXTENSION_API_TOKEN`、`RS_BASE_URL`、`RS_ADMIN_KEY`、`RS_IMPORT_DEFAULTS`（JSON：group_ids、concurrency、weight、enabled）、`BRIDGE_BIND`（默认 `0.0.0.0:8029`）；两边共用 `CODEX_DESKTOP_APP_VERSION`（桌面版版本号兜底，平时从 rs 读）；导入 `LEGACY_ENCRYPTION_KEY`。只写占位值。
- **依赖一次配齐**：`apps/dashboard/Cargo.toml` 和 `apps/bridge/Cargo.toml` 在本 Part 就写好后续要用的依赖（axum、tokio、sqlx、tower-http、axum-extra 或 tower-sessions 等会话库、rust-embed、clap、tracing-subscriber、本仓库三个零件），避免后续并行 Part 抢改同一个 Cargo.toml。后续 Part 若仍需加依赖：part-7 可改 `apps/dashboard/Cargo.toml`，part-6 可改 `apps/bridge/Cargo.toml`，part-5 不得改。
- **服务入口**：`apps/dashboard/src/main.rs` 用 clap：`serve`（调 `server::run(config)`）和 `import-legacy`（调 `legacy_import::run(args)`）两个子命令；`server.rs`、`legacy_import.rs` 只放签名和 `todo!()`。`apps/bridge/src/main.rs` 调 `server::run(config)`。main.rs 之后不再改，part-5 / part-7 只改各自文件。
- **检查脚本**：`deploy/dev.Dockerfile` 基于 `rust:1.99.1-bookworm`，补装 wreq（BoringSSL）编译需要的 cmake、clang、libclang 等；`scripts/check.sh` 构建该镜像（有缓存就复用），用具名卷缓存 cargo registry 和 target，在容器里跑 `cargo check --workspace` 和 `cargo clippy --workspace -- -D warnings`（clippy 失败只提示不阻断也可，写清楚），再在本机跑 `npm --prefix apps/dashboard/web run build` 和 `extension/` 下所有 `.js` / `.mjs` 的 `node --check`（目录不存在就跳过）。支持分段参数，供并行 Part 只查自己的部分：`scripts/check.sh rust <包名>`（只 `cargo check -p <包名>`）、`scripts/check.sh web`、`scripts/check.sh ext`；不带参数跑全部。在 ARCHITECTURE.md 写明：并行施工时别的 Part 的零件可能暂时编译不过，先查自己的包，最终以不带参数的全量检查为准。
- **AGENTS.md** 写：开工必读（ARCHITECTURE、contracts）、零件 / 服务边界规则（服务间只走 HTTP、不共享数据库、零件不依赖服务）、工程约定（只写现状、不写测试、改完跑 `scripts/check.sh`、不提交密钥 / `private-config.mjs` / 数据库）、硬性规则（官方写请求不自动重发；母号令牌不能新老并跑刷新）、沟通（简体中文、面向非专业用户）。
- `.gitignore`：`target/`、`.env`、`*.db*`、`node_modules/`、`apps/dashboard/web/dist/*`（保留 `.gitkeep`）、`extension/private-config.mjs`、`extension/dist/`、`.tmp/`。

## 步骤

1. 读只读参考里的老代码，确定 bridge 合约字段和 Fernet 派生方式。
2. 建 workspace、kit（完整实现）、openai / rs-client 的 lib.rs 签名、两个服务的 main.rs 和占位文件。
3. 写 `0001_init.sql`、两份合约、`.env.example`、AGENTS.md、ARCHITECTURE.md。
4. 建 Vue 脚手架（Vue 3 + Vite + TypeScript，不引 UI 库），`src/api/types.ts` 按合约写类型。
5. 写 `deploy/dev.Dockerfile` 和 `scripts/check.sh`，跑通。

## 完成标准

- [x] `bash scripts/check.sh` 通过（`cargo check --workspace` 无错误，前端能 build）
- [x] 两份合约逐字段写清，bridge 合约与老代码行为一致
- [x] kit 的 `SecretBox` 能解开老项目同密钥加密的值（写一个临时小程序或在完成记录里说明核对方法，临时文件用完删）
- [x] 新仓库 `git init`，暂不提交、不建远端

## 完成记录

- 新仓库建在 `C:\Projects\VPS\天理云-美西国际大宽带-8H16G\team48\`，已 `git init`，未提交。`bash scripts/check.sh` 全量通过（cargo check + clippy -D warnings + 前端 build；extension/ 尚不存在被跳过）。
- 依赖实际版本：wreq `6.0.0-rc.31` + wreq-util `3.0.0-rc.14`（crates.io 上 wreq 的"稳定版"0.16 是旧包，6.x 仍为 rc，已确认能在 Docker 里编译 BoringSSL）；rs-client 用 reqwest 0.12（rustls）；fernet 用 `rustcrypto` 特性（避免和 BoringSSL 冲突）；会话用 axum-extra `cookie-signed`；sqlx 0.9.1（crates.io）。
- 与计划的小偏差：`main.rs` 调 `server::Config::from_env()` 再 `server::run(config)`，`Config` 定义在 `server.rs`（part-6 / part-7 自己定字段）；`import-legacy` 的参数结构 `legacy_import::Args` 定义在 `legacy_import.rs`，part-5 可增删字段而不改 main.rs。
- 公开接口补充：openai 零件额外公开 `CLIENT_ID`、`REDIRECT_URI`、`TEAM_PLAN_PREFIXES`、`generate_state()`、`ProxyUrl::parse/redacted`、`OpenAiError::TokenRejected` 与 `code()`；`is_team_plan` 已在 part-0 实现。rs-client 额外公开 `RsError::code()`（错误码与老 `CodexTransferError` 一致）、`RsAccountDetail`、`AccountSettings`、`ImportDefaults`、`ImportMode`（New / OverwriteSameIdentity / OverwritePreserving）、`RsAccount::weekly_window/five_hour_window`、`WEEKLY_MIN_SECONDS`。kit 额外有 `mask_email`、`normalize_email`。
- 合约里新定的（老代码没有的）：授权桥请求体不合法返回 422 + `invalid_request` 失败体；每请求总超时 90 秒、`error_code = timeout`。看板合约新增 `Verify`、`Mother`、`TeamRef` 类型和 `team_busy` 等错误码。
- SecretBox 兼容性核对：用 Python `cryptography` 按老 `TokenCipher` 方式以密钥 `test-key-123` 加密中文文本，Rust `SecretBox::from_key("test-key-123").decrypt` 解出原文一致；临时 example 已删除。
- 前端脚手架：Vue 3.5 + Vite 8 + TS 6 + vue-tsc 3.3.14（与 rs 一致）；`npm run build` 后自动补回 `dist/.gitkeep`。
- 时间存储统一为 RFC 3339 UTC 文本（`0001_init.sql` 头部注释写明）。
- ship 时写进文档：ARCHITECTURE 已写在新仓库；检查命令 `bash scripts/check.sh [rust [包]|web|ext]`。

