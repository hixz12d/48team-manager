# 架构

## 技术栈

Python 3.11、FastAPI、SQLAlchemy 2 + aiosqlite（SQLite WAL）、Jinja2 + 原生 JS/CSS、APScheduler、Playwright（Chromium / 可选 Chromix）、httpx / curl-cffi。单容器、单库、单个浏览器执行槽。

## 目录地图

| 路径 | 作用 |
|---|---|
| `app/main.py` | 应用入口；启动时 `bootstrap_schema` 建表补列，再启动调度器 |
| `app/core/` | 配置（`config.py`）、加密、JWT、时间、代理工具 |
| `app/domain/` | 纯规则：身份、额度健康、轮转条件、HME 占用判定（`resources/__init__.py`） |
| `app/application/` | 业务服务：同步、额度、授权、邀请 / 补位、手动 / 自动轮转、Sub2API、Codex 导出 |
| `app/application/jobs/` | `scheduler.py` 定时任务、`dispatcher.py` 队列派发、`commands.py` 进程内命令执行 |
| `app/application/resources/` | HME 领号（`hme.py`）、手机号池、插件接码中转（`phone_relay.py`）、代理 |
| `app/integrations/` | 外部客户端：`openai`（官方 API + `browser/` 浏览器自动化）、`sub2api`、`codex`、`mail`、`sms`、`proxy` |
| `app/persistence/` | 表模型、仓储、`migrations/bootstrap.py`（启动时幂等建表 / 补列 / 补索引） |
| `app/web/` | 路由（`pages.py` 页面、`api.py` 接口、`extension.py` 插件接口、`runner.py` 运行器接口）、模板、静态资源；`static/fonts/` 是自托管的 Anthropic Sans / Mono |
| `DESIGN.md` | 界面设计规范（配色、字号、组件、状态写法），改界面前先读 |
| `extensions/chatgpt-signup/` | 自用注册插件；`content.js` 也被服务端托管注册复用；`runner.mjs` 是服务器模式 |
| `scripts/` | 本机工具：插件打包、单席位补位、浏览器冒烟、HubStudio 观察 |
| `tests/` | Python / Node / Playwright 回归；`preview_app.py` 本地示例数据预览 |
| `deploy/` | Dockerfile、compose、Nginx 配置 |

## 模块关系

```
浏览器页面 ──> web/routes ──> application 服务 ──> integrations ──> OpenAI / Sub2API / HME / Cloudflare 邮箱
                   │                 │
                   │                 └──> persistence（SQLite）
                   └── 长命令返回 202 + operation_id，jobs/commands 在进程内执行，步骤写 operations / operation_steps
插件 ──(/api/ext，Bearer 令牌)──> member_handoff ──> 同步 / 接入 / 授权 / 推送 / 计数
服务器 Chromix 里的插件 ──(/api/ext/runner，本次运行令牌)──> extension_runner ──> 轮转 / 自检
```

- 配置优先级：数据库设置（`system_settings`）> 环境变量 > 默认值。
- 官方 HTTP 只有 GET 有限重试；写请求超时或 5xx 返回 `write_outcome_unknown`，不自动重发。
- 命令没有持久化队列；进程重启时进行中的命令标为 `manual_required`。

### 定时任务（`jobs/scheduler.py`）

| 任务 | 间隔 | 说明 |
|---|---|---|
| `runtime_heartbeat` | 5 秒 | 运行状态心跳，供 `/api/runtime/status` |
| `quota_queue_dispatch` / `workspace_queue_dispatch` | 2 秒 | 派发额度检测 / 团队同步队列 |
| `official_quota_probe_scan` | 1 分钟 | 额度检测排程：成功后 60 分钟 + 抖动，失败退避 5/15/30/60 分钟，每批 1–3 个 |
| `auth_probe_scan` | 1 分钟 | 授权探测 |
| `auto_rotate_scan` | 1 分钟 | 自动轮转扫描（开关关闭时不动作） |
| `sub2api_status_sync` | 15 秒 | 远端状态核对；超过 45 秒算旧，单次 20 秒超时 |
| `sub2api_usage_sync` | 5 分钟 | 计费用量：5h / 今日 / 自然 7 天 / 全程（`lifetime`，自绑定起最多 90 天，`/accounts/{id}/stats?days=N`） |
| `auto_reauth_scan` | 30 分钟 | 自动重授权（默认关闭） |

