# Part 0：协议、配置与运行器档案

**状态**：待开始
**依赖**：无
**所属计划**：[README](./README.md)

## 目标

定下插件与服务器之间的协议和所有公共部分，让 part-1（插件）、part-2（服务端运行器）、part-3（轮转接入）能同时开工且互不改同一文件。

## 可改文件

- `docs/contracts/extension-runner.md`（新建，协议唯一来源）
- `app/core/config.py`
- `.env.example`、`deploy.env.example`
- `app/integrations/openai/browser/runner_profile.py`（新建：运行器档案与 Chromix 启动参数）
- `app/integrations/proxy/geo.py`（新建：经代理查出口 IP 与时区）
- `app/application/extension_runner.py`（新建：只放对外函数签名、数据类和错误码常量，函数体 `raise NotImplementedError`，由 part-2 填实现）

## 只读参考

- `app/integrations/openai/browser/environment.py`（现有 Chromix 档案做法：原子发布、损坏即停）
- `app/integrations/proxy/socks_bridge.py`（`SocksAuthBridge`、`needs_socks_auth_bridge`、`chrome_proxy_launch`）
- `app/integrations/sms/client.py` `chrome_proxy_config`
- `extensions/chatgpt-signup/background.js`、`shared.mjs`（现有 job 字段、暂停原因码 `shared.mjs:94-98`）
- Chromix 参数表：https://github.com/lwhx/Chromix/blob/main/docs/fingerprint-flags.md

## 要点

### 1. 配置项（`app/core/config.py`，同步写进两个 env 示例并加注释）

| 变量 | 默认 | 说明 |
|---|---|---|
| `ROTATION_SIGNUP_RUNNER` | `playwright` | `playwright`（旧路径）/ `extension`（本计划新路径），只影响手动 / 自动轮转 |
| `RUNNER_BROWSER_EXECUTABLE` | `""` | Chromix 153 可执行文件路径；`extension` 模式下必填 |
| `RUNNER_FINGERPRINT_PLATFORM` | `linux` | `linux` / `windows`，只影响**新建**档案 |
| `RUNNER_GPU_MODE` | `native` | `native`（不改显卡）/ `preset`（按平台用预设显卡字符串，compatibility 后端），只影响新建档案 |
| `RUNNER_EXTENSION_DIR` | `extensions/chatgpt-signup`（相对项目根） | 插件源目录 |
| `RUNNER_LOCAL_BASE_URL` | `http://127.0.0.1:8008` | 插件回连服务器的地址（容器内） |
| `RUNNER_TIMEOUT_SECONDS` | `1500` | 一次运行服务端总等待上限 |

### 2. 运行器档案（`runner_profile.py`）

- 目录：`data/chrome-profiles/runner/<email 的 @ 换成 _at_>/`，档案文件 `.team48-runner.json`，与旧 `.team48-browser.json` 完全分开，不改 `environment.py`。
- 档案字段（schema 1）：`seed`（1..2^32-1）、`platform`（linux/windows）、`gpu`（`native` 或 `{"vendor":..,"renderer":..}`）、`locale`（固定 `en-US`）、`timezone`（IANA）、`screen`（固定 `{"width":1920,"height":1080}`）、`window`（从 `(1600,900)`、`(1680,960)`、`(1760,990)` 中随机选一个）、`hardware_concurrency`（从 4/8/12/16 选）、`device_memory`（8 或 16）、`exit_country`、`exit_ip_hash`（出口 IP 的 sha256 前 12 位，不存明文）。
- 首次建档：必须先传入 `geo` 查到的时区；查不到时建档失败（`runner_geo_unknown`），**不回退上海或 UTC**。
- 并发首建沿用 `environment.py` 的临时文件 + `os.link` 原子发布；档案损坏或字段不合法时报错停止，不重新随机。
- Windows 预设显卡在两三组常见组合中按种子挑一个，例如 `Google Inc. (Intel)` / `ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)`；Linux 预设用 `Intel` / `Mesa Intel(R) UHD Graphics 630 (CFL GT2)`。只在 `RUNNER_GPU_MODE=preset` 时写入。
- 提供 `chromix_args(profile, *, user_data_dir, extension_dir, proxy_server, bypass) -> list[str]`，返回完整命令行参数（不含可执行文件）：
  - `--user-data-dir=`、`--load-extension=`、`--disable-extensions-except=`（插件目录）、`--proxy-server=`、`--proxy-bypass-list=<-loopback>;localhost;127.0.0.1`
  - `--fingerprint=<seed>`、`--fingerprint-platform=`、`--fingerprint-timezone=`、`--fingerprint-locale=en-US`、`--lang=en-US`、`--fingerprint-hardware-concurrency=`、`--fingerprint-device-memory=`、`--fingerprint-screen-width=1920`、`--fingerprint-screen-height=1080`
  - GPU 预设时：`--fingerprint-gpu-backend=compatibility --fingerprint-gpu-vendor=.. --fingerprint-gpu-renderer=..`
  - `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`、`--window-size=W,H`、`--window-position=0,0`
  - `--no-first-run --no-default-browser-check --password-store=basic --disable-features=Translate,DisableLoadExtensionCommandLineSwitch`（后者保证新版内核仍接受 `--load-extension`）
  - `--disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-background-timer-throttling --silent-debugger-extension-api`
  - `--no-sandbox`（容器 root 运行必需）
  - **禁止**：`--remote-debugging-port`、`--enable-automation`、`--headless`、任何 Playwright 参数
