# Part 0：桌面版授权链接、回调地址、浏览器环境

**状态**：已完成
**依赖**：无
**所属计划**：[README](./README.md)

## 目标

`create_oauth_authorize_url` 默认生成和 codex-rs 一样的"Codex 桌面版快捷登录"链接；回调地址统一成 `http://127.0.0.1:1455/auth/callback`（旧 `localhost` 回调仍能用）；服务器 Playwright 浏览器时区跟随代理出口、WebRTC 只走代理、提供抹掉 `navigator.webdriver` 的公共函数。做完后所有调用方不改代码就拿到新链接。

## 可改文件

- `app/integrations/openai/chatgpt.py`（只改 `create_oauth_authorize_url` 及其新增私有辅助函数）
- `app/integrations/openai/oauth_sessions.py`（`REDIRECT_URI`、本机监听脚本里的提示文字如需）
- `app/domain/automation/__init__.py`（`OAUTH_REDIRECT_URI`）
- `app/application/oauth_sessions.py`（回调主机校验）
- `app/core/config.py`
- `deploy.env.example`、`.env.example`
- `app/integrations/openai/browser/environment.py`
- `app/web/templates/console.html`、`app/web/static/js/app.js`（只改回调地址占位 / 提示文案）

## 只读参考

- rs 链接实现：`C:\Projects\VPS\天理云-美西国际大宽带-8H16G\codex-proxy-rs\backend\crates\providers\openai\src\credential\oauth.rs` 第 37-44 行常量、724-779 行（`derive_surface_stable_id`、`authorization_url`）；`token_client.rs:23-27`
- `app/integrations/proxy/geo.py:54`（`lookup_exit`，异步，返回 `{ip, country, timezone}`）
- `app/integrations/openai/browser/runner_profile.py:241-300`（Chromix 运行器的 WebRTC / 时区写法，可参照）
- `app/integrations/openai/browser/onboard.py:1218-1222`（现有 webdriver 补丁，Part 1 会改成调用本 Part 的公共函数）

## 要点

- **链接格式（照抄 rs）**：
  - 内层 `https://auth.openai.com/oauth/authorize?`，参数按 rs 顺序：`response_type=code`、`client_id`、`redirect_uri`、`code_challenge`、`code_challenge_method=S256`、`state`、`scope=openid profile email offline_access api.connectors.read api.connectors.invoke`、`id_token_add_organizations=true`、`codex_cli_simplified_flow=true`、`originator=Codex Desktop`、`codex_app_version=<配置>`、`source_surface_stable_id=<稳定ID>`、`codex_origin_stable_id=<同一稳定ID>`、`codex_streamlined_login=true`。**不带** `prompt`、`login_hint`、`hint`。
  - 外层 `https://chatgpt.com/codex/desktop-auth?authorize_url=<内层完整 URL>&codex_streamlined_login=true&no_universal_links=1`。返回字典里 `authorize_url` 就是外层链接；`code_verifier`、`state`、`client_id` 不变。
  - 稳定 ID：先得到 installation_id（UUID）——有邮箱（沿用 `login_hint` 参数传入，只用于计算、不进 URL）时取 `uuid.UUID(bytes=sha256("team48/codex-installation/v1\0" + 小写邮箱)[:16], version=4)`，没有邮箱时随机 `uuid4()`；再照 rs 的 `derive_surface_stable_id` 算：`sha256(b"openai/codex-desktop/surface-stable-id/v1\0" + installation_uuid.bytes)` 取前 16 字节，`bytes[6] = (bytes[6] & 0x0f) | 0x40`，`bytes[8] = (bytes[8] & 0x3f) | 0x80`，输出带连字符的 UUID 字符串。
- **入口开关**：函数新增参数 `entry: str | None = None`，取值 `"desktop"` / `"legacy"`；`None` 时读配置 `CODEX_OAUTH_ENTRY`（默认 `desktop`）。`legacy` 保持现在的旧链接逐字不变（含 `prompt=login`、`login_hint`、调用方传入的 scope），作为退路。`desktop` 时忽略调用方传入的 scope，固定用上面的 rs scope。
- **配置项**（`app/core/config.py` 的 `Settings`，并补到两个 env 示例）：
  - `CODEX_OAUTH_ENTRY`：`desktop`（默认）/ `legacy`，非法值按 `desktop` 处理并打一条 warning。
  - `CODEX_DESKTOP_APP_VERSION`：桌面版版本号。默认值取 rs 当前使用的值：在 codex-proxy-rs 源码里找 `profile.desktop_version`（`oauth.rs:610`）的来源；如果是运行时从上游 / 配置取的，就 `ssh tln` 只读查 rs 当前实际值（例如看 rs 生成的授权链接里的 `codex_app_version`），填为默认值，并在完成记录里写明来源。值为空或含空白、控制字符时报配置错误，不生成链接。