### 手动轮转（`application/manual_rotation.py`）

- `FLOW_VERSION="manual_rotation_v1"`，注册固定走 extension 流程（不受 `BROWSER_SIGNUP_FLOW` 影响）。
- 接口：`POST /api/workspaces/{id}/rotate/preview`（只读预检）、`POST /api/workspaces/{id}/rotate`、`POST /api/operations/{id}/continue-rotation`（均 202）。
- 阶段：pause → kick → vacancy → onboard → publish → delete_old → count；步骤名 `paused`、`official_removed`、`kicked`、`joined`、`authorized`、`published`、`old_remote_deleted`、`counted`。父任务存旧号身份，步骤存新号、凭据版本、推送 Operation ID 和各阶段回执。
- SQLite 写锁序列化同邮箱跨团队分配和重复继续；旧远端是否已删只信完整鉴权的目录读取，单独 404 不算。计数与 `switch_counted_at` 同事务提交。
- 通用 `/retry` 拒绝轮转任务，只能 `continue-rotation`。

### 自动轮转（`application/automatic_rotation.py`、`rotate.py`）

设置键 `auto_rotate_enabled`、`auto_rotate_scope`（selected/all）、`auto_rotate_workspace_ids`、`auto_rotate_daily_limit`；推送重试上限 `MAX_PUBLISH_ATTEMPTS=5`。extension 模式下会先 `validate_signup_assets()`。

### 扩展运行器（`application/extension_runner.py`）

`ROTATION_SIGNUP_RUNNER=extension` 时，手动 / 自动轮转的"注册 + OAuth"改由服务器上的 Chromix 子进程加载真插件完成（不接 Playwright / CDP），其余阶段不变。协议全文见 [contracts/extension-runner.md](contracts/extension-runner.md)。

- `extension_runner.py`：运行登记、`signup_and_authorize`（轮转用，成功时在同一会话写凭据、`auth_state`、入组关系并提交）、`run_selfcheck`（自检）、错误码常量。不自己拿浏览器槽，调用方先 `InvitedBrowserSession().try_reserve()`。
- `integrations/openai/browser/runner_process.py`：生成运行目录 `data/runner-runs/<run_id>/`（复制插件、改写 manifest、生成 `private-config.mjs`，结束即删）、启动 / 按进程组结束 Chromix、Xvfb 1920×1080、带认证 SOCKS5 走本地桥；启动前拒绝 `--remote-debugging*`、`--headless` 等参数。
- `runner_profile.py`：档案 `data/chrome-profiles/runner/<email>/`，首次建档经母号代理查出口（`integrations/proxy/geo.py`）固定时区，之后不改；`chromix_args` 生成指纹参数。
- `web/routes/runner.py`：`/api/ext/runner/{run_id}/job|event|callback|probe|phone`，只认本次运行令牌，错误一律 404，不接受管理员会话。
- 接码：`signup_and_authorize(use_phone_pool=True)`（手动 / 自动轮转都开）时 `/job` 下发 `phonePool: true`，`/phone` 用 `runner_phone_context` 建上下文交 `phone_relay.handle`；运行结束关浏览器后释放本次锁的号。阶段 `phone` / `phone_otp` 记为进度步骤 `add_phone` / `sms_otp`。
- 接入点：`onboard.py` 的 `_extension_signup`（`signup_runner`、`use_phone_pool` 参数透传）；手动轮转在踢人前做 `validate_runner_configuration()` + `check_runner_proxy`，任务上下文记 `signup_runner`。
- 自检：任务类型 `runner_selfcheck`（浏览器类，占全局浏览器槽，不占团队锁），`console_actions.start_runner_selfcheck`；接口 `GET/POST /api/runner/selfcheck`、`GET /api/runner/selfcheck/{id}/screenshots/{name}`；截图 `data/selfcheck/`（保留最近 10 次）；启动时 `recover_runner_selfchecks` 把中断的自检标失败。步骤中文在 `presenters.py`。

