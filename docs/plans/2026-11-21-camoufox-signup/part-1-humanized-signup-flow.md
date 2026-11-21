# Part 1：拟人化注册操作、自动完成入组欢迎页与授权前停留

**状态**：已完成
**依赖**：part-0 已完成
**所属计划**：[README](./README.md)

## 目标

把服务器浏览器注册、登录与授权全链路接入 Part 0 的 `launch_browser_context`（支持 Camoufox），并消除现有自动化注册中的四个问题：
1. 邮箱、验证码、`about-you` 姓名生日及关键按钮全部改为真人不均匀节奏输入、随机偏移点击与阅读停顿，彻底去掉 `fill()` 瞬间赋值和固定间隔打字，扩充并按邮箱持久化随机姓名生日；
2. 登录 `chatgpt.com` 后自动识别并点击 `/onboarding` 及工作空间欢迎卡（`Get started` / 接受邀请），确认进入目标或唯一 Team 空间；
3. 本次新建账号进入主页后随机自然停留 **15～30 秒**，再同窗打开桌面版授权链接完成授权。

## 可改文件

- `app/integrations/openai/browser/humanize.py`（新建）
- `app/integrations/openai/browser/onboard.py`
- `app/integrations/openai/browser/chatgpt_login.py`
- `app/integrations/openai/browser/reauth.py`
- `app/application/codex_rs_onboard.py`
- `app/application/oauth_signup.py`
- `app/application/presenters.py`

## 只读参考

- Part 0 的 `app/integrations/openai/browser/environment.py`（`launch_browser_context`）与 `app/core/config.py`（`browser_engine`、`signup_oauth_dwell_min_sec`、`signup_oauth_dwell_max_sec`）
- `docs/plans/2026-11-21-desktop-oauth-entry/part-3-live-run.md`（第 2、3 轮现场还原：`myriad-capote.2v@` 2 秒跳授权、`house-reducer-0y@` 停在 `chatgpt.com/onboarding` 未点 `Get started` 报 `not_team_workspace`）
- `app/application/codex_rs_recovery.py`、`app/application/codex_reauth.py`（了解老号恢复调用参数：`allow_join=False`、`allow_profile_setup=False`，不得被误改成允许自动入新组或填资料）

## 要点

- **新建 `humanize.py`（统一拟人交互原语）**：
  - `human_pause(page, min_sec: float = 0.8, max_sec: float = 2.2)`：用随机浮点数等待，模拟阅读与反应停顿。
  - `human_click(page, target, *, timeout: int = 3500) -> bool`：先确认元素可见可点，取元素包围盒并在中心 `35%～65%` 区域内随机偏移落点（避免每次精确点正中心像素），点击前短停 `80～220ms`，点击按下抬起带 `45～115ms` 随机延迟（配合 Camoufox `humanize=True` 自动生成弧线鼠标轨迹）；拿不到包围盒时退回常规点击。
  - `human_type_text(page, target, text: str, *, clear: bool = True) -> bool`：先 `human_click` 聚焦并短停 `120～280ms`；若输入框已有不同内容且 `clear=True`，用全选 + `Backspace` 清空（**禁止 `.fill("")`**）；随后逐字符敲击：普通字母数字间隔 `45～135ms`，空格 / `@` / `.` / `-` 间隔 `160～360ms`，约每 8～12 个字符穿插一次 `240～520ms` 微停顿，输完短停 `150～320ms`。
  - `human_type_otp(page, code: str, *, single_input=None, boxes=None) -> bool`：先停 `350～800ms`（模拟看验证码邮件）；按真人读 6 位数的习惯分两拍（前 3 位后停 `180～380ms`），每位间隔 `80～200ms` 逐位敲入。6 格框（`input[maxlength='1']`）先 `human_click` 第 1 格再逐位按键盘，若某格未自动进焦则补点该格再敲；单输入框先 `human_click` 再逐位敲入。**禁止对验证码框调 `.fill()`**。
  - `signup_identity_for_email(profile_root: Path, email: str) -> dict[str, Any]`：
    - 扩充英文名池到至少 60 个常见英文名 × 60 个常见姓氏（3600+ 组合），随机 22～45 岁、合法年月日（日取 1～28）。
    - 按邮箱持久化保存在 `data/chrome-profiles/.signup-identities/<email_dir>.json`（原子写入），同一邮箱无论重试还是切换浏览器引擎，永远填同一套姓名与生日；`email` 为空时才临时随机生成。