- **回调地址**：`oauth_sessions.REDIRECT_URI` 与 `domain/automation/__init__.py:57` 都改成 `http://127.0.0.1:1455/auth/callback`。换令牌的 `redirect_uri` 必须和授权链接里的逐字一致——各调用方已经用常量或会话里存的值，不用改。
- **兼容旧回调**：`app/application/oauth_sessions.py:29-62` 校验回调时，主机名 `localhost` 和 `127.0.0.1` 视为等价（端口、路径仍要求一致）。改版前已生成、仍在有效期内的旧会话（存的是 localhost）照常能完成。
- **浏览器环境**（`environment.py`，只影响走 `chromium_context_kwargs` 的 Playwright 流程，不动 `runner_profile.py`）：
  - 时区：`BROWSER_TIMEZONE` 显式配置时用配置值；否则每次启动按当前代理查出口时区（`geo.lookup_exit`；本函数是同步的且跑在子进程里，用 `asyncio.run` 调用，若已有运行中的事件循环则放到新线程里跑），同一进程内按代理 URL 缓存。查询失败抛 `BrowserEnvironmentError`，`error_code="proxy_geo_failed"`，不要退回容器的 Asia/Shanghai。Chromix 分支同样用出口时区覆盖档案里的时区（档案里的种子、语言、窗口照旧固定）。
  - WebRTC：Chromium 分支也加 `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`（Chromix 已有的保持）。
  - 新增公共函数 `harden_context(context)`：`context.add_init_script` 把 `navigator.webdriver` 改为 `undefined`（在 `Navigator.prototype` 上定义 getter），只对 Chromium 引擎生效；Chromix 不需要时直接返回。本 Part 只提供函数，调用由 Part 1 加。
  - `environment_summary` 里带上时区和来源（配置 / 出口），方便排障；不要写代理账密。
- **文案**：`console.html:701,715,729`、`app.js:2384,3278` 里示例回调地址改成 `127.0.0.1`；`app.js:3445` 已同时接受两种主机，保持。

## 步骤

1. 读 rs 两个文件，确认参数顺序和稳定 ID 算法；查出 `codex_app_version` 当前值。
2. 改 `config.py`、两个 env 示例。
3. 改 `create_oauth_authorize_url`（新参数 `entry`，desktop / legacy 两个分支）。
4. 改两个回调常量和 `application/oauth_sessions.py` 的主机等价判断。
5. 改 `environment.py`（时区、WebRTC、`harden_context`、summary）。
6. 改界面占位文案。
7. 用一小段临时脚本（放 `.tmp/`，用完删）打印一条 desktop 链接，和 rs 生成的链接逐个参数对比（state / challenge / 稳定 ID 不同是正常的），确认外层能被 `urllib.parse` 正确解出内层。
8. 跑基本检查。

## 完成标准

- [x] 默认生成的链接外层是 `chatgpt.com/codex/desktop-auth`，内层参数与 rs 一致、无 `prompt` / `login_hint`
- [x] `CODEX_OAUTH_ENTRY=legacy` 时链接与改动前完全一致
- [x] 回调常量为 `127.0.0.1`，旧 `localhost` 回调仍能通过会话校验
- [x] Chromium 启动参数含出口时区和 WebRTC 策略；`harden_context` 可被导入
- [x] 基本检查通过：`.venv/Scripts/python.exe -m compileall -q app scripts`、`.venv/Scripts/python.exe -c "import app.main"`、对 `app/web/static/js/*.js` 逐个 `node --check`

## 完成记录

- 链接：`chatgpt.py` 新增 `create_oauth_authorize_url(..., entry=None)`（desktop / legacy）、模块级 `is_desktop_authorize_url(url)`（给 Part 1 / 2 判断用）。稳定 ID 按邮箱派生，同邮箱每次相同。临时脚本核对：外层 / 内层参数与 rs 逐项一致；legacy 链接仍含 `prompt=login`、`login_hint`；`localhost` / `127.0.0.1` 回调互认，其他主机拒绝。
- **偏离计划（用户要求）**：rs 的桌面版版本号会随官方更新而变，不能只靠配置。新建 `app/integrations/openai/desktop_version.py`：从 rs `GET /api/admin/dashboard/summary` 的 `wireProfiles[provider=openai].release.latestVersion` 读取，存内存；`app/application/jobs/scheduler.py` 新增任务 `codex_desktop_version_sync`（启动即跑一次，之后每 30 分钟）。读不到时沿用上次值，一次都没读到才用 `CODEX_DESKTOP_APP_VERSION`（默认 `26.1117.41037`，2026-11-21 从 rs 查到）。这两个文件不在 Part 1 / 2 的范围内。
- 环境：时区优先 `BROWSER_TIMEZONE`，否则每次启动经代理查出口（不缓存：本地 SOCKS 桥端口每次不同，不能当缓存键）；查不到抛 `ProxyGeoError`（`error_code=proxy_geo_failed`）。没有代理时保持原行为。Chromix 档案的时区被出口时区覆盖，种子 / 语言 / 窗口不变。Chromium 也加了 WebRTC 只走代理。`harden_context(context, settings=None)` 只对 chromium 引擎生效。`environment_summary` 已含时区，没加"来源"字段（launch kwargs 不能带自定义键）。
- 上线要写进文档：ARCHITECTURE 配置表加 `CODEX_OAUTH_ENTRY`、`CODEX_DESKTOP_APP_VERSION`，定时任务表加 `codex_desktop_version_sync`（30 分钟）；`BROWSER_TIMEZONE` 为空时的含义改为"跟随代理出口"。

