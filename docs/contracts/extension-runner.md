# 扩展运行器协议（插件 ↔ 服务器）

轮转在 `ROTATION_SIGNUP_RUNNER=extension` 时，服务器以普通子进程启动 Chromix，加载同一份注册插件（`extensions/chatgpt-signup`），通过本文的 HTTP 接口给插件派任务、收状态、下发授权链接、收回调。本文是插件（`runner.mjs` / `background.js`）与服务端（`app/application/extension_runner.py`、`app/web/routes/runner.py`）共同遵守的唯一标准；字段、取值、时限以本文为准。

常量与错误码的代码位置：`app/application/extension_runner.py`；浏览器档案与启动参数：`app/integrations/openai/browser/runner_profile.py`；出口地理：`app/integrations/proxy/geo.py`。

## 1. 运行目录

服务端每次运行生成，运行结束（任何结果）立即删除；权限 700。

```
data/runner-runs/<run_id>/
├── extension/            插件源目录完整复制（排除本机 private-config.mjs）
│   ├── manifest.json     复制后改写 host_permissions（见下）
│   ├── private-config.mjs  服务端生成
│   ├── runner-config.json  服务端生成（仅供排障，插件不读）
│   └── ……其余源文件原样
└── browser.log           Chromix 标准输出/错误；结束时只取最后 50 行进诊断，去掉 URL 查询参数
```

- `run_id`：`secrets.token_hex(16)`，32 位小写十六进制，正则 `^[a-f0-9]{32}$`。
- `token`：`secrets.token_urlsafe(32)`（43 字符）。服务端内存只存 `sha256(token)`，比对用 `hmac.compare_digest`；运行结束即作废。
- 浏览器档案目录（`--user-data-dir`）不在运行目录里：正式运行用 `data/chrome-profiles/runner/<email 的 @ 换成 _at_>/`（保留，同一邮箱续接复用）；自检用 `data/chrome-profiles/runner-selfcheck/<run_id>/`（结束删除）。

### private-config.mjs（服务端生成）

```js
// Generated per run by Team48; deleted when the run ends.
export const MAILBOX = Object.freeze({"baseUrl": "<cf_mail_base_url>", "address": "<cf_mail_address>", "adminPassword": "<cf_mail_admin_password>"});
export const RUNNER = Object.freeze({"baseUrl": "http://127.0.0.1:8008", "runId": "<run_id>", "token": "<token>"});
```

- `MAILBOX` 三个键名与本机格式一致，值来自 `reauth.load_cf_config(db)`：`base_url` → `baseUrl`、`address` → `address`、`admin_password` → `adminPassword`。用 `json.dumps(..., ensure_ascii=True)` 生成。
- `RUNNER.baseUrl` = 配置 `RUNNER_LOCAL_BASE_URL` 去掉末尾 `/`。
- **不导出 `TEAM48`**：服务器模式不走 resolve / handoff。
- 插件必须用 `import * as PRIVATE_CONFIG` 读 `PRIVATE_CONFIG.RUNNER`，**不能**写 `import {RUNNER}`（本机文件没有这个导出，命名导入会让整个 Service Worker 加载失败）。
- 插件判定服务器模式的条件：`RUNNER.baseUrl` 协议为 `http:`、主机为 `127.0.0.1` 或 `localhost`；`runId` 匹配 `^[a-f0-9]{32}$`；`token` 为字符串且长度 ≥ 24。任一不满足即视为本机模式，行为与现在完全一致。

### runner-config.json（排障用）

`{"schema":1, "runId":..., "kind":"signup"|"selfcheck", "createdAt":ISO8601, "profileSummary":...}`。不含 token、邮箱、密码。

### manifest.json 改写（只改运行目录里的副本）

- `host_permissions` 追加：`RUNNER.baseUrl` 的 origin + `/*`（默认 `http://127.0.0.1:8008/*`）；`MAILBOX.baseUrl` 的 origin + `/*`（若源 manifest 里没有）。
- `kind:"selfcheck"` 时再追加 `<all_urls>`（`captureVisibleTab` 在无用户手势时需要；后台 `fetch('https://ipinfo.io/json')` 也靠它）。
- 其余字段不动：`version` 不改、`incognito` 保持 `split`、`permissions` 不增删。

## 2. 浏览器启动

