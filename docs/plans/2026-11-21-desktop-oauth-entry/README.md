# 计划：授权改走 Codex 桌面版快捷入口，避开手机验证

## 需求

48team 的浏览器自动授权（rs 邮箱自动授权新号、rs 掉登录恢复、号池拉入、轮转等）几乎必定撞"添加手机号"页；用户手工用 codex-rs 生成的链接，在已登录 chatgpt.com 的浏览器里点选账号就能授权，新号、机房 IP、任何浏览器都不撞。

对比源码后的判断（推测，靠本计划的实验验证）：48team 直接打开 `auth.openai.com/oauth/authorize` 并带 `prompt=login`，强制在授权页重新登录、重走风控；rs 走 `chatgpt.com/codex/desktop-auth`（Codex 桌面版快捷登录），沿用 chatgpt.com 已有登录。

做完后：所有授权链接改成桌面版入口；自动化流程先在 chatgpt.com 验证码登录，再在同一浏览器打开授权链接、选对应账号和 Team；用户在 rs 管理台粘贴新邮箱即可自动授权导入，不再撞手机（以实验结果为准）。

## 对齐结论

| 事项 | 结论 |
|---|---|
| 授权链接 | 照抄 rs 桌面版入口参数（外层 `chatgpt.com/codex/desktop-auth`，内层无 `prompt=login`、无 `login_hint`，`originator=Codex Desktop`、带版本号和稳定 ID、回调 `http://127.0.0.1:1455/auth/callback`、scope 同 rs），由 48team 自己生成、持有 PKCE、自己换令牌 |
| 换令牌后核对 | 保留现有核对：邮箱、用户、Team 工作区、有刷新令牌，全部一致才写入 / 导入 |
| 浏览器流程 | chatgpt.com 邮箱验证码登录（新号顺带填 about-you）→ 确认会话就绪 → 同一 context 打开桌面版链接 → 选账号页按邮箱点对应账号卡 → 选唯一 / 指定 Team → 同意 → 截获 127.0.0.1:1455 回调 |
| 范围 | 所有生成授权链接、走浏览器授权的地方都换：rs 邮箱自动授权新号、rs 掉登录恢复、codex 单号邮箱重授权、号池拉入、轮转、账号页"重新授权"链接、插件拿到的链接 |
| Sub2API | 用户已不再用 Sub2API 反代账号，全部接 codex-rs；不做 Sub2API 续期核对。`CODEX_OAUTH_ENTRY=legacy` 保留为通用流程的退路（rs 方向三个流程固定用桌面版） |
| 桌面版版本号 | 会随官方更新变：每 30 分钟从 codex-rs 读取当前值，读不到沿用上次值，从未读到才用配置兜底 |
| 实验 | 改完后用用户给的 2 个"已拉进 Team、未注册"的新邮箱，在服务器跑 rs 邮箱自动授权新号；成功即正式导入 rs。不测设过密码的老号 |
| 仍撞手机 | 换 Camoufox（Firefox 反检测内核，Linux 可用，Playwright 驱动）再试；仍不行就停下写原因交人工。全程不自动接码 |

**按常规处理的细节**：
- 用户入口不变：rs 管理台"添加账号 → 邮箱自动授权"
- 换令牌的 `redirect_uri` 与授权时逐字一致（`127.0.0.1`）；回调解析同时接受旧的 `localhost` 回调，兼容改版前已生成的会话
- 桌面版版本号做成配置项，不写死
- 浏览器时区跟随代理出口；WebRTC 只走代理；抹掉 `navigator.webdriver`
- 验证码填完先等网页自动提交，几秒后仍停在原页且按钮可点才补点一次
- 选账号页永不点"使用其他账号"
- 撞手机 / 人机验证照旧停下写明原因

**不做的事**：
- 不接码硬过（rs 新号与恢复流程保持不接码；号池 / 轮转原有的接码逻辑不动）
- 不改 rs（codex-proxy-rs）代码，不用 rs 的 `oauth/start` / `oauth/complete`
- 不做远程接管浏览器界面
- 不测设过密码的老号

## Part 总览

| Part | 内容 | 依赖 | 可以同时做的 Part |
|---|---|---|---|
| part-0-desktop-link | 桌面版链接生成、入口开关与版本号配置、回调改 127.0.0.1（兼容 localhost）、浏览器时区 / WebRTC / webdriver | 无 | — |
| part-1-server-browser-flow | 服务器 Playwright：先登 chatgpt.com 再开链接、选账号卡、不点换号、验证码不重复提交；rs 三个流程显式用桌面版，新号允许填 about-you | part-0 | part-2 |
| part-2-extension | 插件和插件运行器接受桌面版链接、desktop-auth 页处理、验证码等待、版本 0.9.0 | part-0 | part-1 |
| part-3-live-run | 部署后用 2 个新邮箱实测 rs 自动授权 | part-0/1/2 + 已部署 | — |
| part-4-camoufox-fallback | 备选：仅 part-3 仍撞手机时，换 Camoufox 内核 | part-3 结论 | — |

**执行顺序**：先做 part-0；完成后开 2 个窗口同时做 part-1、part-2；然后 `/flow-ship` 部署；再做 part-3；part-3 结论要求时才做 part-4（之后再部署复测）。

## 上线注意

- 新配置项：`CODEX_OAUTH_ENTRY`（默认 `desktop`）、`CODEX_DESKTOP_APP_VERSION`（默认值见 part-0 完成记录），需要时写进 `/opt/team48/.env`
- 已有回归测试里断言旧链接参数（`prompt=login`、`login_hint`、`auth.openai.com` 来源）的会失效，本计划不维护测试
- 插件改了要重新打包并在本机重新加载，核对版本号
- 上线后先跑实验（2 个新邮箱），通过前不开任何自动开关