- **改造 `onboard.py`**：
  - `ensure_virtual_display()`：把 `1280x900x24` 改为 `1920x1080x24`（与 `Dockerfile` 一致，防止 Xvfb 重启后把 Camoufox 窗口尺寸压扁）。
  - `run_browser_onboard`：改为使用 `environment.launch_browser_context(...)` 启动浏览器上下文；当 `settings.browser_engine == "camoufox"` 时，即便 `(signup_flow or settings.browser_signup_flow) == "extension"`，注册模式（`mode == "register"`）也走 `run_chatgpt_login_loop`（因为 `run_managed_signup` 强依赖 Chromium 专有的 CDP `new_cdp_session`，Firefox/Camoufox 不支持 CDP）。
  - `_click_first`、`_click_exact` 内部改用 `human_click`；`_fill_first`、`_fill_email` 改用 `human_type_text`（仅当连续 2 次真实敲击后输入框仍为空时才用原有 `Object.getOwnPropertyDescriptor` 兜底）；`_fill_otp` 改用 `human_type_otp`。
  - `_fill_about_you(page, *, email: str = "")`：进入资料页先 `human_pause(page, 1.2, 2.6)`；用 `signup_identity_for_email` 取固定姓名生日；姓名框用 `human_type_text` 逐字敲入 → 停 `0.5～1.1s` → 生日优先处理可见的下拉框 / `spinbutton` / 分段输入框（逐字敲数字并带停顿；仅原生 `<input type="date">` 无法直接逐字敲入时才允许赋值兜底）；生日已填成功时**不要**再去瞎试不存在的 `age` 框（只有页面上确实可见 `age` 输入框且未填生日时才用 `human_type_text` 填年龄）；填完停 `0.8～1.8s` 再点提交按钮。
  - `_save_debug`：父目录名判断改为 `in {"chromix", "camoufox"}`（连同 `chatgpt_login.py` 里的 `debug_log` 路径一起改），确保 Camoufox 失败截图同样落在 `data/debug/<时间>/`。
- **改造 `chatgpt_login.py`（入组欢迎页 `Get started` + 新号标记）**：
  - 在登录/注册主循环的邮箱页、密码切验证码、验证码提交前加入 `1.0～2.4s` 自然停顿；短信验证码框同样改用 `human_type_otp`；调 `_fill_about_you(page, email=email)`。
  - 记录 `created_account: bool`（本次运行中触发过 `about-you` 填写或 `create_account` 即为 `True`），放进 `run_chatgpt_login_loop` 和 `ensure_chatgpt_login` 的返回字典。
  - 新增 `complete_chatgpt_onboarding(page, *, team_name: str = "", pick_single_team: bool = False, report: StageCallback = None) -> dict[str, Any]`：
    - 适用时机：`ensure_chatgpt_login(..., allow_onboarding: bool = False, team_name: str = "", pick_single_team: bool = False)` 在确认会话是目标邮箱后（**包括 `skipped_login=True` 已有登录态的情况**，以覆盖 `house-reducer-0y@` 这类已注册但停在 `/onboarding` 的账号），若 `allow_onboarding=True` 则执行；`run_chatgpt_login_loop` 在 `mode == "register"` 或 `invite_entry=True` 时也执行。
    - 执行逻辑：
      1. 先在 `chatgpt.com` 等待 `2.0～3.2s`（新号或受邀号打开 `/` 后常在 1～2 秒内前端跳转到 `/onboarding` 或弹出邀请卡片）。
      2. 最多循环 6 轮（总计约 20 秒）：检查当前 URL（路径含 `/onboarding`、`/workspace`、`/accept-invite`）及页面可见文案/弹窗（如 `"You've been added to a new workspace"`、`"invited you"`、`"Get started"`、`"Accept invite"`、`"Join workspace"`、`"Choose a workspace"`）。
      3. 若出现多个工作空间选项：有 `team_name` 时点匹配的团队名；无 `team_name` 且 `pick_single_team=True` 时过滤掉 `"Personal account"` / `"个人"`，若非个人 Team 多于 1 个则立即返回 `{"ok": False, "error_code": "workspace_ambiguous", "error": "有多个 Team，未自动选择"}`，恰好 1 个则 `human_click` 点选它并停 `0.8～1.5s`。
      4. 若存在入组/欢迎确认按钮（`"Get started"`、`"Accept invite"`、`"Join workspace"`、`"Accept"`、`"Continue"`、`"Okay, let's go"`、`"开始使用"`、`"接受邀请"`、`"加入工作空间"`）或条款勾选框，上报 `report("onboarding", "正在接受团队邀请进入工作空间")`，用 `human_click` 点击并等待 `2.0～3.0s`。
      5. 当路径已离开 `/onboarding` 且页面不再有入组欢迎卡/邀请弹窗（主聊天框 `#prompt-textarea` 或常规主页已就绪），再次核对 `/api/auth/session` 仍是目标邮箱后返回 `{"ok": True, "onboarding_completed": acted}`。
      6. 若连续 6 轮仍卡在 `/onboarding` 未能进入主页，返回 `{"ok": False, "error_code": "onboarding_stuck", "error": "卡在 ChatGPT 入组欢迎页，未能自动进入工作空间"}`。
