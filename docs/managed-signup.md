# 项目内新版注册流程

项目可在现有 Chromium / Chromix 浏览器中运行 `extensions/chatgpt-signup/content.js` 的填写逻辑。无需手工安装扩展，也无需将自用扩展的邮箱密钥放进服务器。独立扩展的使用方式不变。

## 启用与回退

```dotenv
# 默认值。保留原注册流程。
BROWSER_SIGNUP_FLOW=legacy

# 新流程，显式启用。
# BROWSER_SIGNUP_FLOW=extension
```

`BROWSER_ENGINE`、`BROWSER_EXECUTABLE`、代理和有头/无头设置仍使用项目原配置。选择 `extension` 不会下载、安装或自动选择 Chromix。配置需要在实际运行浏览器 worker 的进程中生效；仅修改源码不代表线上启用。

新选项作用于 `run_browser_onboard(mode="register")`，包括普通注册和邀请注册；复用登录、独立重新授权及 OAuth 继续使用原入口。新流程失败时返回明确错误，不在同一个任务里自动换旧流程再注册。需要回退时，将后续任务的选项改回 `legacy`。

源码已完成离线验证。Bybit2 单账号真实测试使用 VPS 隔离目录，不代表线上服务已全局切换；过程与结果见 [实测记录](bybit2-managed-signup-trial.md)。

## 注册与授权交接

1. 项目提供当前账号的邮箱、密码和邮箱读取配置。浏览器仍按邮箱使用原持久化档案与代理。
2. 开始导航前读取旧验证码快照。邮箱请求在 Python worker 内执行，页面只收到本步所需的验证码，不接触邮箱管理密钥。
3. 共享页面脚本处理邮箱、密码、验证码、资料和 Continue。Continue 无响应时，等待门槛 2 秒，最多补点两次；加载、禁用、校验错误、字段变化和前台焦点检查继续生效。
4. 新版邀请注册先由官方 API 确认目标邮箱的邀请、角色与席位，再直接打开 `https://chatgpt.com/` 注册；不等待邀请邮件或邀请链接。ChatGPT 的无注册字段页面由脚本让出控制，项目核对登录态邮箱，再处理工作空间选择/接受。底层仍兼容显式提供的邀请链接入口，其登录后重开规则不变。
5. 只有 ChatGPT 主页标记可见且可操作、文档已加载、页面在前台，并且没有可见注册字段、加载指示、弹窗或待接受工作空间按钮，才开始完成检查。主页需保持这些条件至少2秒，且目标邮箱登录态的两次有效读取间隔至少2秒。导航、文档替换、短暂加载/弹窗、失去前台、身份读取失败或工作空间动作会重置检查。满足后移除初始化脚本、停止循环及全部监听器，再返回注册结果。
6. 原流程查询官方成员列表，确认角色与席位后，使用原 `InvitedBrowserSession` 的同一浏览器、页面和代理进入 OAuth。原 callback/state、token 邮箱检查及凭据保存逻辑保持生效。

登录到 ChatGPT 不等于加入 Team 或获得 Codex 授权。官方入组和 OAuth 各自的成功条件仍由原项目检查。

已读到登录态但30秒内主页仍未就绪时，返回 `registration_home_not_ready`，不启动 OAuth；账号与浏览器档案保留。这个超时仅限制注册收尾，不缩短注册/验证码阶段的原预算。共享填写脚本在 DOMContentLoaded 后启动，输入、点击和 Continue 补点算法不变。稳定时间用于验证页面状态，不是尝试用固定等待时长换取免手机验证。

交接诊断按顺序记录 `signup_wait_home`、`signup_workspace`（存在页面操作时）、`signup_home_ready`、`signup_identity_confirmed`、`signup_ready`、`signup_runner_stopped`。原任务继续记录 `reconciling`、`authorizing` 和 `oauth_same_session`。`signup_diagnostics` 只含固定阶段名、相对毫秒数、确认次数与工作空间动作次数；不含 URL、邮箱或凭据。Bybit2 实测脚本分别保留 `registration_result` 和 `oauth_result`，后者不再覆盖前者。