### 团队收入账本（`application/revenue_ledger.py`）

- `settle_departure`（按团队 + 账号）/ `settle_binding`（按绑定，远端已删时 `allow_remote=False` 只用缓存）；只 flush，调用方 commit，失败不抛异常。
- 调用点：`rotate.py` 的 `kick_to_standby` 在 `official_removed` 之后、解绑 / 删远端 / 删档之前；`manual_rotation.py` 的 `stage_delete_old` 删旧远端前、`_drop_binding` 丢弃绑定前；`workspace_sync.py` 同步提交后给本次标为离队的成员记账（来源 `sync_departure`）；`main.py` 启动后后台跑 `backfill_departures` 补记漏掉的离队绑定。
- 金额来源优先级 `lifetime` / `lifetime_capped_90d` > `cache_lifetime` > `cache_seven_day` > `missing`，只有同级或更好才覆盖；`settled_at` 取首次入账时间。
- 查询：`totals()`（累计、本月、按团队）、`entries()`；接口 `GET /api/workspaces/{id}/revenue`，看板 `portfolio` 每组带 `revenue`，总览 summary 带 `revenue_total` / `revenue_month`。
- 每日收入（`application/revenue_daily.py`）：`record_day` 取较大值 upsert 到 `sub2api_revenue_daily`；写入来源 `sync`（`sub2api_usage` 的 `today` 窗口同步成功后）、`settle`（`settle_binding` 复用全程读取的 `summary.today`，读不到用当天 `today` 快照）、`backfill`（`main.py` 启动补录最近 7 天离队号，读 `/accounts/{id}/stats` 的 `history[]`，并对比 Sub2API `server_timezone` 与 `TIMEZONE`，不一致时打 warning 并写进 `note`）。
- 总览 `overview_totals(db, groups)` → `portfolio.revenue_overview` → summary 的 `revenue_running` / `revenue_today` / `revenue_seven_day`（`{user_cost, synced, total, stale, note}`）：在跑 = 各组 `lifetime` 快照之和；今日 / 近 7 天 = 在绑定号的快照 ＋ 离队号的每日记录，按远端号去重。

### 备用号池（`application/standby_pool.py`、`pool_join.py`）

- 表 `standby_pool_entries`（模型 `persistence/models/pool.py`）；接口 `web/routes/pool.py`（`/api/pool*`，schema 在 `web/schemas/pool.py`）；页面 `/resources/pool`（`templates/pool.html`、`static/js/pool.js`、`static/css/pool.css`）。
- `standby_pool.py`：导入、HME 收件检测（`probe_account_mailbox`）、打 / 恢复 `GPT号池` 标签、列表（按任务结果纠偏状态并算 `can_join / can_continue / can_remove`）、推荐团队（只读本地数据；席位未知不挡，每个候选团队带上次同步快照里的非母号已入组成员 `members`，含继承用的角色 / 席位）、移出、拉入成功后 `apply_team_label`。HME 调用用 `asyncio.to_thread`。
- `pool_join.py`：`start_pool_join` / `continue_pool_join` 预检后建 `pool_join` 任务（占全局浏览器槽 + 团队锁，禁止通用重试，重启后标待人工），可带 `replace_email`：后台先 `rotate.kick_to_standby(reason="pool_replace")` 移出该子号（步骤 `official_removed`，远端只暂停不删；回执显示空位计费时停在待人工），再调 `onboard.invite_and_onboard(login_existing=True, mailbox=…)`，成功后步骤 `pool_finish`（复用 `finish_after_authorization` 推送 + 计数）、`pool_label`（改团队标签），回写条目状态。继续时读上一任务的 `replace_email`，已移出的不重踢。
- `login_existing=True` 模式：强制 playwright + legacy 流程；不领 HME、不设密码、不走 Cloudflare，邀请邮件和验证码从 HME 收件读；浏览器 `run_browser_onboard(mode="login")` 只登录，识别到创建账号 / about-you 页返回 `account_not_registered`。不传新参数时原有邀请 / 补位 / 轮转路径不变。

