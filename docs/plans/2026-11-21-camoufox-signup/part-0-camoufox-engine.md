# Part 0：Camoufox 浏览器引擎、镜像预装与持久指纹环境

**状态**：已完成
**依赖**：无
**所属计划**：[README](./README.md)

## 目标

为服务器 Playwright 浏览器引入可选的 `BROWSER_ENGINE=camoufox` 引擎（固定 `camoufox[geoip]==0.5.9`）：Docker 镜像构建时离线预置 Camoufox 内核与 GeoIP 库；`environment.py` 提供统一的浏览器上下文启动器 `launch_browser_context`，按邮箱独立保存并复用固定桌面指纹，每次启动按代理出口 IP 自动对齐时区、地理位置、WebRTC 与回环代理绕过；本机与容器内均可通过 `scripts/browser_environment_smoke.py --engine camoufox` 离线冒烟验证。

## 可改文件

- `requirements.txt`
- `deploy/Dockerfile`
- `app/core/config.py`
- `.env.example`
- `deploy.env.example`
- `app/integrations/openai/browser/environment.py`
- `scripts/browser_environment_smoke.py`

## 只读参考

- `C:\Projects\Github_Other_Projects\HixBrowser\engines\camoufox-0.5.9\requirements.in`、`shim.py`、`C:\Projects\Github_Other_Projects\HixBrowser\engines\common\hix_adapter.py`（参考 `camoufox[geoip]==0.5.9` 用法、`.mmdb` 保鲜刷新避免运行时联网下载、IPv6 下不开 `webrtc:ipv6`、屏幕上限避免被显示器压扁、`locale` 固定 `en-US`）
- `app/integrations/proxy/geo.py`（`lookup_exit` 返回 `{"ip", "country", "timezone"}`）
- `app/integrations/proxy/socks_bridge.py`（`LOOPBACK_BYPASS` 与本地 SOCKS5 桥行为）
- `app/integrations/openai/browser/onboard.py`、`app/integrations/openai/browser/reauth.py`（查看现有 `chromium_context_kwargs` / `launch_persistent_context` 调用方式，Part 1 会改为调用本 Part 的 `launch_browser_context`）

## 要点

- **版本与镜像预装（离线运行硬要求）**：
  - `requirements.txt` 新增 `camoufox[geoip]==0.5.9`。
  - `deploy/Dockerfile`：
    - `ENV` 增加 `XDG_CACHE_HOME=/ms-playwright/cache`（Linux 下 `platformdirs` 会把 Camoufox 内核与 GeoIP 库放在 `/ms-playwright/cache/camoufox`，路径固定不随运行用户变化）。
    - `apt-get` 补齐 Firefox / Camoufox 在 Debian bookworm 所需的动态库（如 `libgtk-3-0`、`libdbus-glib-1-2`、`libxt6`、`libx11-xcb1`、`libpci3`、`libegl1`）。
    - 在构建阶段执行 `python -m camoufox fetch` 预下载浏览器内核与 GeoIP `.mmdb` 库（必要时顺带触发 `from camoufox.fpgen_model import load_fpgen; load_fpgen()` 确保模型文件也在镜像内）。
    - 运行时**严禁联网下载**：启动前刷新已有 `.mmdb` 文件的修改时间（参照 HixBrowser 的 `mark_geo_db_fresh`），让 Camoufox 认为地理库新鲜、不发起在线更新；内核或地理库缺失时直接抛 `BrowserEnvironmentError`（`error_code="camoufox_not_installed"`）。
- **配置项**（`app/core/config.py`、`.env.example`、`deploy.env.example`）：
  - `browser_engine: Literal["chromium", "chromix", "camoufox"] = "chromium"`（代码默认保持 `chromium`，方便 `.env` 一键回退）。
  - 新增注册后主页停留时长配置：`signup_oauth_dwell_min_sec: int = 15`、`signup_oauth_dwell_max_sec: int = 30`（供 Part 1 使用；负数按 0 处理，`max < min` 时取 `min`）。
- **出口位置查询**（`environment.py`）：
  - 新增 `exit_location(proxy_config: dict[str, str]) -> dict[str, str]`：调用 `geo.lookup_exit(_proxy_url(proxy_config))`（同步上下文下沿用现有 `asyncio.run` / 线程池写法），校验时区合法后返回 `{"ip", "country", "timezone"}`，失败抛 `ProxyGeoError`（`error_code="proxy_geo_failed"`）。原 `exit_timezone(proxy_config)` 保留并改为调用 `exit_location(proxy_config)["timezone"]`，兼容已有调用。