- 命令：`[RUNNER_BROWSER_EXECUTABLE, *runner_profile.chromix_args(profile, user_data_dir=..., extension_dir=<运行目录>/extension, proxy_server=...), "about:blank"]`，`subprocess.Popen`，新进程组，环境变量 `DISPLAY=:99`、`TZ=<档案时区>`。
- Linux 上 `RUNNER_BROWSER_EXECUTABLE` 指向 Chromix 包自带的启动脚本 `chromix/chromix`（它设置 `FONTCONFIG_FILE` 加载包内字体后再 `exec chrome`），不要直接指向 `chrome`。
- 不传 `--remote-debugging-port`、`--enable-automation`、`--headless`，不用 Playwright / CDP。`chromix_args` 自身会拒绝这些参数。
- 代理：`socks5(h)` 带账号密码 → 起 `SocksAuthBridge`，`proxy_server="socks5://127.0.0.1:<bridge.port>"`；无认证 socks5 / http(s) 直接传；带账号密码的 http(s) 由 `check_runner_proxy` 在踢人前拒绝（`proxy_auth_unsupported`）。`proxy_server` 里出现账号密码时 `chromix_args` 抛 `proxy_auth_unsupported`。
- 回环地址（`localhost;127.0.0.1;[::1]`）走直连，插件访问 `RUNNER.baseUrl` 不经代理。
- 窗口：普通（非无痕）窗口。档案是该邮箱专用目录，首次即全新环境。

### 档案与指纹参数（`runner_profile.py`）

| 档案字段 | 取值 | 启动参数 |
|---|---|---|
| `seed` | 1..2^32-1 | `--fingerprint=<seed>` |
| `platform` | `linux` / `windows`（建档时取 `RUNNER_FINGERPRINT_PLATFORM`） | `--fingerprint-platform=`；windows 另加 `--fingerprint-windows-font-metrics` |
| `timezone` | 建档时经代理查到的 IANA 时区 | `--fingerprint-timezone=`，子进程 `TZ=` |
| `locale` | 固定 `en-US` | `--fingerprint-locale=en-US,en`、`--lang=en-US` |
| `screen` | 固定 1920×1080 | `--fingerprint-screen-width=1920`、`--fingerprint-screen-height=1080` |
| `window` | 1600×900 / 1680×960 / 1760×990 随机其一 | `--window-size=W,H`、`--window-position=0,0` |
| `hardware_concurrency` | 4 / 8 / 12 / 16 | `--fingerprint-hardware-concurrency=` |
| `device_memory` | 固定 8 | `--fingerprint-device-memory=8` |
| `gpu` | `"native"` 或 `{"vendor","renderer"}`（建档时 `RUNNER_GPU_MODE=preset` 才写预设） | native：`--uxr-gpu-backend=native`；预设：`--uxr-gpu-backend=compatibility --fingerprint-gpu-vendor=.. --fingerprint-gpu-renderer=..` |
| `exit_country` / `exit_ip_hash` | 建档时出口国家 / 出口 IP 的 sha256 前 12 位 | —（只用于摘要与自检比对） |

其余固定参数：`--force-webrtc-ip-handling-policy=disable_non_proxied_udp`、`--no-first-run`、`--no-default-browser-check`、`--password-store=basic`、`--disable-features=Translate,DisableLoadExtensionCommandLineSwitch`、`--disable-backgrounding-occluded-windows`、`--disable-renderer-backgrounding`、`--disable-background-timer-throttling`、`--silent-debugger-extension-api`、`--disable-dev-shm-usage`、`--no-sandbox`、`--load-extension=` / `--disable-extensions-except=`（运行目录插件）、`--proxy-server=`、`--proxy-bypass-list=`。

建档规则：先 `load_runner_profile(dir)`；为 `None` 时 `geo.lookup_exit(原始代理 URL)` 后 `create_runner_profile(dir, geo=..., platform=..., gpu_mode=...)`。查不到时区抛 `runner_geo_unknown`，不回退上海或 UTC；档案损坏或字段不合法抛 `runner_profile_invalid`，不重新随机。两者都是 `BrowserEnvironmentError` 子类，带 `error_code`。

## 3. 接口

公共约定：

