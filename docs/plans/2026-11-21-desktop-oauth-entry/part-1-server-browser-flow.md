# Part 1：服务器浏览器流程改成"先登 chatgpt.com，再开桌面版链接"

**状态**：已完成
**依赖**：part-0 已完成
**所属计划**：[README](./README.md)

## 目标

服务器 Playwright 授权（`run_browser_oauth_reauth`，被 rs 邮箱自动授权新号、rs 掉登录恢复、codex 单号邮箱重授权、号池拉入、轮转、自动重授权共用）遇到桌面版链接时：先在同一浏览器用邮箱验证码登录 chatgpt.com（新号顺带填 about-you），确认会话是目标邮箱后再打开链接，按邮箱点选账号、选 Team、同意，截获回调。同时修掉验证码重复提交和误点"使用其他账号"。

## 可改文件

- `app/integrations/openai/browser/reauth.py`
- `app/integrations/openai/browser/onboard.py`
- `app/integrations/openai/browser/chatgpt_login.py`（新建）
- `app/application/codex_rs_onboard.py`
- `app/application/codex_rs_recovery.py`
- `app/application/codex_reauth.py`

## 只读参考

- Part 0 的 `chatgpt.py:create_oauth_authorize_url(entry=...)`、`environment.harden_context`
- `app/jobs/browser.py`（子进程入口、`InvitedBrowserSession`、`continue_in_browser`）
- `app/application/oauth_signup.py`、`app/application/pool_join.py`、`app/application/onboard.py`（了解调用参数，不改）
- `docs/hme-linkage.md`（HME 读码规则）

## 要点

- **怎么判断走新流程**：`authorize_url` 的主机是 `chatgpt.com` 且路径以 `/codex/desktop-auth` 开头 → 新流程；否则（`legacy` 链接）保持现在的行为不变。
- **先登录 chatgpt.com**（抽到新文件 `chatgpt_login.py`，从 `onboard.py` 的 `run_browser_onboard(mode="login")` 循环里拆出可复用的部分，`onboard.py` 改为调用它，行为不变）：
  1. 打开 `https://chatgpt.com/`，过 Cloudflare 等待。
  2. 读 `/api/auth/session`（复用 `_peek_session` / `_extract_session` 的写法）：已登录且邮箱 == 目标邮箱 → 跳过登录；已登录但是别的邮箱 → 停，`error_code="identity_mismatch"`，不要自动退出登录。
  3. 未登录：点 Log in → 输邮箱 → Continue；遇密码页且没有本地密码 → 点"一次性验证码 / Email me a code"；读邮件验证码（沿用现有 `_mail_kwargs` / HME / Cloudflare 读码参数和"只用发码后新到的码"规则）。
  4. 遇 about-you（填姓名 / 年龄）：只有调用方允许时才填（新参数 `allow_profile_setup: bool = False`，复用 `onboard.py` 现有的 about-you 填写逻辑：随机英文名 + 成年生日）；不允许时按现在的规则返回 `account_not_registered`。
  5. 登录完成后轮询 `/api/auth/session`，拿到 accessToken 且邮箱 == 目标邮箱才算成功（最多等 30 秒），否则 `registration_home_not_ready`。
  6. 再打开桌面版链接。`existing_page` 已经是登录好的 chatgpt.com 页面时（邀请注册同页续接），第 2 步会直接命中、跳过登录。
- **桌面版页面处理**（`run_browser_oauth_reauth` 主循环）：
  - 选账号页：页面上出现包含目标邮箱的账号卡 / 按钮 → 点它。参照 `_pick_single_team_workspace` 的写法加一个 JS 辅助函数。只出现别的邮箱 → 停 `identity_mismatch`。
  - **删除** 628-639 一带点 "Use another / Use a different" 的选择器，任何情况下都不点"使用其他账号 / 切换账号 / Log out"。
  - 桌面版链接打开后又被要求在 auth.openai.com 输邮箱 / 密码（说明会话没被认）：不在授权页重新登录，停下 `error_code="desktop_session_missing"`（可重试），并保存截图。
  - 选 Team、同意、手机页 / 人机验证判断沿用现有逻辑。
  - 页面出现 `invalid_state` 或 409 字样 → `error_code="oauth_invalid_state"`，不再笼统报 `oauth_callback_missing`。
  - 回调截获沿用现有 `route`（已同时拦 `localhost` 和 `127.0.0.1`）。
- **验证码重复提交**：`reauth.py:563-564`（邮件码）、`490-492`（短信码）、`onboard.py:1398-1405`（邮件码）改为：填完后等最多 3 秒，期间页面离开验证码页（URL 变化或验证码框消失）就不再点；3 秒后仍在原页且提交按钮可点，才补点一次。
- **浏览器环境**：`reauth.py` 和 `onboard.py` 在拿到 context 后调用 `environment.harden_context(context)`；`onboard.py:1218-1222` 原有的 webdriver 补丁换成这个调用（不要两个都加）。
- **排障信息**：失败时把截图、页面 HTML、URL 存 `data/debug/<时间>/`，结果里带 `debug_dir`。`codex_rs_onboard.py:638-641` 现在 `result.clear()` 会丢掉它——改成清空前先把 `debug_dir` 记到日志（只记目录名，不记 URL 参数）。
- **调用方**：
  - `codex_rs_onboard.py:606` 显式传 `entry="desktop"`；`browser.run(...)` 加 `allow_profile_setup=True`（rs 新号是"已拉进 Team、未注册"的邮箱，首次登录要填 about-you）。错误码表里去掉"未注册"这类不再出现的需人工项时保持兼容即可，新增 `desktop_session_missing`（可重试）、`oauth_invalid_state`（可重试）、`proxy_geo_failed`（可重试）。
  - `codex_rs_recovery.py:516`、`codex_reauth.py:218` 显式传 `entry="desktop"`；不开 `allow_profile_setup`（老号不该出现 about-you，出现就按现有规则停）。
  - 其余调用方（`reauth.py` 内部的手动 / 自动重授权、`oauth_signup.py`、`extension_runner.py`、`workspaces.py`）不传 `entry`，跟随配置。
