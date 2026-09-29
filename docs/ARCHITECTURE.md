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
| `app/application/resources/` | HME 领号（`hme.py`）、手机号池、代理 |
| `app/integrations/` | 外部客户端：`openai`（官方 API + `browser/` 浏览器自动化）、`sub2api`、`codex`、`mail`、`sms`、`proxy` |
| `app/persistence/` | 表模型、仓储、`migrations/bootstrap.py`（启动时幂等建表 / 补列 / 补索引） |
| `app/web/` | 路由（`pages.py` 页面、`api.py` 接口、`extension.py` 插件接口）、模板、静态资源 |
| `extensions/chatgpt-signup/` | 自用注册插件；`content.js` 也被服务端托管注册复用 |
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
| `sub2api_usage_sync` | 5 分钟 | 计费用量 |
| `auto_reauth_scan` | 30 分钟 | 自动重授权（默认关闭） |

### 手动轮转（`application/manual_rotation.py`）

- `FLOW_VERSION="manual_rotation_v1"`，注册固定走 extension 流程（不受 `BROWSER_SIGNUP_FLOW` 影响）。
- 接口：`POST /api/workspaces/{id}/rotate/preview`（只读预检）、`POST /api/workspaces/{id}/rotate`、`POST /api/operations/{id}/continue-rotation`（均 202）。
- 阶段：pause → kick → vacancy → onboard → publish → delete_old → count；步骤名 `paused`、`official_removed`、`kicked`、`joined`、`authorized`、`published`、`old_remote_deleted`、`counted`。父任务存旧号身份，步骤存新号、凭据版本、推送 Operation ID 和各阶段回执。
- SQLite 写锁序列化同邮箱跨团队分配和重复继续；旧远端是否已删只信完整鉴权的目录读取，单独 404 不算。计数与 `switch_counted_at` 同事务提交。
- 通用 `/retry` 拒绝轮转任务，只能 `continue-rotation`。

### 自动轮转（`application/automatic_rotation.py`、`rotate.py`）

设置键 `auto_rotate_enabled`、`auto_rotate_scope`（selected/all）、`auto_rotate_workspace_ids`、`auto_rotate_daily_limit`；推送重试上限 `MAX_PUBLISH_ATTEMPTS=5`。extension 模式下会先 `validate_signup_assets()`。

### HME 领号（`application/resources/hme.py`）

`maybe_claim_alias` → `claim_next_alias`：排除租约中邮箱和 `occupied_account_emails`（accounts 表里仍在用的账号）→ `pick_next_unoccupied` → 写 `hme_alias_leases`（25 分钟，冲突最多 8 次）。收尾 `finalize_claim` 调 HME `POST /api/aliases/:id/label` 打本地标签。规则见 [hme-linkage.md](hme-linkage.md)。

### Sub2API 同步合约（`integrations/sub2api/client.py`）

- 先读 `GET /api/v1/admin/integration/capabilities`，要求 `oauth_sync.revision ∈ {3,4,5}` 且声明 `credential_cas`、`operation_receipts`、`atomic_receipts`、`resumable_followups`；否则返回 `sync_oauth_unsupported`，没有 PUT 退路。
- 写凭据只走 `sync-oauth-credentials`（带 `expected_instance_id`）；认证恢复要求 revision ≥ 4（Sub2API 迁移 249）；交回刷新权 `credential-refresh-handoff` 要求 revision 5（迁移 250）。
- Team 侧状态表都不存令牌明文。账号推送不带 `proxy_id`，保留远端已有绑定。

### 浏览器与注册（`integrations/openai/browser/`）

- `environment.py`：注册、入组、OAuth、重授权共用一个启动配置（`launch_persistent_context`，沿用代理和 SOCKS5 认证桥）。档案目录 `data/chrome-profiles/<email>/`，Chromix 在 `data/chrome-profiles/chromix/<email>/`；`.team48-browser.json` 固定种子、语言、时区、窗口，损坏即停止不重新随机。
- 注册流程 `BROWSER_SIGNUP_FLOW`：`legacy`（默认，需邀请链接）或 `extension`（先由官方 API 确认邀请，再从首页注册）。extension 失败不回退 legacy。
- extension 托管桥：`signup.py`、`signup_state.py`、`signup_readiness.py`、`signup_input.py`、`signup_bridge.js`。在隔离环境注入插件 `content.js`，输入走 CDP `Input.dispatch*` / `insertText`（真实事件），验证码在 Python 侧读取。主页可操作并两次确认登录邮箱才算完成，30 秒未就绪返回 `registration_home_not_ready`。
- 注册、入组确认、OAuth 共用同一进程、页面和代理（`InvitedBrowserSession`），全程占用全局浏览器槽。

### 插件接口（`web/routes/extension.py`、`application/member_handoff.py`）

`EXTENSION_API_TOKEN` 做 Bearer 鉴权，少于 24 位时 `/api/ext/*` 全部 404，不接受管理员会话。接口：`GET /ping`、`GET /workspaces`、`POST /resolve`（单团队 20 秒、总 60 秒）、`POST /handoff`、`POST /handoff/complete`。

## 数据存储

SQLite `data/team48.db`（WAL；需 SQLite ≥ 3.25）。表由 `bootstrap.py` 启动时自动建 / 补列，无单独迁移命令。

| 分组 | 主要表 |
|---|---|
| 身份 | `accounts`、`workspaces`、`workspace_memberships`、`workspace_official_member_snapshots`、`external_bindings` |
| 任务 | `operations`、`operation_steps` |
| 额度 | `quota_snapshots`、`quota_probe_states`、`quota_dispatch_lease`、`credential_leases` |
| Sub2API | `sub2api_sync_observations`、`sub2api_refresh_authorities`、`sub2api_refresh_handoffs`、`sub2api_account_status`、`sub2api_usage_snapshots`、`sub2api_proxy_bindings` |
| 其他 | `oauth_sessions`、`codex_bindings`、`system_settings`、`hme_alias_leases`、`phone_pool`、`phone_attempts`、`proxy_profiles`、`seat_vacancy_events` |

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
for f in app/web/static/js/*.js; do node --check "$f"; done
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
| `OPENAI_CA_BUNDLE` | OpenAI 请求自定义 CA |
| `IDENTITY_GMAIL_POLICY` | `owner_only`（默认）/ `warn` / `unrestricted` |
| `OFFICIAL_QUOTA_PROBE_ENABLED` / `AUTO_REAUTH_ENABLED` / `AUTO_ROTATE_ENABLED` / `FORCE_REFILL` | 自动化开关，默认全关，数据库设置优先 |
| `LOG_LEVEL` / `DATABASE_ECHO` / `TIMEZONE` | 日志、SQL 回显、时区 |

界面设置（存 `system_settings`）：Sub2API 地址与密钥、`sub2api_push_defaults`、HME 地址与服务 token（`hme_base_url` 默认 `http://icloud-hme:8081`）、Cloudflare 邮箱、`invite_seat_wire_standard/premium`、`official_quota_probe_batch_size`、自动轮转各项。
