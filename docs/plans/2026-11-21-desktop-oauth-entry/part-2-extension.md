# Part 2：注册插件和插件运行器支持桌面版链接

**状态**：已完成
**依赖**：part-0 已完成
**所属计划**：[README](./README.md)

## 目标

插件（本机包和服务器插件运行器共用 `extensions/chatgpt-signup`）能接受并走完桌面版链接：注册 / 登录后在同一标签页打开 `chatgpt.com/codex/desktop-auth?...`，按邮箱选账号、选 Team、同意，截获 `127.0.0.1:1455` 回调交回服务器。验证码填完先等自动提交。版本号升到 0.9.0。

## 可改文件

- `extensions/chatgpt-signup/background.js`
- `extensions/chatgpt-signup/content.js`
- `extensions/chatgpt-signup/shared.mjs`
- `extensions/chatgpt-signup/runner.mjs`
- `extensions/chatgpt-signup/manifest.json`
- `extensions/chatgpt-signup/README.md`
- `app/application/extension_runner.py`
- `docs/contracts/extension-runner.md`

## 只读参考

- Part 0 的链接格式（`app/integrations/openai/chatgpt.py:create_oauth_authorize_url`）
- `app/application/member_handoff.py:416-431`（`/handoff` 返回链接的来源）
- `scripts/build_signup_extension.py`（打包，读 manifest 版本号）

## 要点

- **链接来源检查**：`background.js:532` 现在要求链接 origin 是 `https://auth.openai.com`，`runner.mjs:11`（`AUTH_ORIGIN`）和 `:206` 同样。改成接受两种：`https://auth.openai.com/oauth/authorize...`，或 `https://chatgpt.com/codex/desktop-auth?...` 且其 `authorize_url` 参数解出来是 `https://auth.openai.com/oauth/authorize`。其他一律拒绝（保持原有的安全意图）。
- **页面分类**：`shared.mjs:134-137` 把所有 chatgpt.com 页面归为 `chatgpt`。新增一类（如 `desktop_auth`）专指 `chatgpt.com/codex/desktop-auth`；`background.js:1132` 在 oauth 阶段忽略 chatgpt.com 的 `complete` 消息——对 `desktop_auth` 页不能忽略。
- **桌面版页面操作**（`content.js` 的 `oauthPage` 一带 1224-1317）：
  - 选账号：点包含目标邮箱的账号卡 / 按钮；只看到别的邮箱时停下报 `identity_mismatch`。**不点**"使用其他账号 / Use another account / Switch account / Log out"。
  - 桌面版链接打开后被要求在 auth.openai.com 输邮箱 / 密码：说明会话没被认，停下报 `desktop_session_missing`，不在授权页重新登录（本机包保留原来的人工步骤提示即可）。
  - 选 Team、同意沿用现有逻辑；`content.js:1273` "不在 chatgpt.com 才点 Log in"的判断要确认不会在 desktop-auth 页误点 Log in。
- **验证码**：`content.js:1879-1891` 填完后等最多 3 秒，页面离开验证码页就不再点提交；仍在原页且按钮可点才补点一次。
- **回调**：`background.js:1300-1305` 过滤器、`shared.mjs:62-66` 已同时接受 `localhost` / `127.0.0.1`，核对一遍即可。
- **运行器服务端**：`extension_runner.py:739`（`_begin_authorize`）链接来自 Part 0 的生成函数，跟随配置；`:247-260` 回调主机已兼容。检查有没有别处校验链接 origin，有就同样放宽。
- **版本号**：`manifest.json:5`、`content.js:4`、`README.md` 第 1、20、78 行，0.8.9 → 0.9.0。
- **契约文档**：`docs/contracts/extension-runner.md:170,180,252` 的链接 / 回调示例改成新格式。
- 不改插件的接码、注册、团队识别逻辑。

## 步骤

1. 读 `background.js`（500-700、1100-1350）、`content.js`（860-880、1200-1340、1870-1900）、`shared.mjs`、`runner.mjs`。
2. 改链接来源检查（background / runner）。
3. 加 `desktop_auth` 页面分类和对应处理（选账号、不点换号、会话未认就停）。
4. 改验证码提交等待。
5. 核对 `extension_runner.py` 有无 origin 校验；改契约文档；升版本号。
6. 跑基本检查；用 `python scripts/build_signup_extension.py --local-config --unpack` 确认能打包（产物在 `dist/`，不提交）。

## 完成标准

- [x] 插件接受桌面版链接，拒绝其他非官方链接
- [x] desktop-auth 页按邮箱选账号，代码里没有点"使用其他账号"的逻辑
- [x] 验证码填完先等自动提交
- [x] 版本号 0.9.0 四处一致，能打包
- [x] 基本检查通过：对 `extensions/chatgpt-signup/*.js`、`*.mjs` 逐个 `node --check`；`.venv/Scripts/python.exe -m compileall -q app`

## 完成记录

- 由 Gemini（Neon high）施工，Claude 验收。
- `shared.mjs`：新增 `validAuthorizeUrl` / `isDesktopAuthorizeUrl`（只放行 `auth.openai.com/oauth/authorize`，或内层是它的 `chatgpt.com/codex/desktop-auth`）；`pageKind` 新增 `desktop_auth`；暂停码新增 `identity_mismatch`、`desktop_session_missing`。
- `background.js` / `runner.mjs`：改用 `validAuthorizeUrl` 校验；handoff 带 `desktopAuth` 标志传给页面；两个新暂停码在服务器模式是人工类（最终）。
- `content.js`：desktop-auth / 选账号页按目标邮箱点账号卡，只见别的邮箱就停 `identity_mismatch`，不点任何换号 / 退出；桌面版链接落到 auth.openai.com 的登录表单就停 `desktop_session_missing`；验证码填完等 3 秒自动提交，仍在原页且按钮可点才补点；desktop-auth 页不算"主页"。
- **偏离计划**：`background.js` 的 `complete` 消息是"注册完成"信号，OAuth 完成走回调截获；在 oauth 阶段放行会误结束任务，所以保持 oauth 阶段忽略，只把判断改为 `pageKind === 'chatgpt'`（desktop-auth 页不发"注册完成"）。合理，已采纳。
- `extension_runner.py` 无 origin 校验，未改；服务端不校验暂停码枚举（未知码走 `runner_paused_<码>`），不影响。
- 打包：`build_signup_extension.py --local-config --unpack` 产出 `dist/team48-chatgpt-signup-0.9.0-personal.zip` 并更新 `dist/chatgpt-signup/`；脚本按设计重写了 `private-config.mjs`（git 忽略，三项配置都在）。
- 上线要做：本机插件在 `chrome://extensions` 重新加载 `dist/chatgpt-signup` 并确认版本 0.9.0；服务器运行器随镜像带上新插件源码。