### HME 领号（`application/resources/hme.py`）

`maybe_claim_alias` → `claim_next_alias`：排除租约中邮箱和 `occupied_account_emails`（accounts 表里仍在用的账号）→ `pick_next_unoccupied` → 写 `hme_alias_leases`（25 分钟，冲突最多 8 次）。收尾 `finalize_claim` 调 HME `POST /api/aliases/:id/label` 打本地标签。规则见 [hme-linkage.md](hme-linkage.md)。

### Sub2API 同步合约（`integrations/sub2api/client.py`）

- 先读 `GET /api/v1/admin/integration/capabilities`，要求 `oauth_sync.revision ∈ {3,4,5}` 且声明 `credential_cas`、`operation_receipts`、`atomic_receipts`、`resumable_followups`；否则返回 `sync_oauth_unsupported`，没有 PUT 退路。
- 写凭据只走 `sync-oauth-credentials`（带 `expected_instance_id`）；认证恢复要求 revision ≥ 4（Sub2API 迁移 249）；交回刷新权 `credential-refresh-handoff` 要求 revision 5（迁移 250）。
- Team 侧状态表都不存令牌明文。账号推送不带 `proxy_id`，保留远端已有绑定。

### 浏览器与注册（`integrations/openai/browser/`）

- `environment.py`：注册、入组、OAuth、重授权共用一个启动配置（`launch_persistent_context`，沿用代理和 SOCKS5 认证桥）。档案目录 `data/chrome-profiles/<email>/`，Chromix 在 `data/chrome-profiles/chromix/<email>/`；`.team48-browser.json` 固定种子、语言、时区、窗口，损坏即停止不重新随机。
- 注册流程 `BROWSER_SIGNUP_FLOW`：`legacy`（默认，需邀请链接）或 `extension`（先由官方 API 确认邀请，再从首页注册）。extension 失败不回退 legacy。
- extension 托管桥：`signup.py`、`signup_state.py`、`signup_readiness.py`、`signup_input.py`、`signup_bridge.js`。在隔离环境注入插件 `content.js`，输入走 CDP `Input.dispatch*` / `insertText`（真实事件），验证码在 Python 侧读取。主页可操作并两次确认登录邮箱才算完成；主页 30 秒仍未稳定时，若目标邮箱登录态已间隔确认两次，就带登录态交给官方入组 / 角色席位核对后再同页授权（截图存 `data/debug/<时间>/home-not-ready.png`，`signup_diagnostics.home_not_ready` 记各就绪条件），否则返回 `registration_home_not_ready`。
- 注册、入组确认、OAuth 共用同一进程、页面和代理（`InvitedBrowserSession`），全程占用全局浏览器槽。

### 插件接口（`web/routes/extension.py`、`application/member_handoff.py`）

`EXTENSION_API_TOKEN` 做 Bearer 鉴权，少于 24 位时 `/api/ext/*` 全部 404，不接受管理员会话。接口：`GET /ping`、`GET /workspaces`、`POST /resolve`（单团队 20 秒、总 60 秒）、`POST /phone`（接码，请求体 ≤ 4KB）、`POST /handoff`（可带 `phone_session`，接入时把成功的号码和接码链接写到账号）、`POST /handoff/complete`（推送去向读 `auth_push_target`，结果在 `followups.sub2api` 或 `followups.codex_rs`）。

### codex-rs 导入（`application/codex_publish.py`、`integrations/codex/client.py`）