- **按邮箱固定 Camoufox 档案与指纹**：
  - 档案目录：`directory.parent / "camoufox" / directory.name`（即 `data/chrome-profiles/camoufox/<email_dir>/`），不碰原 Chromium / Chromix 目录。
  - 持久指纹清单：`<camoufox_dir>/.team48-camoufox.json`。注意 `Camoufox(persistent_context=True)` 若不显式传 `fingerprint`，每次重启都会重新随机生成指纹！因此必须：
    - 首次创建时：若 `<camoufox_dir>` 内已有 Firefox 数据文件（如 `prefs.js`、`places.sqlite`、`cookies.sqlite`）却没有 `.team48-camoufox.json`，抛 `BrowserEnvironmentError`（不静默换指纹）；否则调用 `camoufox.fingerprints.generate_fingerprint(os="windows", screen=Screen(max_width=1920, max_height=1080))` 生成一份桌面指纹，连同 schema 版本原子写入 `.team48-camoufox.json`。
    - 后续启动时：从 `.team48-camoufox.json` 读回并还原为 `browserforge.fingerprints.Fingerprint` 对象传入 `Camoufox(..., fingerprint=saved_fp)`；档案损坏或格式不对抛 `BrowserEnvironmentError`。
- **Camoufox 启动参数细节**：
  - 固定 `os="windows"`、`persistent_context=True`、`user_data_dir=str(camoufox_dir)`、`humanize=True`、`locale=settings.browser_locale`（默认 `"en-US"`，不要让 `geoip` 改成小语种）、`i_know_what_im_doing=True`、`headless=bool(settings.browser_headless)`（生产 `BROWSER_HEADLESS=False`，跑在现有 Xvfb `:99` `1920x1080` 上）。若安装了默认 uBlock 插件影响加载或联网，参照 HixBrowser 传 `exclude_addons=[DefaultAddons.UBO]`。
  - 地区与 WebRTC：
    - 若显式配置了 `settings.browser_timezone`（如 `.env` 临时固定时区或离线冒烟脚本）：不调 `exit_location`，传 `geoip=False`、`block_webrtc=True`、`config={"timezone": settings.browser_timezone, "showcursor": False}`。
    - 否则有代理时调 `loc = exit_location(proxy_config)`：当 `loc["ip"]` 是 IPv4 且本地 MMDB 能查到（先 `mark_geo_db_fresh` 再试 `camoufox.geolocation.get_geolocation(loc["ip"])`）时，传 `geoip=loc["ip"]`（直接传 IP 字符串走离线 MMDB，自动设时区、经纬度和 `webrtc:ipv4` 并关 IPv6 DNS）及 `config={"showcursor": False}`；当出口是 IPv6（Camoufox 的 `webrtc:ipv6` 会破坏 SDP）或 MMDB 报 `UnknownIPLocation` 时，降级为 `geoip=False`、`block_webrtc=True`、`config={"timezone": loc["timezone"], "showcursor": False}`。
  - 回环代理绕过（保证 `http://127.0.0.1:1455/auth/callback` 能被 `route` 拦住）：传 `proxy=dict(proxy_config)`，并加 `firefox_user_prefs={"network.proxy.no_proxies_on": "localhost, 127.0.0.1, ::1", "network.proxy.allow_hijacking_localhost": False}`。
- **统一启动上下文管理器**（`environment.py`，供 Part 1 的 `onboard.py` 与 `reauth.py` 复用）：
  - 新增 `@contextmanager def launch_browser_context(profile_dir: Path | str, proxy_config: dict[str, str], *, settings: Settings | None = None, executable_path: str = "")`：
    - 非 headless 时自动确保虚拟显示器就绪（调用 `onboard.ensure_virtual_display()` 或把显示器检查放在公共位置）。
    - `chromium` / `chromix` 分支：走 `sync_playwright()` + `pw.chromium.launch_persistent_context(**launch_kwargs)` + `harden_context`，产出 `(context, page, actual_profile_dir, summary)`。
    - `camoufox` 分支：走 `from camoufox.sync_api import Camoufox` 的 `with Camoufox(...) as context:`，取 `page = context.pages[0] if context.pages else context.new_page()`，产出 `(context, page, camoufox_dir, summary)`；`summary` 形如 `"Camoufox env=<12位摘要> screen=<WxH> locale=en-US timezone=<tz>"`（不含邮箱、路径、代理账密或原始种子）。
  - 保留原有 `validate_configuration`、`context_options`、`harden_context`、`environment_summary` 兼容现有调用；`validate_configuration` 支持 `"camoufox"`。
- **冒烟脚本**（`scripts/browser_environment_smoke.py`）：
  - `--engine` 增加 `"camoufox"` 选项；改用或兼容 `launch_browser_context`。
  - 验证离线状态下（代理指向不可达本机端口 `127.0.0.1:9`、显式 `browser_timezone="America/Los_Angeles"`）：Camoufox 能启动、本地 DOM 点击成功、`navigator.language` 与 `Intl` 时区符合预期、同一 profile 连续启动两次观测结果完全一致（`observations[0] == observations[1]`）、两个独立 profile 指纹不相同（`observations[0]["observed"] != observations[2]["observed"]`）。