- **改造 `reauth.py`（Camoufox 启动器 + 主页 15～30 秒自然停留 + 授权页拟人）**：
  - `existing_browser is None` 时改用 `environment.launch_browser_context(...)` 启动浏览器上下文。
  - `run_browser_oauth_reauth` 新增参数 `allow_onboarding: bool | None = None`（未传时默认等于 `bool(allow_join)`），连同 `team_name`、`pick_single_team` 一起传给 `ensure_chatgpt_login(...)`。
  - **授权前停留**：`desktop_flow` 下 `ensure_chatgpt_login` 成功后：
    - 若 `login_res.get("created_account")` 为 `True`：从配置读取 `signup_oauth_dwell_min_sec` / `signup_oauth_dwell_max_sec`（默认 `15～30` 秒），抽取随机停留秒数 `dwell_sec`，上报 `report("post_signup_dwell", f"已进入团队主页，停留 {int(round(dwell_sec))} 秒后发起授权")`。按 3～5 秒分段等待，期间做 1～2 次轻微页面内鼠标移动（`page.mouse.move`），若等待期间延迟弹出 `/onboarding` 欢迎卡且 `effective_onboarding` 为真，补跑一次 `complete_chatgpt_onboarding`。
    - 若是已注册老号（`created_account` 为 `False`）：只做 `1.8～3.5s` 自然短停顿，不拖慢掉登录恢复速度。
  - 授权循环里：短信/邮件验证码框改用 `human_type_otp`；选账号卡（`_pick_account_card`）、选工作空间（`_pick_single_team_workspace` / `_pick_workspace`）、点同意/继续按钮前先 `human_pause(page, 0.8, 1.8)` 并用 `human_click` 点击。
  - 当 `effective_onboarding` 为 `True`（如 `codex_rs_onboard`）时，即使 `allow_join=False`，遇到 `Accept invite` / `Join workspace` 按钮也自动点击入组，不再误报 `membership_changed`；而 `codex_rs_recovery` 与 `codex_reauth`（两者都不传 `allow_onboarding` 且 `allow_join=False`）仍保持原样报 `membership_changed`。
- **调用方与进度文案（`codex_rs_onboard.py`、`oauth_signup.py`、`presenters.py`）**：
  - `codex_rs_onboard.py`：`browser.run(...)` 增加 `allow_onboarding=True`；`STAGE_MESSAGES` 增加 `"onboarding": "正在接受团队邀请"`、`"post_signup_dwell": "已进入团队主页，稍候发起授权"`；`REASONS` 增加 `"onboarding_stuck": "入组欢迎页未完成，请重试"`、`"camoufox_not_installed": "服务器未安装 Camoufox 浏览器"`（`onboarding_stuck` 不放进 `MANUAL_CODES`，允许在 rs 弹框点"重试"；`camoufox_not_installed` 也可重试以便修好环境后直接点重试）。
  - `oauth_signup.py`：`run_invited_oauth_signup` 调用 `runner(...)` 显式传 `allow_onboarding=True`。
  - `presenters.py`：`BUSINESS_STEP_LABELS` 与 `_BROWSER_STAGES` 补充 `"onboarding"`（`"接受团队邀请"`）和 `"post_signup_dwell"`（`"主页停留等待授权"`）。

## 步骤

1. 新建 `app/integrations/openai/browser/humanize.py`，实现暂停、偏移点击、逐字输入、OTP 分拍逐位输入与持久化扩充姓名生日库，并用 `.tmp/` 小脚本（或 `browser_environment_smoke` 思路）验证姓名库持久化幂等与输入函数无语法/运行时错误（临时文件用完即删）。
2. 改 `onboard.py`：切到 `launch_browser_context`、`ensure_virtual_display` 分辨率对齐 `1920x1080x24`、Camoufox 注册分流、替换瞬间填表与调试目录层级。
3. 改 `chatgpt_login.py`：接入拟人停顿与 `email` 透传、返回 `created_account`、实现 `complete_chatgpt_onboarding`。
4. 改 `reauth.py`：切到 `launch_browser_context`、透传 `allow_onboarding`、新号 15～30 秒主页停留、授权循环拟人点击与验证码输入。
5. 改 `codex_rs_onboard.py`、`oauth_signup.py`、`presenters.py`。
6. 跑基本检查。

## 完成标准