- `client.py`：codex-rs 管理 API 客户端（`x-api-key`，不跟随重定向、不自动重试、15 秒超时）；HTTP 只放行回环和 `host.docker.internal`（容器经它访问同机 codex-rs `:8180`）。导出接口读到的 `refreshToken` 在客户端里就丢弃。
- `codex_publish.py`：`import_account` / `import_accounts`（单次 ≤ 50，逐个）、`pull_access_token`（在 `tokens.refresh_account` 的 `CredentialLease` 内读回 AT）、`disable_on_departure`（`batch-update enabled=false`）、`options`（分组 + 可用代理数）、`probe`。绑定表 `codex_bindings`（租约串行、`import_attempted` 一旦为真即视为 codex 续期；首次导入被 4xx 明确拒绝时删掉占位绑定）。
- 续期归属：`refresh_ownership.codex_refresh_owner` / `refresh_owner_kind`；`tokens.py`（刷新改读回）、`reauth.py` / `oauth_sessions.py`（自动重授权拦截 `remote_refresh_owned`）、`quota.py`（健康按远端续期处理、401 重试走读回）、`sub2api_publish.py`（互斥 `codex_rs_bound`）。
- 推送去向：`settings.load_auth_push_target` → `console_actions.account_reauth_complete(push_target=)` → `member_handoff.finish_after_authorization`；管理端重授权接口和插件 `/handoff/complete` 都传，轮转、号池不传（默认 `sub2api`）。
- 离队停用接入点：`rotate.kick_to_standby`（离队记账后，返回 `codex_rs_disable`）、`workspace_sync.py`（离队记账循环）。
- 接口：`GET /api/codex-rs/options`、`POST /api/accounts/codex-rs/import`（部分失败也是 200，看 `ok` / `results`）、`POST /api/settings/probe` 的 `codex_rs` 目标；账号行字段 `codex_rs`（`queries/identity.py`）。前端设置卡片 `static/js/codex-rs-settings.js`（`window.Team48CodexRs`）。

### 插件接码中转（`application/resources/phone_relay.py`）

协议见 [contracts/phone-relay.md](contracts/phone-relay.md)。`handle` 分派 `acquire`（领号，同会话幂等并续期锁）/ `code`（读短信，只返回与领号时基线不同的码）/ `report`（写 `phone_attempts`、按原因降级号码）/ `release`。本机模式 `personal_context` 按账号代理 → 母号代理定位读短信代理，按会话缓存 2 小时。基线存进程内存，服务重启后丢失。插件侧在 `content.js`（识别手机页；号码页直线流程 `phoneNumberStep` → `phoneSmsChannel` → `phoneContinue` → `phoneJudge`，失败上报后刷新页面换号，20 秒无反应按 `cancelled` 上报；短信页 90 秒无码点"更换号码"）和 `background.js`（调用接口、每账号最多 3 个号），新增暂停原因 `phone_pool_empty` / `phone_limit` / `phone_back_missing` / `phone_relay_error`。

## 数据存储

SQLite `data/team48.db`（WAL；需 SQLite ≥ 3.25）。表由 `bootstrap.py` 启动时自动建 / 补列，无单独迁移命令。

| 分组 | 主要表 |
|---|---|
| 身份 | `accounts`、`workspaces`、`workspace_memberships`、`workspace_official_member_snapshots`、`external_bindings` |
| 任务 | `operations`、`operation_steps` |
| 额度 | `quota_snapshots`、`quota_probe_states`、`quota_dispatch_lease`、`credential_leases` |
| Sub2API | `sub2api_sync_observations`、`sub2api_refresh_authorities`、`sub2api_refresh_handoffs`、`sub2api_account_status`、`sub2api_usage_snapshots`、`sub2api_proxy_bindings`、`sub2api_revenue_entries`（收入账本，无外键，存邮箱和团队名快照，唯一键 团队 + 远端账号 ID）、`sub2api_revenue_daily`（每日收入，每个远端号每天一行存当天累计 U，只增不减，无外键，不随团队 / 账号 / 绑定删除） |
| 其他 | `oauth_sessions`、`codex_bindings`（codex-rs 绑定：远端 ID、`import_attempted`、`remote_enabled`、`official_workspace_id`、`last_pulled_at`）、`system_settings`、`hme_alias_leases`、`phone_pool`、`phone_attempts`、`proxy_profiles`、`seat_vacancy_events`、`standby_pool_entries`（备用号池条目） |

