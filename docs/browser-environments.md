# 账号浏览器环境

注册、邀请入组、OAuth、独立重新授权通过同一个浏览器配置函数启动。轮转到其他 Team 不改变邮箱对应的浏览器档案；注册到 OAuth 的现有同页交接继续保留。

页面注册逻辑可独立选择 `BROWSER_SIGNUP_FLOW=legacy|extension`。`extension` 复用新版插件的页面填写代码，浏览器引擎、代理、档案与同页 OAuth 交接仍按本文件执行；详见 [项目内新版注册流程](managed-signup.md)。

## 启用

默认仍为 `chromium`，可通过 `.env` 启用 Chromix：

```dotenv
BROWSER_ENGINE=chromix
BROWSER_EXECUTABLE=/app/data/browsers/chromix/chrome
BROWSER_HEADLESS=False
BROWSER_LOCALE=en-US
BROWSER_TIMEZONE=America/Los_Angeles
```

路径必须指向已下载、校验并完整解压的浏览器可执行文件，且容器内可访问。示例路径不代表已安装。语言、时区按实际使用场景选择；不会按账号随机选择国家，也不会发起 GeoIP 请求。空时区在新 Chromix 档案中固定为 UTC，普通 Chromium 仍使用宿主时区。

`BROWSER_EXECUTABLE` 现在同时适用于独立重新授权和旧注册入口，不再只在邀请注册流程生效。CLI 的 `--browser-executable` 优先于环境配置，但不自动选择 Chromix 模式。指定文件时不再同时传入 Chrome channel。

代码不下载浏览器，不安装 Chromix SDK，也不会自动修改线上 `.env` 或重启服务。生产环境沿用自己的发布流程；本仓库的改动不表示线上已经开启 Chromix。

## 档案如何复用

- 普通 Chromium 保持原目录：`data/chrome-profiles/<email-with-_at_>/`。
- Chromix 使用独立目录：`data/chrome-profiles/chromix/<email-with-_at_>/`，保留旧 Chromium 的 Cookie 和浏览器数据。
- 首次创建时将随机非零 32 位种子、操作系统、语言、时区和窗口尺寸写入 `.team48-browser.json`。窗口从 `1200×720`、`1280×720`、`1200×800`、`1280×800` 中选择，适合当前 Xvfb 显示尺寸。
- 重试、重启、重新授权及同一账号轮转，始终读取原档案。不按任务 ID、Team ID 或日期重新抽取种子。
- 环境配置更改只影响新 Chromix 档案。已有档案保留其语言、时区及窗口尺寸；浏览器可执行文件和有头/无头模式仍按运行配置选择。
- 并发首次创建通过完整临时文件与原子硬链接发布，所有调用复用同一份档案。数据盘需要支持硬链接。
- 档案损坏、版本不支持、操作系统不符，或已有浏览器数据但档案丢失时停止，不自动删除数据或重新随机。备份必须包含整个账号目录，尤其是 `.team48-browser.json`。

首次把已有账号从普通 Chromium 切到 Chromix，会进入新的浏览器档案，可能需要重新登录。不会自动迁移 Cookie；不要把这次环境迁移当作无感切换。切回 `BROWSER_ENGINE=chromium` 会重新使用保留的旧目录。

环境标识仅用于诊断，不是账号 ID，也不能证明两个浏览器在所有可观察特征上都不同。

## 功能边界与版本

当前集成使用已发布 Linux x64 `152.0.7977.82`（上游源码 `921312ba41ac7053a8ac2a418761ed20b8dead74`）支持的种子、语言、时区参数，由 Playwright 直接启动，不依赖主分支 SDK 的新功能。上游归档 SHA256 为 `85593ab5bedbdceee2196f2d1e9c603a09cf18f5dfb9bcc949c4232e7b35295f`；校验的是整个 ZIP，不是解压后的 `chrome` 文件。

- 使用持久化种子启用该构建的指纹能力，并显式关闭合成 GPU 身份；不伪造 OS、CPU、内存或 WebRTC IP。
- 显卡仍可能是服务器的 SwiftShader。独立账号环境不等于物理设备隔离或完整真实设备库。
- Chromix 不叠加项目原来的 `navigator.webdriver` JavaScript 注入。普通 Chromium 的现有处理保持兼容。
- 浏览器继续走已有代理配置及 SOCKS5 认证桥。Chromix 限制非代理 UDP；这不代表已验证所有 DNS/ICE/操作系统流量路径。
- 无效 Chromix 可执行文件配置会在领取 HME / 发送邀请前阻止入驻。磁盘权限和已有档案损坏仍可能在浏览器启动阶段报告。
- `browser_environment` 进度记录包含配置摘要、窗口、语言和时区，不包含邮箱路径、原始种子或代理凭据。邀请流程保留该摘要；其他页面诊断仍按现有脱敏规则处理。

不会自动增加注册次数、解除轮转冷却或改变接码授权规则。浏览器环境稳定性测试不代表能减少手机号验证；需要区分注册阶段和 OAuth 阶段观察实际结果。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_browser_environment tests.test_browser_handoff tests.test_oauth_signup tests.test_invitation_flow tests.test_onboard tests.test_reauth tests.test_rotate tests.test_replenish tests.test_oauth_security -q
```

离线浏览器检查使用两个临时档案，重复启动第一个档案，然后启动第二个；检查 DOM 交互、窗口、语言、时区和同档案实际观测值的稳定性。代理指向不可用的本机端口，页面由本地 HTML 生成，不访问注册或 OAuth 页面。正常结束会清理临时浏览器和临时档案。

```bash
python -m scripts.browser_environment_smoke --engine chromix --browser-executable /app/data/browsers/chromix/chrome --headed
```

在现有 VPS 容器内，以内存加载的新启动配置和已安装的隔离 Chromix 做过有头离线验证：三个启动均成功；同档案重启后环境摘要和 Canvas 样本一致，另一档案使用不同种子，Canvas 样本不同；语言、时区和窗口符合配置。GPU 仍显示真实软件渲染器。没有注册账号、请求短信、修改正式应用文件或重新部署服务。该结果只覆盖这份二进制和探针场景，不是全部指纹接口的验证。