- [x] `onboard.py`、`chatgpt_login.py`、`reauth.py` 中对邮箱、验证码、姓名框不再使用 `.fill()` 瞬间赋值；姓名池不少于 60×60 组合且同邮箱重试保持一致
- [x] `reauth.py` 与 `onboard.py` 均通过 `launch_browser_context` 启动浏览器，`BROWSER_ENGINE=camoufox` 与 `BROWSER_ENGINE=chromium` 均可正常工作
- [x] `ensure_chatgpt_login` 在 `allow_onboarding=True` 时能处理 `/onboarding` 及工作空间欢迎卡（含已登录 `skipped_login` 场景），多 Team 仍正确报 `workspace_ambiguous`
- [x] 新建账号（`created_account=True`）在发起桌面版授权前有 15～30 秒随机停留，已注册老号恢复不走长停留
- [x] `codex_rs_recovery` 与 `codex_reauth` 仍保持 `allow_onboarding=False`、`allow_profile_setup=False`
- [x] 基本检查通过：`.venv/Scripts/python.exe -m compileall -q app scripts`、`.venv/Scripts/python.exe -c "import app.main"`

## 完成记录

与 Part 0 并行施工（按 Part 0 写定的接口编码），由 Gemini 施工、Claude 验收；只改了本 Part 的 7 个可改文件。

**改了什么**
- `humanize.py`（新建）：`human_pause` / `human_click`（35%～65% 偏移落点 + 按压延迟）/ `human_type_text`（全选 + Backspace 清空，不均匀逐字敲）/ `human_type_otp`（3+3 两拍逐位）/ `human_nudge_mouse` / `uniform_float`；姓名库 84 名 × 120 姓，按邮箱（不区分大小写）持久化到 `data/chrome-profiles/.signup-identities/<email_dir>.json`，22～45 岁、日取 1～28。
- `onboard.py`：Xvfb 改 `1920x1080x24`；`run_browser_onboard` 改走 `launch_browser_context`；Camoufox 引擎下 `register` 不走 `run_managed_signup`；点击 / 填表 / 验证码全部换拟人原语；`_fill_about_you(page, email=)` 用持久身份，生日成功后不再试年龄框；`_save_debug` 支持 `camoufox` 层级。删除旧的 12×12 姓名池和 `_random_about_you_profile` 的随机实现。
- `chatgpt_login.py`：新增 `looks_like_chatgpt_onboarding`、`complete_chatgpt_onboarding`（6 轮，按团队名或唯一非个人 Team 选择，多 Team 报 `workspace_ambiguous`，卡住报 `onboarding_stuck`，结束前核对会话邮箱）；`run_chatgpt_login_loop` / `ensure_chatgpt_login` 新增 `allow_onboarding`、`pick_single_team`（后者还有 `team_name`），返回 `created_account` / `onboarding_completed`；`skipped_login` 已登录场景同样执行入组。
- `reauth.py`：改走 `launch_browser_context`（由 ExitStack 关闭浏览器）；新增 `allow_onboarding`（默认等于 `allow_join`）；`created_account=True` 时按配置停留 15～30 秒，3～5 秒分段、1～2 次鼠标轻移，期间弹出欢迎卡会补跑入组；老号只停 1.8～3.5 秒；`effective_onboarding` 时授权页遇 `Accept invite` / `Join workspace` 自动点击，否则仍报 `membership_changed`。
- `codex_rs_onboard.py`、`oauth_signup.py`：传 `allow_onboarding=True`；补 `onboarding` / `post_signup_dwell` 阶段文案和 `onboarding_stuck` / `camoufox_not_installed` 原因（均可重试）。`presenters.py` 补两个阶段标签。

**偏离计划**
- 唯一保留的 `.fill()` 是原生 `<input type="date">` 兜底（计划允许）；生日优先顺序改为：下拉框 → spinbutton → 分段输入框 → `bday` 单框 → 原生日期框。
- 旧号密码框也改用 `human_type_text` 逐字输入（计划没提，顺带统一）。
- `codex_rs_recovery.py`、`codex_reauth.py` 未改动，它们不传 `allow_onboarding` 且 `allow_join=False`，所以入组欢迎卡不会自动处理。

**检查**
- `compileall` 与 `import app.main` 通过。
- `Settings(browser_engine="camoufox", 负数停留)` 归一为 0/0。
- 同一邮箱大小写不同、两次调用得到同一身份（临时目录离线验证，已删除）。
- 没有在真实页面上跑过（Part 2 实测）。

**上线注意 / 需要写进 docs**
- PRODUCT：rs 邮箱自动授权新号会自动点掉 `/onboarding` 入组欢迎卡，新号授权前在主页停留 15～30 秒（`SIGNUP_OAUTH_DWELL_MIN_SEC` / `SIGNUP_OAUTH_DWELL_MAX_SEC`）。
- ARCHITECTURE：`humanize.py` 是拟人交互原语统一入口；注册身份按邮箱存在 `data/chrome-profiles/.signup-identities/`。
- 入组欢迎页的按钮文案（`Get started` 等）是按现场截图和常见文案猜的；Part 2 若报 `onboarding_stuck`，看 `data/debug/` 截图补选择器。
