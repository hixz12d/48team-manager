# Part 2：服务端运行器

**状态**：待开始
**依赖**：part-0 已完成
**所属计划**：[README](./README.md)

## 目标

实现 `app/application/extension_runner.py` 的两个主函数：服务器不借助 Playwright，直接以普通子进程启动 Chromix 并加载插件；通过 `/api/ext/runner/*` 接口和插件对话；完成"注册 → 确认入组 → 下发授权链接 → 换票并写入凭据"，或执行环境自检。

## 可改文件

- `app/application/extension_runner.py`（填实现；part-0 定下的签名、数据类、常量不改）
- `app/integrations/openai/browser/runner_process.py`（新建：运行目录生成、子进程启动 / 关闭、Xvfb 与代理桥）
- `app/web/routes/runner.py`（新建：`build_runner_router(get_db)`，挂载由 part-4 负责）

## 只读参考

- `docs/contracts/extension-runner.md`
- `app/integrations/openai/browser/runner_profile.py`、`app/integrations/proxy/geo.py`（part-0）
- `app/integrations/proxy/socks_bridge.py`（`SocksAuthBridge(host, port, user, pw)`、`.port`、`.close()`；`chrome_proxy_launch`）
- `app/integrations/sms/client.py:128-142` `chrome_proxy_config`
- `app/integrations/openai/browser/onboard.py:58-103` `ensure_virtual_display`
- `app/application/oauth_signup.py:13-131`（授权链接生成、`oauth_session_store.persist / begin_exchange / exchange_context / finish`、`exchange_oauth_code`、邮箱核对，照这个写换票）
- `app/application/invitation_flow.py:109-152` `authorize_joined`（入组与角色席位核对、`apply_tokens`）
- `app/application/onboard.py:104` `_confirm_joined`、`:730-746` `ensure_membership` 用法
- `app/application/reauth.py:55-60` `load_cf_config`
- `app/web/routes/extension.py`（路由写法参考；本 Part 不改它）

## 要点

### 运行登记（进程内）

- 模块级 `_RUNS: dict[run_id, RunState]`，`RunState` 保存：`token_hash`、`kind`、`job`（发给插件的任务）、`status`、`last_event_at`、`events`（最近 50 条，脱敏）、`command`（none/authorize/stop）、`authorize_url`、`callback_url`、`probes`、`asyncio.Event`（有新事件时唤醒等待方）。
- 路由只通过 `extension_runner` 暴露的函数（如 `runner_job(run_id, token)`、`runner_event(...)`、`runner_callback(...)`、`runner_probe(...)`）读写登记；token 用 `hmac.compare_digest` 比对 sha256；未知 run、token 不符、运行已结束都返回 404。
- 容器重启后登记丢失：进行中的轮转会被现有机制标为 `manual_required`，不需要额外处理。

### `signup_and_authorize`

1. `validate_runner_configuration()`、`check_runner_proxy(proxy_url)`。
2. 档案：`runner_profile` 读取已有档案；没有就经代理 `geo.lookup_exit` 查时区后新建（平台 / 显卡取当前配置）。把 `profile_summary` 通过 `on_stage("browser_environment", summary)` 记入任务（`invitation_flow.browser_progress` 只会原样保存这个阶段的文字）。
3. 生成运行目录 `data/runner-runs/<run_id>/`：复制 `RUNNER_EXTENSION_DIR` 全部文件（排除本机 `private-config.mjs`），生成新的 `private-config.mjs`（`MAILBOX` 来自 `load_cf_config`，`RUNNER` 含 `baseUrl/runId/token`），按协议改写 manifest 的 `host_permissions`。目录权限 700。
4. 代理：`socks5` 带认证 → 起 `SocksAuthBridge`，`--proxy-server=socks5://127.0.0.1:<port>`；无认证 socks5 / http 直接用；带认证 http 已被第 1 步拒绝。
5. `ensure_virtual_display()` 后用 `subprocess.Popen([executable, *chromix_args(...), "about:blank"], env={..., "DISPLAY": ":99", "TZ": 档案时区})` 启动；标准输出重定向到运行目录日志（结束时只保留最后 50 行进任务诊断，不含 URL 参数）。**不传 `--remote-debugging-port`，不使用 Playwright。**
6. 任务内容：`email=account.email`、`password`（取 `account.password_encrypted`，为空则 `random_password()` 并写回账号后 commit）、`profile`（沿用 `signup.signup_profile(profile_dir)` 生成并固定的姓名生日，写在运行器档案目录旁）、`workspaceNames`（参照 `member_handoff._workspace_names`）。
7. 等待循环（每次最多等 5 秒事件，`on_stage("heartbeat", "")` 每 30 秒一次以维持任务心跳和取消检查）：
   - 插件 `status:"paused"` → 结束，错误码按协议映射（`phone` → `phone_verification_required`）。
   - 插件 `status:"stopped"` 或进程退出 → `runner_exited`。
   - 超过 120 秒没有任何事件 → `runner_heartbeat_lost`；总时长超过 `RUNNER_TIMEOUT_SECONDS` → `runner_timeout`。
   - 插件报 `phase:"signup", status:"done"` → 在服务端做入组确认：最多 8 次、间隔 5 秒调用 `OnboardService._confirm_joined` 等价逻辑（可直接复用 `rotate.onboard`/`OnboardService` 实例由调用方传入；如不便，直接用 `workspace_service.lookup_live_member`），核对 `official_roles_equivalent(role)` 与 `existing_invite_seat_error(seat_intent)`。确认后 `ensure_membership(... joined)`、`joined=True`，并生成授权链接（照 `oauth_signup.py:21-43`：`create_oauth_authorize_url(login_hint=email)`、`create_session`、`persist(purpose="account_reauth")`），设置 `command="authorize"`。未确认 → `not_joined` 或 `membership_mismatch`，下发 `stop`。
   - 收到回调 → 照 `oauth_signup.py:84-106` 做 `begin_exchange`、revision 核对、`exchange_oauth_code(identifier=email)`（走账号代理）、token 邮箱核对；成功后 `auth_service.apply_tokens`、`auth_state="healthy"`、commit，`authorized=True`。
   - 每个关键节点用 `on_stage` 记一句中文进度：`runner_started`、`runner_signup_done`、`runner_joined`、`runner_authorizing`、`runner_callback`、`runner_authorized`。
