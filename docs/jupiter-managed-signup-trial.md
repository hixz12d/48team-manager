# Jupiter 1 优化后实测（2026-09-19）

结果：新子号已注册并正式加入 Jupiter 1，Owner + Premium；同浏览器页面继续 OAuth 时仍出现 `auth.openai.com/add-phone` / `Phone number required`。未发送短信，无 OAuth refresh token。本轮出现注册资料页暂停，使用同邮箱续接并补一次受控最终按钮点击后才完成注册，不能视为纯自动成功或单一变量的免手机对照。

## 授权范围与环境

用户明确选择保留母号 `hixz2611@gmail.com`，替换额度用尽的 `muckier_oak7w@icloud.com`，领取一个新 HME 测试注册→入组→同页 OAuth，遇手机验证停止。

- Workspace 11 / Jupiter 1，官方 ID `617649a2-2463-4e4f-bab8-ab145aa8a1bf`。
- 母号账号26保留；旧子账号296、官方用户 `user-IiMNiOSyZruI2FPlGzEGbmsI` 已移除，本地转 standby。
- 新 HME `49_nonslip.banks@icloud.com` / 账号305，只领一次，只发一次邀请。
- 隔离目录 `/opt/team48/data/experiments/managed-signup-jupiter-20260919T131650Z`，容器内 `/app/data/experiments/managed-signup-jupiter-20260919T131650Z`。
- 复用经包和可执行文件校验的 Chromix；有头、持久化档案、团队原代理。没有改成 HubStudio 无痕，因此仍不是用户成功环境的完整复现。
- 已备份 SQLite 并校验上传代码；浏览器离线完成条件烟测通过，收信读取通过，真实授权登录页 HTTP 200。未替换线上应用文件、重启服务或改 Sub2API。

## 实测过程

1. `136fb3c7fd45`：官方确认旧成员移除，领取新别名并邀请 Owner + Premium；主页注册完成邮箱验证码，资料页暂停。`registration_manual_required`，当时仅母号在组、新子待邀请。
2. `66cc0fd1a522`：同账号、同持久化档案，从现有 `/about-you` 页续接并采集诊断。姓名年龄填写完成，记录到两次 `form_submit`，没有 profile 点击尝试，45秒后 `submission_timeout`。截图最终按钮 `Finish creating account` 可用，未显示校验或手机要求。没有证明这些 submit 事件已获服务端接受。
3. `13acc9ce9e3d`：继续同账号，填完后检查值匹配既有资料、表单有效、按钮启用且无加载，稳定3秒后仅执行一次受控 Playwright 最终按钮点击，记录 `trial_profile_submit`。这属于诊断干预，未改共享插件算法。
4. 13:27:40 UTC 识别主页；13:27:42 主页可操作；13:27:45 两次间隔身份确认、稳定4256ms、注册脚本停止。13:27:46 同浏览器同页 OAuth；13:27:52 手机必填，停止。

## 最终独立回读

- 官方2人：母号 `hixz2611@gmail.com` Owner + Standard，新子 `49_nonslip.banks@icloud.com` Owner + Premium（`user-oLchoWzn0eMd9aw6lNyxA8RL`）。待邀请0。
- 新子 active / joined / oauth_required；有网页登录 access token，无 OAuth refresh token，无保存手机号。
- 任务 `13acc9ce9e3d` partial / `phone_verification_required`；OAuth session failed且已消费。
- HME 本地标签 `Jupiter 1` 精确查询回读成功，无租约；未改 Apple 原生标签。默认 `/api/aliases` 只返回500条，改标签后目标不在首页，后续报告应按邮箱查询，不能以首页缺失认定别名不存在。
- 新子外部绑定0；试验浏览器和运行脚本均已退出；没有短信、Sub2API推送、再次删除成员或领取其它新别名。

证据：`dist/jupiter-verified-report.json`、`dist/jupiter-profile-paused.png`、`dist/jupiter-oauth-phone.png`。原始截图与档案保留在隔离目录；不把 Cookie、密码、OTP、完整授权URL写入报告。

## 后续边界与方向

保留账号305，不重复注册或换号。用户已反馈继续VPS试验无效，希望完整复现本人注册全过程。优先使用其成功的本机 HubStudio ChroBrowser 无痕环境和原版插件，核对运行版本、原代理、工作空间选择与手动授权动作；不要把CDP注入填写代码视作完整扩展复现，不通过继续增加等待时间猜测免手机原因。

本轮只能说明页面稳定门禁确实运行且未消除这次手机号要求，不能排除浏览器环境、账号历史或其它因素。资料页 submit 事件误判为完成提交的问题仍待独立修复与回归。