凭据加密存储；接口只返回 `secret_state`（stored / missing），不回显密钥。浏览器档案也在 `data/` 下。

## 本地运行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python -m uvicorn app.main:app --reload --port 8008
```

打开 `http://127.0.0.1:8008`，账号密码来自 `.env`。只看界面用 `python -m uvicorn tests.preview_app:app --port 8019`（示例数据，禁止写操作）。

## 基本检查

秒级检查，改完必跑，确保能启动：

```bash
.venv/Scripts/python.exe -m compileall -q app scripts
.venv/Scripts/python.exe -c "import app.main"
for f in app/web/static/js/*.js extensions/chatgpt-signup/*.js extensions/chatgpt-signup/*.mjs; do node --check "$f"; done
```

## 配置项

环境变量（`app/core/config.py`，读 `.env`；示例见 `.env.example`、`deploy.env.example`）：

| 变量 | 作用 |
|---|---|
| `APP_HOST` / `APP_PORT` | 监听地址，默认 `0.0.0.0:8008` |
| `DATABASE_URL` | 默认 `data/team48.db`，不要指向旧 `team_manage.db` |
| `SECRET_KEY` / `SESSION_SECRET_KEY` / `ENCRYPTION_KEY` | 主密钥；后两个为空时用 `SECRET_KEY`。代码默认值是占位，生产必须覆盖 |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 管理员登录；代码默认密码很弱，生产必须覆盖 |
| `SESSION_COOKIE_SECURE` | 生产为 True |
| `EXTENSION_API_TOKEN` | 插件接口令牌，≥ 24 位才启用 |
| `BROWSER_HEADLESS` / `BROWSER_CHANNEL` / `BROWSER_EXECUTABLE` | 浏览器启动 |
| `BROWSER_ENGINE` | `chromium`（默认）/ `chromix`（需同时设 `BROWSER_EXECUTABLE`） |
| `BROWSER_SIGNUP_FLOW` | `legacy`（默认）/ `extension` |
| `BROWSER_LOCALE` / `BROWSER_TIMEZONE` | 新建档案的语言 / 时区 |
| `ROTATION_SIGNUP_RUNNER` | 轮转注册方式：`playwright`（默认）/ `extension`（扩展运行器） |
| `RUNNER_BROWSER_EXECUTABLE` | Chromix 153 启动脚本 `chromix`（不是 `chrome`），extension 模式必填 |
| `RUNNER_FINGERPRINT_PLATFORM` / `RUNNER_GPU_MODE` | 新建运行器档案的伪装平台 `linux`/`windows`、显卡 `native`/`preset`；已有档案不变 |
| `RUNNER_EXTENSION_DIR` / `RUNNER_LOCAL_BASE_URL` / `RUNNER_TIMEOUT_SECONDS` | 插件源目录、插件回连地址（回环）、单次上限（默认 1500 秒） |
| `OPENAI_CA_BUNDLE` | OpenAI 请求自定义 CA |
| `IDENTITY_GMAIL_POLICY` | `owner_only`（默认）/ `warn` / `unrestricted` |
| `OFFICIAL_QUOTA_PROBE_ENABLED` / `AUTO_REAUTH_ENABLED` / `AUTO_ROTATE_ENABLED` / `FORCE_REFILL` | 自动化开关，默认全关，数据库设置优先 |
| `LOG_LEVEL` / `DATABASE_ECHO` / `TIMEZONE` | 日志、SQL 回显、时区 |

界面设置（存 `system_settings`）：Sub2API 地址与密钥、`sub2api_push_defaults`、codex-rs 地址与管理 Key（`codex_base_url` / `codex_admin_key_encrypted`）、`auth_push_target`（`sub2api` / `codex_rs`）、`codex_rs_import_defaults`（分组、并发、权重、启用）、HME 地址与服务 token（`hme_base_url` 默认 `http://icloud-hme:8081`）、Cloudflare 邮箱、`invite_seat_wire_standard/premium`、`official_quota_probe_batch_size`、自动轮转各项。