- 提供 `profile_summary(profile) -> str`：引擎、平台、时区、窗口、出口国家、档案哈希前 12 位；不含种子原值、邮箱、路径。

### 3. 出口地理（`geo.py`）

- `lookup_exit(proxy_url) -> {"ip","country","timezone"}`：用 `httpx` 经该代理请求，依次尝试 `https://ipinfo.io/json`、`https://ipapi.co/json/`，超时 10 秒；返回 IANA 时区并用 `zoneinfo` 校验。全部失败抛 `GeoLookupError`。
- `socks5h://` 需要 `socksio`（已在依赖中）。不打印代理凭据。

### 4. 协议（`docs/contracts/extension-runner.md`，写成 part-1 和 part-2 共同遵守的唯一标准）

**运行目录**（服务端每次运行生成，结束即删）：`data/runner-runs/<run_id>/extension/` = 插件源文件完整复制 + 生成的 `private-config.mjs` + `runner-config.json`。

- `private-config.mjs`：沿用现有格式导出 `MAILBOX`（值来自后台 `cf_mail_*` 设置）；另导出 `export const RUNNER = Object.freeze({baseUrl, runId, token})`；不导出 `TEAM48`（服务器模式不走 resolve/handoff）。
- `token`：每次运行 `secrets.token_urlsafe(32)`，服务端只存 sha256；运行结束立即作废。
- `manifest.json` 在复制后由服务端改写：`host_permissions` 追加 `RUNNER_LOCAL_BASE_URL` 对应的 `http://127.0.0.1:8008/*`（自检时再加探针域名，见下）；`incognito` 保持 `split`。源码中的 manifest 不改。

**接口**（全部 `Authorization: Bearer <RUNNER.token>`，`credentials: 'omit'`；token 不匹配或运行已结束一律 404）：

| 接口 | 请求 | 响应 |
|---|---|---|
| `GET /api/ext/runner/{run_id}/job` | — | `{kind:"signup"\|"selfcheck", email, password, profile:{name,birthday}, mode:"auto", workspaceNames:[..], probeUrls:[..]}`（selfcheck 只有 `kind`、`probeUrls`） |
| `POST /api/ext/runner/{run_id}/event` | `{status, stage, phase, pauseReason, message, codexResult, diagnostics?}`；状态变化或每 30 秒心跳各发一次 | `{ok:true, command:"none"\|"authorize"\|"stop", authorizeUrl?}` |
| `POST /api/ext/runner/{run_id}/callback` | `{callbackUrl}` | `{ok:true}` 或 `{ok:false,error_code}` |
| `POST /api/ext/runner/{run_id}/probe` | `{name, kind:"signals"\|"exit"\|"screenshot", data}`；`signals`/`exit` 的 `data` 是 JSON（≤64KB），`screenshot` 的 `data` 是 PNG dataURL（≤4MB） | `{ok:true}` |

**服务器模式的窗口**：浏览器以普通（非无痕）窗口启动，档案本身是该邮箱专用的全新目录，等同于干净环境。插件在服务器模式下跳过所有"必须无痕"的检查，复用启动时已有的第一个普通窗口里的标签页。

**自检（`kind:"selfcheck"`）**：
- 本次运行由服务端改写的 manifest 额外加入 `<all_urls>` host 权限（`captureVisibleTab` 无用户手势时需要；只加在这次运行的副本里）。
- 插件依次：① 打开 `https://chatgpt.com/`，由 content script 采集本地信号并上报 `signals`：UA、`navigator.platform`、UA-CH（`userAgentData.getHighEntropyValues` 的 platform/platformVersion/brands）、时区与 UTC 偏移、`languages`、`screen`、`outerWidth/innerWidth`、`hardwareConcurrency`、`deviceMemory`、`webdriver`、WebGL vendor/renderer、WebRTC 候选 IP 列表（用 `RTCPeerConnection` + 公共 STUN 收集 5 秒）；② 后台 `fetch('https://ipinfo.io/json')` 上报 `exit`；③ 依次打开 `probeUrls`，每个等 25 秒后用 `chrome.tabs.captureVisibleTab` 截图上报 `screenshot`；④ 上报 `status:"done"`。
- 自检不附加 `chrome.debugger`，不登录任何账号。

