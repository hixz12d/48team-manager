# 计划：服务器换用 Camoufox 反检测浏览器并拟人化注册入组

## 需求

上一计划（桌面版快捷授权入口）实测证明：手工注册的账号走服务器自动"登录 + 桌面版授权"不触发手机验证；但服务器自动化**注册**出来的账号会被 OpenAI 在注册阶段标记为机器人账号，之后无论服务器还是本机手工授权 Codex 都必跳 `add-phone`。此外，新受邀账号首次进入 `chatgpt.com` 会停在 `/onboarding` 欢迎卡（`Get started`），未点击确认入组就去授权会导致拿到的令牌只是个人空间（`not_team_workspace`）。

对比现有代码与 GitHub 同类项目后确认：纯 HTTP 协议注册项目同样普遍触发 Codex 手机验证（靠付费接码硬过），而我们现有服务器注册代码除使用普通 Chromium + CDP 外，还在验证码与 `about-you` 资料页使用了 0 毫秒瞬间赋值、匀速机械打字、注册后 2 秒立即跳授权，并漏掉了点击 `/onboarding` 入组欢迎卡。

做完后：服务器 Playwright 浏览器支持一键切换为 **Camoufox（Firefox 反检测内核）**，所有填表与点击改为真人鼠标轨迹和不均匀逐字敲击，登录或注册后自动点掉入组欢迎卡确认进入 Team 空间，新号在主页自然停留 15～30 秒后再发起桌面版授权并导入 codex-rs，让用户在 rs 管理台粘贴已发邀请的新邮箱即可免手机全自动完成"注册 + 入组 + 授权导入"。

## 对齐结论

| 事项 | 结论 |
|---|---|
| 覆盖范围 | 服务器 Playwright 浏览器流程统一支持 `BROWSER_ENGINE=camoufox`（rs 粘贴新邮箱自动授权、服务器邀请/补位注册、rs 掉登录恢复、单号重授权、备用号池拉入共用，保留随时切回 `chromium`）；本机 Chrome 插件与 Chromix 扩展运行器不动 |
| 注册方式 | 保持无密码、纯邮箱一次性验证码注册（手工无密码注册已实测不撞手机，且后续掉登录恢复无需管理密码） |
| 拟人化操作 | 启用 Camoufox 原生真人鼠标轨迹（`humanize=True`）+ 按钮随机偏移点击；邮箱、验证码、姓名、生日全部改为不均匀节奏逐字敲击，页面与步骤间加 1～3 秒随机阅读停顿，彻底移除 `fill()` 瞬间赋值和固定间隔打字 |
| 自动入组欢迎页 | 登录 `chatgpt.com` 后自动识别 `/onboarding` 及工作空间欢迎卡（`Get started` / `Accept invite` / `Join workspace` 等）并点击进入；多 Team 场景按指定团队名或唯一非个人空间校验，多个 Team 无法唯一定位时停下交人工 |
| 注册后停留节奏 | 本次新建账号完成资料与入组进入主页后，在主页随机自然停留 **15～30 秒**（可配置）再同窗打开桌面版授权链接；已注册老号重授权只做 2～4 秒短停顿 |
| 实验与备选退路 | 上线后用 2 个全新未碰过、已发 Team 邀请的 HME 邮箱实测（并可顺带重试 `house-reducer-0y@` 验证入组点击），全程不自动接码；若服务器自动注册仍撞手机，不接手机号池，另起方案用本机 HixBrowser（真显卡 Camoufox）只跑前半段注册、服务器跑后半段入组与授权（本计划不写本机脚本） |

**按常规处理的细节**：
- 扩充英文姓名词库（60×60 以上常见英文名组合，告别重名 `James Smith`）配 22～45 岁随机成年生日；同一邮箱按本地档案固定同一套姓名生日，重试不换身份
- Camoufox 版本固定为 `camoufox[geoip]==0.5.9`（与 HixBrowser 一致），Docker 构建时预下载浏览器内核与 GeoIP 库，运行时绝不联网下载
- 每个邮箱独立持久档案与固定指纹 `data/chrome-profiles/camoufox/<邮箱>/`，同一邮箱多次启动保持同一套 UA、屏幕、硬件并发与 WebGL 指纹，不覆盖原 Chromium / Chromix 档案
- 每次启动按代理出口 IP 自动对齐时区、地理位置与 WebRTC（IPv4 伪装为出口 IP 并禁 IPv6，IPv6 或未知 IP 时屏蔽 WebRTC 并按出口时区设置），界面语言固定 `en-US`
- Firefox 代理显式绕过本机回环（`localhost, 127.0.0.1, ::1`），确保 `http://127.0.0.1:1455/auth/callback` 回调被 Playwright `route` 正常截获
- Camoufox 引擎下服务器 `mode="register"` 统一走拟人化 Playwright 循环，不走仅支持 Chromium CDP 的 `run_managed_signup`
- 遇到 Cloudflare / CAPTCHA 人机验证或手机验证页（不接码流程）照旧保存截图到 `data/debug/` 并停下交人工，不做任何验证破解

**不做的事**：
- 不照搬纯 HTTP 协议逆向注册，不做任何 Cloudflare Turnstile / CAPTCHA / Sentinel 破解或第三方打码接入
- 不在 rs 新号与掉登录恢复流程接入手机号池自动接码
- 不改动本机 Chrome 插件（`extensions/chatgpt-signup`）和 Chromix 扩展运行器（`extension_runner.py`）
- 本计划先不编写本机 HixBrowser 批量注册脚本（仅在 part-2 实测仍撞手机时再决定是否另开计划）

## Part 总览

<!-- /flow-plan 填写。进度只看各 Part 文件的状态行，本表写好后不改。 -->

| Part | 内容 | 依赖 | 可以同时做的 Part |
|---|---|---|---|
| part-0-camoufox-engine | 安装预置 Camoufox 0.5.9 与地理库、新增 `BROWSER_ENGINE=camoufox` 与停留配置、实现持久指纹与统一浏览器启动器及冒烟脚本 | 无 | — |
| part-1-humanized-signup-flow | 新建拟人输入模块、去掉瞬间填表并扩充持久姓名库、自动点 `/onboarding` 入组欢迎卡、新号主页停留 15～30 秒再授权 | part-0 | — |
| part-2-live-run | 部署启用 `BROWSER_ENGINE=camoufox` 后容器冒烟与 2 个全新 HME 邮箱实测验收 | part-0、part-1 + 已部署 | — |

**执行顺序**：先做 `part-0`；完成后做 `part-1`；然后运行 `/flow-ship` 构建镜像并部署上线；最后做 `part-2` 生产实测。

## 上线注意

- `deploy/Dockerfile` 新增了系统依赖与 `python -m camoufox fetch` 预下载（镜像体积会明显增大，首次构建较慢），上线必须带 `--build` 重建镜像。
- 上线后在 VPS `/opt/team48/.env` 增加 `BROWSER_ENGINE=camoufox`（如需微调主页停留秒数可设 `SIGNUP_OAUTH_DWELL_MIN_SEC=15`、`SIGNUP_OAUTH_DWELL_MAX_SEC=30`）再重建容器生效；如需紧急回退，改回 `BROWSER_ENGINE=chromium` 重建即可，原 Chromium 档案完好保留。
- 实测通过前，保持自动轮转、自动补号、自动重授权开关关闭；不要再用已被 OpenAI 标记的旧测试号（`panda.liable-4i@`、`banking0calmer@`、`myriad-capote.2v@`）评价注册效果。
