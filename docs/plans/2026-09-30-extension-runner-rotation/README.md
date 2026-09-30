# 计划：轮转改由服务器上的指纹浏览器 + 真插件完成注册与授权

## 需求

服务器轮转注册的新号，在 OAuth 授权时经常被要求手机验证；同一个插件在本机 HubStudio 指纹浏览器里跑则基本免手机。原因是服务器浏览器和操作方式都很"假"：时区为上海但出口在美国、显卡是 SwiftShader、Linux 自带 Chromium、Playwright 远程驱动（`Runtime.enable`）、键鼠事件简化、授权页用 Playwright 直接 `fill`。

改成：轮转到注册这一步时，服务器用 **Chromix 指纹浏览器（普通子进程，不接 Playwright / CDP）** 加载**同一份插件**，由服务器给插件派发任务；插件完成注册和同页 OAuth，把回调交给服务器换票；之后轮转照旧推送 Sub2API、删旧号远端、计数。另外提供"浏览器环境自检"，在不碰官方团队的前提下看指纹检测结果。

## 对齐结论

| 事项 | 结论 |
|---|---|
| 路线 | 服务器跑真插件（不再用 Playwright 驱动注册 / OAuth），浏览器用 Chromix 153 |
| 适用范围 | 手动轮转、自动轮转；由设置开关 `ROTATION_SIGNUP_RUNNER=extension` 启用，默认 `playwright`（旧路径），可随时切回 |
| 插件在服务器模式下 | 不推送 Sub2API、不计数、不走 resolve/handoff；这些仍由轮转负责 |
| 手机 / 人机验证 | 插件停下 → 服务器关浏览器 → 轮转 `manual_required`，保留同一邮箱；人工授权后"继续轮转"（沿用已上线续接）。自动轮转也不再用号码池接码 |
| 出口 IP | 继续用母号代理（冻结在任务上的 `resolved_proxy`） |
| 时区 | 建档时经代理查出口 IP 所在地，写入该号档案并固定 |
| 伪装平台 / 显卡 | 支持 linux / windows 两种平台、原生 / 预设显卡两种；由自检结果决定生产默认值 |
| 验证方式 | 先跑"环境自检"（不注册、不碰官方团队），再由用户挑团队实跑一次手动轮转 |
| 用户本机插件 | HubStudio 用法、弹窗、Team48 接入全部不变；服务器模式只在存在 `RUNNER` 配置时生效 |

**按常规处理的细节**：
- 插件的邮箱配置和本次运行令牌由服务器在启动前生成到本次运行目录，结束即删；不进镜像和仓库；令牌按次随机，不复用 `EXTENSION_API_TOKEN`。
- 插件与服务器通信走容器内 `http://127.0.0.1:8008`，不经代理、不走公网。
- 服务器总等待上限 25 分钟；插件心跳超过 120 秒没来、浏览器进程退出都算失败，关浏览器标待人工。
- 全局仍只有一个浏览器执行槽，沿用 `InvitedBrowserSession` 的预留。
- 每次运行把环境摘要（引擎、伪装平台、时区、出口 IP 所在国，不含密钥和种子原值）和插件脱敏诊断写进任务步骤。
- 带账号密码的 HTTP 代理 Chromix 子进程无法直接使用：预检时直接拒绝（`proxy_auth_unsupported`），在踢人前停下；SOCKS5 走现有认证桥。
- 新号浏览器档案放 `data/chrome-profiles/runner/<email>/`，与旧 Chromium / Chromix 档案分开。

**不做的事**：邀请、补位、控制台注册、独立重新授权、自动重授权仍走现有方式；不迁移旧号档案；不解决"显卡底层仍是服务器软件渲染"（只能改显示的型号）；不保证 100% 免手机；不删除旧的托管注册代码（邀请 / 补位还在用）。

## Part 总览

| Part | 内容 | 依赖 | 可以同时做的 Part |
|---|---|---|---|
| part-0-contracts | 插件↔服务器协议文档、配置项、运行器档案与 Chromix 启动参数、出口地理查询、运行器函数桩 | 无 | — |
| part-1-extension | 插件增加"服务器模式"：自动领任务、免无痕、状态上报、按服务器指令授权并回交回调、自检探针 | part-0 | part-2、part-3 |
| part-2-runner | 服务端运行器：生成运行目录、启动 Chromix 子进程、运行登记与等待、`/api/ext/runner` 接口、入组确认后发授权链接、换票 | part-0 | part-1、part-3 |
| part-3-rotation | 手动 / 自动轮转接入运行器（预检、注册授权段替换） | part-0 | part-1、part-2 |
| part-4-selfcheck-integration | 环境自检任务与界面、任务类型登记、路由挂载、Dockerfile / Compose | part-1、part-2、part-3 | — |

**执行顺序**：先做 part-0；完成后开 3 个窗口同时做 part-1、part-2、part-3；最后做 part-4。

**与其他计划的冲突**：`2026-09-30-team-revenue-ledger` 的 part-2 也改 `app/web/routes/api.py`、`app/web/static/js/app.js`、`app/web/templates/console.html`，本计划 part-4 不能和它同时做，先完成其中一个。该计划已完成的 part 在 `manual_rotation.py`、`rotate.py` 等文件留有未提交改动，本计划 part-3 必须保留这些改动。

## 上线注意

- 部署前按 RUNBOOK 备份数据库和镜像；确认自动轮转关闭、无进行中任务。
- VPS 下载 Chromix `153.0.8010.36` 的 `chromix-linux-x64.zip`（发布页 https://github.com/lwhx/Chromix ），核对发布页给出的 SHA256 后解压到 `/opt/team48/data/browsers/chromix153/`（容器内 `/app/data/browsers/chromix153/`）。
- `/opt/team48/.env` 新增：`ROTATION_SIGNUP_RUNNER=playwright`（先不开）、`RUNNER_BROWSER_EXECUTABLE=/app/data/browsers/chromix153/chrome`、`RUNNER_FINGERPRINT_PLATFORM`、`RUNNER_GPU_MODE`。自检通过后再改成 `extension`。
- 镜像变更：Xvfb 改 1920×1080、镜像内带完整插件（仍排除 `private-config.mjs`）。需要 `--build`。
- 上线后先跑自检（linux / windows 各一次），看结果定平台和显卡，再由用户挑团队实跑手动轮转验收。
- 收尾时更新 `docs/ARCHITECTURE.md`（运行器模块、配置项）、`docs/RUNBOOK.md`（Chromix 安装、自检、切换 / 回退）、`docs/PRODUCT.md`（轮转规则里"自动轮转不再接码"）、插件 README 版本号。