**生命周期**：
1. 插件启动（`onStartup` / `onInstalled`）发现 `RUNNER` 配置 → `GET job`。
2. `signup`：插件在自己打开的窗口里按 `auto` 模式注册；完成（`finishSignup`）后上报 `status:"done", phase:"signup"`，然后每 5 秒 `event`，等待服务端返回 `command:"authorize"`。
3. 服务端确认官方已入组后，下一次 `event` 回复 `command:"authorize", authorizeUrl`；插件在**同一标签页**打开授权链接，按现有 OAuth 阶段逻辑登录、选团队（`workspaceNames`）、同意。
4. 拦截到 `localhost:1455/auth/callback` → `POST callback`；插件任务结束，上报 `status:"done", phase:"oauth"`。
5. 任何暂停（`phone`、`captcha`、`rate_limit` 等）都上报 `status:"paused"` 和 `pauseReason`；服务端据此结束运行。插件在服务器模式下**不**自动重试人工类暂停。
6. `command:"stop"`：插件停止任务并不再请求。

**服务端错误码**（写在 `extension_runner.py` 常量里，part-2/3 共用）：`runner_not_configured`、`runner_executable_missing`、`proxy_auth_unsupported`、`runner_geo_unknown`、`runner_launch_failed`、`runner_exited`、`runner_heartbeat_lost`、`runner_timeout`、`runner_paused_<pauseReason>`（其中 `phone` → `phone_verification_required`、`captcha` → `captcha_required`，沿用轮转 `MANUAL_MARKERS` 能识别的字样）、`not_joined`、`membership_mismatch`、`oauth_exchange_failed`、`token_identity_mismatch`。

### 5. `extension_runner.py` 桩（签名定死，part-3 按此调用）

```python
@dataclass
class RunnerOutcome:
    ok: bool
    error_code: str = ""
    error: str = ""
    joined: bool = False          # 官方已确认入组
    authorized: bool = False      # 已换票并写入凭据
    diagnostics: dict | None = None

def runner_enabled() -> bool: ...               # 读 ROTATION_SIGNUP_RUNNER == "extension"
def validate_runner_configuration() -> None: ... # 可执行文件存在、插件目录完整；失败抛 BrowserEnvironmentError（沿用其 error_code 字段）
async def check_runner_proxy(proxy_url: str) -> None: ...  # 带认证的 http(s) 代理抛 BrowserEnvironmentError(proxy_auth_unsupported)
async def signup_and_authorize(db, *, account, workspace, role: str, seat_intent: str,
                               proxy_url: str, job_id: str, on_stage=None) -> RunnerOutcome: ...
async def run_selfcheck(db, *, proxy_url: str, platform: str | None, job_id: str) -> dict: ...
```

`signup_and_authorize` 的约定：调用前邀请已发出且官方已确认邀请；调用方已持有全局浏览器槽；函数内部完成"启动浏览器 → 注册 → 确认入组 → 发授权链接 → 换票 → `apply_tokens` + `auth_state=healthy` + 写 joined 成员关系"，任何情况下返回前都关闭浏览器、删除运行目录。

## 步骤

1. 写 `docs/contracts/extension-runner.md`（上面第 4 节全文 + 字段类型）。
2. 加配置项与 env 示例。
3. 写 `runner_profile.py`、`geo.py`。
4. 写 `extension_runner.py` 桩（数据类、常量、签名、`runner_enabled`、`validate_runner_configuration`、`check_runner_proxy` 可直接实现；两个 async 主函数留 `NotImplementedError`）。
5. 跑基本检查。

## 完成标准

- [ ] 协议文档完整，part-1 / part-2 不需要再猜字段
- [ ] `runner_profile.chromix_args` 输出不含 `--remote-debugging-port`、`--enable-automation`
- [ ] `ROTATION_SIGNUP_RUNNER` 默认 `playwright`，不设任何新变量时应用行为不变
- [ ] 基本检查通过：`.venv/Scripts/python.exe -m compileall -q app scripts` 与 `.venv/Scripts/python.exe -c "import app.main"`

## 完成记录