- 前缀 `/api/ext/runner/{run_id}`；请求头 `Authorization: Bearer <RUNNER.token>`；插件 `fetch` 用 `credentials: 'omit'`、`cache: 'no-store'`，JSON 请求带 `Content-Type: application/json`，超时 15 秒（`AbortSignal.timeout(15000)`）。
- **所有请求只从后台 Service Worker 发**（content script 跨源请求会被 CORS 拦下）。
- 服务端只认本次运行的 token，不接受管理员会话，不依赖 `EXTENSION_API_TOKEN`；响应带 `Cache-Control: no-store`。
- `run_id` 未知、token 不符、运行已结束：一律 `404`，不说明原因。插件收到 404 视为运行已结束：停止当前任务（同 `stop`），不再发任何请求。
- 请求体过大 `413`，字段不合法 `422`；插件对 413/422 不重试同一请求体。网络错误或 `5xx`：按各接口的重试规则。
- 服务端不记录 token、密码、验证码、回调 URL、截图内容。

### GET `/job`

`signup`：

```json
{
  "kind": "signup",
  "email": "new@example.com",
  "password": "T48!....",
  "profile": {"name": "Emma Clark", "birthday": "1998-04-17"},
  "mode": "auto",
  "workspaceNames": ["团队显示名", "官方名"],
  "probeUrls": [],
  "phonePool": true
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `kind` | `"signup"` | |
| `email` | string | 小写，账号邮箱 |
| `password` | string | 账号已有密码（`password_encrypted`），为空时服务端生成并写回后再下发；插件必须用它注册，不自己生成 |
| `profile` | `{name: string ≤80, birthday: "YYYY-MM-DD"}` | `signup.signup_profile(<runner 档案目录>)` 固定生成，续接时不变；插件按 `checkedProfile` 规则校验 |
| `mode` | `"auto"` | 固定 |
| `workspaceNames` | string[] ≤5 | `member_handoff._workspace_names(workspace, owner_email)`；OAuth 选团队用 |
| `probeUrls` | `[]` | signup 为空数组 |
| `phonePool` | bool | 服务端是否允许本次运行接码（轮转打开号码池且有任务 ID 时为 `true`）；插件存进 `job.runner.phonePool` |

`selfcheck`：

```json
{"kind": "selfcheck", "probeUrls": [
  {"name": "browserscan", "url": "https://www.browserscan.net/"},
  {"name": "creepjs", "url": "https://abrahamjuliot.github.io/creepjs/"},
  {"name": "browserleaks_webrtc", "url": "https://browserleaks.com/webrtc"}
]}
```

`name` 满足 `^[a-z0-9_-]{1,40}$`，用作截图文件名。

插件重试：网络错误 / 5xx 最多重试 3 次，间隔 3 秒；仍失败则什么都不做（服务端会因心跳丢失结束运行）。

### POST `/event`

请求（≤ 64KB，含结束时的诊断）：

```json
{
  "seq": 12,
  "status": "running",
  "phase": "signup",
  "stage": "otp",
  "pauseReason": null,
  "pauseFinal": false,
  "message": "已收到验证码，正在逐位输入",
  "codexResult": "unknown",
  "diagnostics": null
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `seq` | int ≥ 1 | 本次运行内递增；服务端忽略不大于已收最大值的事件（仍正常回复指令） |
| `status` | `"running"` / `"paused"` / `"stopped"` / `"done"` | 插件 job 状态；自检也用这四个值 |
| `phase` | `"signup"` / `"oauth"` / `"selfcheck"` | |
| `stage` | `shared.mjs` `STAGES` 之一 | 含 `phone`（号码输入页）、`phone_otp`（短信验证码页），服务端分别映射为任务步骤 `add_phone`、`sms_otp`（注册和授权阶段都映射）；自检固定 `"unknown"` |
| `pauseReason` | `PAUSE_REASONS` 之一或 `null` | 仅 `status:"paused"` 时非空 |
| `pauseFinal` | bool | `true`：插件不会自己恢复（人工类暂停，或自动重试次数用完）；`false`：插件还会自动重试 / 页面前进后自动继续（`autoRetryAllowed(job)` 为真或原因属于 `AUTO_RESUME_REASONS`，且不在下表"人工类"里） |
| `message` | string ≤200 | 插件 job 的中文状态文字；不得含邮箱、密码、验证码、页面原文 |
| `codexResult` | `"unknown"` / `"no_phone"` / `"phone_required"` / `"failed"` | |
| `diagnostics` | object 或 `null` | 仅在 `status` 为 `done` / `stopped` / 最终暂停时附带 `diagnosticReport(job, VERSION)`；其他时候 `null` |

服务器模式的人工类暂停（`pauseFinal` 必为 `true`，插件原地停住不自动恢复）：`phone`、`phone_pool_empty`、`phone_limit`、`phone_back_missing`、`phone_relay_error`、`captcha`、`rate_limit`、`manual_step`、`email_mismatch`、`email_unverified`、`session_error`、`debugger_detached`；`unknown_page` 在自动重试次数用完后也为最终。

手机页：`phonePool:true` 时插件遇手机页先走接码中转（`POST /phone`），只有中转失败才以 `phone_pool_empty` / `phone_limit` / `phone_back_missing` / `phone_relay_error` 暂停；`phonePool:false` 时照旧以 `phone` 暂停。

发送时机：`status/phase/stage/pauseReason/codexResult` 任一变化立即发；无变化时每 30 秒心跳一次；注册完成等待授权期间每 5 秒一次。同一时刻只保留一个在途请求，后到的变化合并进下一次。

响应：

```json
{"ok": true, "command": "none"}
{"ok": true, "command": "authorize", "authorizeUrl": "https://auth.openai.com/oauth/authorize?..."}
{"ok": true, "command": "stop"}
```

- `authorize`：服务端从下发起每次都返回同一个 `authorizeUrl`，直到收到 `phase:"oauth"` 的事件。插件已在 OAuth 阶段时忽略重复的 `authorize`。插件只接受 origin 为 `https://auth.openai.com` 的链接，否则当作 `stop`。
- `stop`：一旦下发，之后每次都返回 `stop`。插件执行与弹窗"停止"相同的逻辑，上报一次 `status:"stopped"`（附诊断，失败不重试），然后不再请求。
- 网络错误 / 5xx：不单独重试，下一次心跳照常发。

### POST `/callback`

请求：`{"callbackUrl": "http://localhost:1455/auth/callback?code=...&state=..."}`，必须通过 `shared.mjs` `oauthCallback()` 校验（`localhost` 或 `127.0.0.1`、端口 1455、路径 `/auth/callback`、有 `state` 且有 `code` 或 `error`）。

响应：

| 响应 | 含义 | 插件动作 |
|---|---|---|
| `{"ok": true}` | 已收下（同一 URL 重复提交也回 `ok:true`）；换票由服务端异步完成，结果不回给插件 | 标 `done`，标签页转 `about:blank`（不转 `popup.html`），上报 `status:"done", phase:"oauth"` 并附诊断 |
| `{"ok": false, "error_code": "callback_invalid"}` | URL 不合格 | 不重试，上报 `status:"stopped"` |
| `{"ok": false, "error_code": "callback_already_received"}` | 已收到另一条回调 | 不重试，上报 `status:"stopped"` |
| `{"ok": false, "error_code": "not_authorizing"}` | 服务端尚未下发授权链接 | 不重试，上报 `status:"stopped"` |

网络错误 / 5xx：每 5 秒重试，最多 12 次；仍失败上报 `status:"stopped"`。服务器模式下 `captureCallback` 不调用 `/api/ext/handoff/complete`。

### POST `/phone`（仅 signup）

接码中转：领号、读短信验证码、报告结果、释放。见 [phone-relay.md](phone-relay.md)。

### POST `/probe`（仅自检）

请求：`{"name": "...", "kind": "signals" | "exit" | "screenshot", "data": ...}`

| kind | name | data | 上限 |
|---|---|---|---|
| `signals` | `"signals"` | 见下方对象 | JSON 序列化后 64KB |
| `exit` | `"exit"` | `{"status": HTTP 状态码, "body": ipinfo 返回的 JSON 对象}`，失败时 `{"error": "简短原因"}` | 64KB |
| `screenshot` | `probeUrls[i].name` | `"data:image/png;base64,..."` | 字符串 4MB |

`signals` 对象（content script 在 `https://chatgpt.com/` 顶层页面采集；取不到的字段给 `null`）：

```json
{
  "userAgent": "Mozilla/5.0 ...",
  "platform": "Linux x86_64",
  "uaData": {"platform": "Linux", "platformVersion": "", "architecture": "x86", "bitness": "64", "mobile": false,
             "brands": [{"brand": "Google Chrome", "version": "153"}]},
  "timezone": "America/New_York",
  "utcOffsetMinutes": -240,
  "language": "en-US",
  "languages": ["en-US", "en"],
  "screen": {"width": 1920, "height": 1080, "availWidth": 1920, "availHeight": 1080, "colorDepth": 24, "devicePixelRatio": 1},
  "window": {"outerWidth": 1600, "outerHeight": 900, "innerWidth": 1600, "innerHeight": 789},
  "hardwareConcurrency": 8,
  "deviceMemory": 8,
  "webdriver": false,
  "webgl": {"vendor": "WebKit", "renderer": "WebKit WebGL", "unmaskedVendor": "Google Inc. (Intel)", "unmaskedRenderer": "ANGLE (...)"},
  "webrtcIps": ["203.0.113.5"]
}
```

- `timezone` = `Intl.DateTimeFormat().resolvedOptions().timeZone`；`utcOffsetMinutes` = `-new Date().getTimezoneOffset()`。
- `uaData` 来自 `navigator.userAgentData.getHighEntropyValues(['platform','platformVersion','architecture','bitness'])` 加 `brands`、`mobile`。
- `webgl.unmasked*` 取 `WEBGL_debug_renderer_info`。
- `webrtcIps`：`new RTCPeerConnection({iceServers: [{urls: 'stun:stun.l.google.com:19302'}]})` 建数据通道并 `createOffer`，收集 5 秒候选，只上报候选里的 IP 字符串（去重，不含 `.local` mDNS 主机名）。

插件重试：网络错误 / 5xx 每 3 秒重试，最多 3 次；失败跳过该项继续下一步。响应恒为 `{"ok": true}`（或 404 / 413 / 422）。

## 4. 生命周期

### 公共

1. 插件 `chrome.runtime.onStartup` 与 `onInstalled` 各触发一次 `boot()`，另设 alarm 兜底（Service Worker 被回收后恢复轮询 / 心跳）。发现合法 `RUNNER` 配置 → `GET /job`。
2. 使用浏览器已有的第一个普通窗口里的标签页（启动参数给了 `about:blank`）；没有就 `chrome.tabs.create`。服务器模式跳过所有"必须无痕"检查（含只允许无痕 worker 执行的判断）。
3. 服务端每收到一个事件就记下 `last_event_at`；超过 120 秒没有任何事件 → `runner_heartbeat_lost`；浏览器进程退出 → `runner_exited`；总时长超过 `RUNNER_TIMEOUT_SECONDS` → `runner_timeout`。

### signup

1. 插件按现有 `auto` 模式注册：`hidePanel:true`、不走 Team48 接入、密码与资料用任务里给的值。插件自身 `RUN_TTL` 超时仍然生效（超时 → `stopped`）。
2. 注册完成（`finishSignup`）→ 上报 `status:"done", phase:"signup"`，之后每 5 秒 `POST /event`，等待指令。
3. 服务端收到 `phase:"signup", status:"done"` → 核对官方成员（最多 8 次、间隔 5 秒；角色 `official_roles_equivalent`、席位 `existing_invite_seat_error`）：
   - 确认入组：`ensure_membership(... joined)`，生成授权链接（`purpose="account_reauth"`），此后事件回复 `command:"authorize"`。
   - 未入组 → `not_joined`；角色 / 席位不符 → `membership_mismatch`；两者都下发 `stop`。
4. 插件收到 `authorize` → 同一标签页打开链接，`phase:"oauth"`，按现有 OAuth 逻辑登录（邮箱验证码仍走 `MAILBOX`）、按 `workspaceNames` 选团队、同意。
5. 拦截到 `localhost:1455/auth/callback` → `POST /callback` → 上报 `status:"done", phase:"oauth"`，插件任务结束（之后仍可收到 `stop`，也可以不再请求）。
6. 服务端换票（`begin_exchange` → revision 核对 → `exchange_oauth_code` → token 邮箱核对 → `apply_tokens`、`auth_state="healthy"`），结束运行：给插件下发 `stop`、关浏览器、删运行目录。

### 暂停

- `status:"paused", pauseFinal:true` → 服务端立即结束运行，错误码见第 5 节。
- `status:"paused", pauseFinal:false` → 服务端继续等；同一次暂停超过 180 秒（`NONFINAL_PAUSE_LIMIT`）仍未恢复运行，也按最终暂停处理。
- `status:"stopped"`（非服务端下发的 stop） → `runner_exited`。

### selfcheck

插件依次：① 打开 `https://chatgpt.com/`，页面加载后 background 给 content script 发 `selfcheck-signals` 消息，content script 采集并回传，background `POST /probe`（`signals`）；② background `fetch('https://ipinfo.io/json')`（走浏览器代理）→ `POST /probe`（`exit`）；③ 依次打开 `probeUrls`，每个等 25 秒后 `chrome.tabs.captureVisibleTab(windowId, {format: 'png'})` → `POST /probe`（`screenshot`）；④ 上报 `status:"done", phase:"selfcheck"`。期间照常每 30 秒心跳（`status:"running", phase:"selfcheck"`）。自检不附加 `chrome.debugger`、不登录任何账号。服务端等 `done` 或 240 秒（`SELFCHECK_TIMEOUT`）。

## 5. 服务端错误码

`RunnerOutcome.error_code` 与 `BrowserEnvironmentError.error_code` 的取值（常量在 `extension_runner.py`）：

| 错误码 | 何时 | 轮转结果 |
|---|---|---|
| `runner_not_configured` | 未配 Chromix 路径、插件目录不完整、`RUNNER_*` 配置不合法 | 预检拦下（踢人前） |
| `runner_executable_missing` | `RUNNER_BROWSER_EXECUTABLE` 指向的文件不存在 | 预检拦下 |
| `proxy_missing` | 母号代理为空或格式无效 | 预检拦下 |
| `proxy_auth_unsupported` | 带账号密码的 http(s) 代理 | 预检拦下 |
| `runner_geo_unknown` | 首次建档查不到出口时区 | partial（可继续） |
| `runner_profile_invalid` | 档案损坏 / 缺失但目录已有浏览器数据 | partial，需人工恢复档案 |
| `runner_launch_failed` | 子进程起不来、运行目录生成失败 | partial |
| `runner_exited` | 浏览器进程退出、插件自行停止 | partial |
| `runner_heartbeat_lost` | 120 秒无事件 | partial |
| `runner_timeout` | 超过 `RUNNER_TIMEOUT_SECONDS` | partial |
| `phone_verification_required` | 插件暂停原因 `phone` | manual_required |
| `captcha_required` | 插件暂停原因 `captcha` | manual_required |
| `runner_paused_<pauseReason>` | 其他最终暂停，如 `runner_paused_rate_limit`、`runner_paused_email_mismatch`、`runner_paused_phone_limit` | 含 `MANUAL_MARKERS` 子串（如 `mismatch`、`phone`）的为 manual_required，其余 partial |
| `not_joined` | 注册完成但官方列表未确认入组 | partial |
| `membership_mismatch` | 已入组但角色 / 席位不符 | partial |
| `oauth_exchange_failed` | 换票失败、授权会话过期或 revision 不符 | partial |
| `token_identity_mismatch` | token 邮箱与账号不符 | partial |

轮转判定"待人工"沿用 `manual_rotation.MANUAL_MARKERS`（`phone`、`sms`、`challenge`、`captcha`、`turnstile`、`mismatch`、`conflict` 子串匹配）。所有 partial / manual_required 都保留同一邮箱，可"继续轮转"。

## 6. 服务端函数（路由调用）

`app/web/routes/runner.py` 只通过以下函数读写运行登记；返回 `None` 表示 404：

```python
runner_job(run_id, token) -> dict | None
runner_event(run_id, token, event: dict) -> dict | None
runner_callback(run_id, token, callback_url: str) -> dict | None
runner_probe(run_id, token, *, name: str, kind: str, data) -> dict | None
runner_phone_context(run_id, token) -> dict | None
```

`runner_phone_context`：`kind != "signup"` 或本次未开放接码时返回 `{"enabled": False}`；否则返回 `{"enabled": True, "lease_key": <任务 public_id>, "account_id", "proxy_url", "phase"}`，并刷新 `last_event_at`。路由据此构造 `phone_relay.RelayContext`。

大小上限常量：`EVENT_MAX_BYTES`（64KB）、`PROBE_JSON_MAX_BYTES`（64KB）、`PROBE_SCREENSHOT_MAX_BYTES`（4MB）。