8. `finally`：给插件下发 `stop`，等 3 秒后 `terminate` → 5 秒后 `kill` 子进程（连同进程组），关闭代理桥，`oauth_session_store.finish`，从 `_RUNS` 移除，删除运行目录（`shutil.rmtree`，忽略错误并记日志）。浏览器档案目录保留（同一邮箱续接时复用）。
9. 返回 `RunnerOutcome`，`diagnostics` 含 `profile_summary`、事件摘要、插件结束时上报的脱敏诊断。

### `run_selfcheck`

- 使用临时档案 `data/chrome-profiles/runner-selfcheck/<run_id>/`，平台用参数 `platform`（为空则用配置），不写入正式档案，结束删除。
- `probeUrls` 固定：`https://www.browserscan.net/`、`https://abrahamjuliot.github.io/creepjs/`、`https://browserleaks.com/webrtc`。
- 等插件上报 `status:"done"` 或 4 分钟超时。截图保存到 `data/selfcheck/<job_id>/<name>.png`（最多保留最近 10 次自检的目录，更早的删除）。
- 返回 `{"ok", "summary": profile_summary, "exit": {...}, "signals": {...}, "screenshots": [相对路径], "findings": [..]}`。`findings` 由服务端比对得出，例如：时区与出口国家不符、WebRTC 候选里出现非出口 IP、`webdriver` 为 true、WebGL 含 `SwiftShader`、UA 平台与 `navigator.platform` 不一致、窗口大于屏幕。

### 路由（`app/web/routes/runner.py`）

- `APIRouter(prefix="/api/ext/runner")`，四个接口见协议文档；鉴权只认本次运行的 token，**不接受**管理员会话，也不依赖 `EXTENSION_API_TOKEN`。
- 请求体用 pydantic 模型写在本文件内，限制大小（event ≤16KB、probe ≤4MB）。

## 步骤

1. 写 `runner_process.py`：`prepare_run_dir`、`launch`、`terminate`、代理桥封装。
2. 填 `extension_runner.py`：登记、`signup_and_authorize`、`run_selfcheck`、供路由调用的函数。
3. 写 `routes/runner.py`。
4. 本机可用 Windows 版 Chrome 手动冒烟（可选）：设 `RUNNER_BROWSER_EXECUTABLE` 指向本机 Chrome、`kind=selfcheck`，确认插件能领任务并回报；冒烟产生的档案、截图、运行目录全部删除。
5. 基本检查。

## 完成标准

- [ ] 启动命令里没有 `--remote-debugging-port`，代码中本流程不 import playwright
- [ ] 任意失败路径都会关闭浏览器进程、代理桥并删除运行目录
- [ ] 换票与邮箱核对规则与 `oauth_signup.py` 一致；token、密码、回调 URL 不写日志和任务步骤
- [ ] 基本检查通过：`.venv/Scripts/python.exe -m compileall -q app scripts` 与 `.venv/Scripts/python.exe -c "import app.main"`

## 完成记录