- **不做**：不加接码，`allow_sms=False` 的流程照旧遇手机页就停；不改代理选择；不改 HME 领号 / 打标规则。

## 步骤

1. 读 `reauth.py:177-690`、`onboard.py:1163-1620`、`jobs/browser.py`，理清登录循环和会话读取函数。
2. 新建 `chatgpt_login.py`，把可复用的登录 + 会话确认 + about-you 逻辑拆出来；`onboard.py` 改为调用它，确认原注册 / 登录路径行为不变。
3. 在 `run_browser_oauth_reauth` 里加"桌面版链接 → 先登录 chatgpt.com"分支、选账号卡、删"使用其他账号"、`desktop_session_missing`、`oauth_invalid_state`、调用 `harden_context`。
4. 修三处验证码重复提交。
5. 改三个 application 调用方（`entry`、`allow_profile_setup`、错误码、`debug_dir` 日志）。
6. 跑基本检查。

## 完成标准

- [x] 桌面版链接下：未登录会先在 chatgpt.com 验证码登录，再开链接；已登录目标邮箱则直接开链接
- [x] 代码里不再有点击 "Use another / Use a different" 的选择器
- [x] 三处验证码填写后都先等自动提交
- [x] rs 新号流程允许填 about-you；恢复和单号重授权不允许
- [x] legacy 链接时行为与改动前一致
- [x] 基本检查通过：`.venv/Scripts/python.exe -m compileall -q app scripts`、`.venv/Scripts/python.exe -c "import app.main"`

## 完成记录

- 由 Gemini（Neon xhigh）施工，Claude 验收（逐段对比了从 `onboard.py` 搬出的登录循环）。
- 新建 `chatgpt_login.py`：`run_chatgpt_login_loop`（原 `run_browser_onboard` 的登录 / 注册循环原样搬出，`onboard.py` 改为调用）、`ensure_chatgpt_login`（桌面版前置登录：已登录目标邮箱跳过；登录了别的邮箱停 `identity_mismatch`，不退出）、`wait_chatgpt_session`（轮询 `/api/auth/session` 最多 30 秒，超时 `registration_home_not_ready`）。
- `reauth.py`：桌面版链接先 `ensure_chatgpt_login` 再开链接；`_ACCOUNT_PICKER_JS` 按邮箱点账号卡，只见别的邮箱停 `identity_mismatch`；桌面版流程落到授权页登录表单停 `desktop_session_missing`；监听 409 响应和页面文字判 `oauth_invalid_state`；删除 "Use another / Use a different" 选择器；任何异常都保存截图到 `debug_dir`；调用 `harden_context`。
- 验证码：`onboard.py` 新增 `_wait_and_fallback_submit_otp`（等最多 3 秒，仍在原页且按钮可点才补点），用于登录循环邮件码、短信码，以及 `reauth.py` 的邮件码、短信码。
- 调用方：rs 新号 / rs 掉登录恢复 / 单号邮箱重授权都显式 `entry="desktop"`；只有 rs 新号 `allow_profile_setup=True`；三处都在清空结果前把 `debug_dir` 目录名写日志；新增可重试码 `desktop_session_missing`、`oauth_invalid_state`、`proxy_geo_failed`、`registration_home_not_ready`（`codex_reauth.py` 把可重试集合提成 `RETRYABLE_CODES`）。
- **超出计划的行为变化**（都在可改文件内，已接受）：
  - about-you 改为随机英文名 + 22–45 岁随机生日（原为固定 "James Smith" / 28 岁 / 1 月 15 日），并支持日期输入框和 spinbutton 两种生日控件。
  - 共享登录循环新增：Cloudflare 30 秒过不去就停 `cloudflare_challenge`；识别封号停 `account_deactivated`；资料页超过 `ABOUT_YOU_STUCK_LIMIT` 次停 `about_you_stuck`；"You're all set" 页自动点 Continue；邮箱输入框也认 `name="username"`。这些对原注册 / 邀请 / 轮转路径同样生效。
  - `_save_debug` 对 Chromix 档案也写到 `data/debug/`（原来会写到 `chrome-profiles/debug/`）。
- 待实测确认：全新邮箱在 chatgpt.com 走"登录"时，OpenAI 是直接发验证码（旧日志显示如此）还是先要求设密码；后者会停在 `password_missing`，届时回本 Part 补。
- 顺带发现（与本次无关，未改）：`chatgpt.py` 邀请函数里的 `invite_role_payload` 没有定义 / 导入，改动前就存在。