成功对照由用户确认：HubStudio ChroBrowser 内无痕窗口，与 VPS 同一代理出口，使用 48team 生成的授权链接。本轮仅修正托管收尾行为和诊断；不改变浏览器内核/无痕模式或 OAuth 参数。HubStudio 实机对照及免手机比例仍需后续实测，旧的已触发手机验证账号不能替代全新注册对照。

## 手机验证与失败续接

新注册阶段不发送或轮询短信。看到手机号页时返回 `phone_verification_required`；人机验证、限流、手工修改、异常页面也会停止，不换号、不循环重注册。OAuth 阶段沿用原来显式提供号码、且实际出现手机验证页才使用的规则。

父任务保留同一账号及原 HME 占用/部分成功处理。浏览器 worker 结束后关闭浏览器；下一次显式继续使用该账号的持久化档案，不承诺保留已结束进程中的实时页面。

提交计数、验证码占用和补点凭据在本次 Python worker 内跨页面保存，页面刷新不能重置。worker 终止后不会自动恢复或继续点击；新的显式任务仍由项目的账号/成员状态决定续接路径。

资料沿用随机英文姓名、22–45 岁生日的规则，第一次生成后保存在账号浏览器目录的 `.team48-signup-profile.json`，后续任务复用；档案损坏时停止，不重新随机。保存的数据只有姓名和生日，不含邮箱密钥、密码或 token。

## 隔离与部署资源

页面脚本运行在随机命名的 CDP isolated world 中，只绑定被管理页面的主 frame、HTTPS 允许域名和当前文档。普通网页 JavaScript、子 frame、其他域名不能调用宿主桥接取得任务密码。跨页面消息仍检查版本和任务 ID。

项目只需两份共享扩展资源：

- `extensions/chatgpt-signup/content.js`
- `extensions/chatgpt-signup/manifest.json`

`deploy/Dockerfile` 已添加这两份公开资源；`.dockerignore` 排除自用包目录和 `private-config.mjs`。邀请/HME 操作前检查资源与托管接口是否存在，缺失时停止。该检查只属于入驻流程，不阻止独立 OAuth 使用自己的浏览器入口。

新版桥接与状态模块位于 `app/integrations/openai/browser/signup.py`、`signup_state.py`、`signup_readiness.py` 和 `signup_bridge.js`。进度使用固定步骤描述，不保存页面错误原文、OTP、密码、邮箱管理密钥或完整邀请 URL。

## 离线验证

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_managed_signup tests.test_browser_environment tests.test_browser_handoff tests.test_oauth_signup tests.test_invitation_flow tests.test_onboard tests.test_replenish tests.test_oauth_security tests.test_reauth tests.test_rotate -q
.\.venv\Scripts\python.exe -m unittest tests.browser_managed_signup -v
node --test tests/signup_extension.test.cjs
```

单元测试使用模拟 API/数据库，覆盖官方邀请后无需邀请邮件从主页注册、邀请失败停止、注册登录后未确认入组不得 OAuth。浏览器测试使用本机 Chromium，将所有请求拦截到本地模拟页面，覆盖主页注册后接受待加入工作空间、显式邀请链接重开、两处 Continue 补点、身份不匹配阻止入组、手机号页停止、隔离桥接访问、填写中退出，以及退出后同一浏览器页面交给授权操作。另运行独立扩展的关键表单和自动/核对完整流程回归。

离线测试不能证明注册或 OAuth 不会要求手机号；真实运行结果以实测记录为准。尚未运行完整 Docker 镜像构建。

注册收尾优化的针对性验证：`tests.test_managed_signup`、`tests.test_browser_handoff`、`tests.test_oauth_signup` 共44项通过；`tests.browser_managed_signup` 本轮覆盖13个浏览器用例，均通过（其中表单返回的超时回归为追加执行）。新增覆盖有效登录态但页面未知、主页不可操作/有弹窗/加载、两次身份读取之间短暂加载、迟到弹窗、邮箱变化、未验证邮箱、返回注册表单不耗尽收尾预算，以及停止脚本后同页交接。此优化尚未部署到 VPS 或在 HubStudio 上进行真实账号对照。