## 步骤

1. 阅读 HixBrowser 的 `shim.py` / `hix_adapter.py` 中与 `generate_fingerprint`、`DefaultAddons`、MMDB 保鲜、WebRTC 相关的调用细节。
2. 更新 `requirements.txt` 并在本机 `.venv` 安装 `camoufox[geoip]==0.5.9`，执行 `.venv/Scripts/python.exe -m camoufox fetch` 拉取本机开发验证用的内核与地理库。
3. 修改 `deploy/Dockerfile`、`app/core/config.py`、`.env.example`、`deploy.env.example`。
4. 在 `app/integrations/openai/browser/environment.py` 实现 `exit_location`、Camoufox 持久指纹读写、离线 MMDB 校验与保鲜、以及统一的 `launch_browser_context`。
5. 更新 `scripts/browser_environment_smoke.py` 支持 `--engine camoufox`，在本机分别跑 `--engine chromium` 与 `--engine camoufox` 验证通过。
6. 跑基本检查。

## 完成标准

- [x] `BROWSER_ENGINE=camoufox` 通过 `validate_configuration` 校验；缺内核时不联网下载而是报 `camoufox_not_installed`
- [x] `.venv/Scripts/python.exe -m scripts.browser_environment_smoke --engine camoufox` 输出 `"ok": true` 且 `"same_profile_stable": true`，两个独立档案指纹不同
- [x] `.venv/Scripts/python.exe -m scripts.browser_environment_smoke --engine chromium` 仍通过，行为未退化
- [x] `deploy/Dockerfile` 包含 Camoufox 依赖库与构建期 `camoufox fetch`
- [x] 基本检查通过：`.venv/Scripts/python.exe -m compileall -q app scripts`、`.venv/Scripts/python.exe -c "import app.main"`

## 完成记录

**改了什么**
- `requirements.txt` 加 `camoufox[geoip]==0.5.9`（配套内核 v158.0.1-beta.41，Windows 包约 1.3 GB；GeoIP AIO 约 53 MB；fpgen 模型 model-2/2026）。本机 `.venv` 已安装并 `camoufox fetch`。
- `deploy/Dockerfile`：`XDG_CACHE_HOME=/ms-playwright/cache`；补 Firefox 动态库（libgtk-3-0、libdbus-glib-1-2、libxt6、libx11-xcb1、libpci3、libegl1 等）并 `playwright install-deps firefox`；构建期 `python -m camoufox fetch`，随后断言 fpgen 模型已固定（`is_pinned`）、内核路径可解析（`camoufox_path(download_if_missing=False)`）、GeoIP ipv4 库存在。
- `app/core/config.py`：`browser_engine` 增加 `camoufox`；新增 `signup_oauth_dwell_min_sec=15` / `signup_oauth_dwell_max_sec=30`，加载时负数归 0、`max < min` 取 `min`（`model_validator`，Part 1 直接读字段即可）。`.env.example`、`deploy.env.example` 同步（deploy 里新写了 `BROWSER_ENGINE=chromium` 与两个停留秒数）。
- `environment.py`：
  - `exit_location(proxy_config)` 返回 `{"ip","country","timezone"}`，失败或时区无效抛 `ProxyGeoError`；`exit_timezone` 改为调用它。
  - `CamoufoxNotInstalledError`（`BrowserEnvironmentError` 子类，`error_code="camoufox_not_installed"`）；`camoufox_executable()` 只用 `camoufox_path(download_if_missing=False)` 定位内核，并检查 fpgen 模型、GeoIP 库，缺任何一样直接抛，不联网。`validate_configuration` 支持 `camoufox` 且会做这项检查。
  - `context_options` 在 `camoufox` 引擎下直接抛错（防止 Part 1 改完前旧调用点悄悄起 Chromium）。
  - 档案 `camoufox_profile_dir()` = `data/chrome-profiles/camoufox/<email>/`；`camoufox_fingerprint()` 首次生成 fpgen Windows 指纹写入 `.team48-camoufox.json`（`{schema, os, fingerprint}`，原子写入），之后只读回；已有 Firefox 数据却没清单、清单损坏或格式不对均抛 `BrowserEnvironmentError`。
  - `camoufox_launch_options()` 组装参数（固定 os/持久档案/humanize/locale/UBO 排除/回环绕过代理等）；`launch_browser_context(profile_dir, proxy_config, *, settings=None, executable_path="")` 按引擎分支，yield `(context, page, actual_profile_dir, summary)`，退出时关闭浏览器；非 headless 时在函数内延迟 import `onboard.ensure_virtual_display()`。
- `scripts/browser_environment_smoke.py`：`--engine` 增加 `camoufox`，三个引擎都改走 `launch_browser_context`；多观测 outer 尺寸、屏幕可用区、dpr、核数、`navigator.webdriver`；同档案不一致时报出变化的字段。

**冒烟结果（本机 Windows，离线代理 127.0.0.1:9，headless）**
- `--engine chromium`：`ok: true`、`same_profile_stable: true`（1280x900、en-US、America/Los_Angeles）。
- `--engine camoufox`：连跑两次均 `ok: true`、`same_profile_stable: true`，两个独立档案观测不同（屏幕/窗口/WebGL 显卡不同），UA 为 Firefox 158 Windows、`webdriver=false`。

**偏离计划的地方**
- 0.5.9 用的是 fpgen 而不是 browserforge：`fingerprint` 参数就是 fpgen 生成的普通 dict，所以清单直接存这个 dict（JSON 往返后再用，首启和之后传入的内容完全一致），没有"还原为 browserforge Fingerprint 对象"这一步。
- 屏幕上限用 HixBrowser 的"宽高合成一个条件"写法（官方 `Screen` 分开两个条件时只有高度生效），范围 1366x768～1920x1080（下限对齐 Camoufox 的现代屏幕下限，避免被抬高后与指纹不符）；生成后若 `window.screenX` 超出 ±50 改为 0，否则 Camoufox 每次启动随机 `screenY`，同档案不稳定。
- 额外 Firefox 设置 `browser.startup.couldRestoreSession.count=2`：实测第二次启动会弹一次"恢复上次会话"提示条，让该次 `innerHeight` 少 39px。
- 固定传 `canvas_noise=True`：Linux 服务器上（Windows 身份 ≠ 宿主系统）官方本来就会开启画布遮盖，效果相同；在 Windows 开发机上则避免官方先不挂代理起一次内核读显卡。因此画布读回是随机数据，冒烟脚本对 camoufox 不比较画布哈希。
- IPv4 出口走 MMDB 时额外加 `network.dns.disableIPv6=True`（照 HixBrowser）；无代理且未配置时区时不设时区、屏蔽 WebRTC。
- `launch_browser_context` 的 `executable_path` 只对 Chromium / Chromix 生效，Camoufox 一律用预装内核（`BROWSER_EXECUTABLE` 也不影响它）。
- 冒烟脚本 chromium 分支不再追加 `--disable-background-networking`（统一走启动器；代理不可达，不影响离线性）。

**上线注意**
- 镜像会大 1 GB 以上（Linux 内核包 + GeoIP + Firefox 依赖），首次 `--build` 较慢；构建期需要能访问 GitHub 下载。
- Camoufox 运行时会往 `$XDG_CACHE_HOME/camoufox/fontconfig` 写字体配置、刷新 `.mmdb` 修改时间，容器以 root 运行可写；若改成非 root 运行需处理 `/ms-playwright/cache` 权限。
- 本机开发环境注意：项目路径含中文时 Windows 上 `indexed_zstd` 打不开 fpgen 的 `values.dat.zst`（`Unable to create a ZSTDSeekContext`），已在本机 `.venv` 里解压出 `values.dat`（哈希与官方 pin 一致，`is_pinned` 仍通过）。容器路径是 ASCII，不受影响。
- 冒烟只在本机 Windows headless 下验证；Linux 容器内 headed（Xvfb）还需 part-2 部署后跑一次 `python -m scripts.browser_environment_smoke --engine camoufox --headed`（镜像不含 `scripts/`，需 `docker cp`）。

**需要写进 docs 的内容**
- ARCHITECTURE「浏览器与注册」：`launch_browser_context` 统一启动；Camoufox 档案 `data/chrome-profiles/camoufox/<email>/` + `.team48-camoufox.json` 固定指纹、损坏即停；地区/WebRTC 规则（`BROWSER_TIMEZONE` 优先 → 出口 IPv4 且 MMDB 可查走 geoip → 否则按出口时区并屏蔽 WebRTC）；缺内核报 `camoufox_not_installed`。
- ARCHITECTURE「配置项」：`BROWSER_ENGINE` 增加 `camoufox`；新增 `SIGNUP_OAUTH_DWELL_MIN_SEC` / `SIGNUP_OAUTH_DWELL_MAX_SEC`。
- RUNBOOK：切换 / 回退 `BROWSER_ENGINE` 的方法与容器内 camoufox 冒烟命令。

**基本检查**：`compileall` 与 `import app.main` 均通过（检查时 Part 1 的文件也在工作区里，未报错）。
