# ChatGPT 注册助手 · 自用版 0.6.0

在无痕窗口打开插件，**输入邮箱地址 → 点击开始注册**。Cloudflare 接口、收件箱、管理密钥、邮件解析和默认注册资料已内置在 `private-config.mjs`，不需要配对码、后台登录或保存设置。

## 做什么 / 不做什么

- 做：在 ChatGPT 网页自动完成注册（邮箱、密码、验证码、姓名、年龄/生日），通过 `chrome.debugger`（CDP）由浏览器生成键鼠输入；从 Cloudflare 收件箱（地址见 `private-config.mjs`）按收件人精确匹配读取新验证码；配置 Team48 令牌后，注册完成可自动接入团队并完成 Codex 授权。
- 不做：不创建邮箱或 HME 别名、不发团队邀请、不写 iCloud；不接入或授权母号；不保存或导出 ChatGPT 令牌；日志不上传。输入的邮箱必须已能转发到内置收件箱。
- 人机验证、手机验证、限流、网页报错或未识别页面会暂停并说明原因，处理后点「继续」。
- 项目内托管注册复用本插件 `content.js`，见项目 `docs/ARCHITECTURE.md`。
- 服务器模式由 Team48 自动使用，本机无需设置。

## 安装 / 更新

1. 解压自用包，用其中 `chatgpt-signup` 文件夹覆盖原插件目录；首次安装则在 `chrome://extensions` 打开开发者模式，加载该文件夹。不要同时加载多个版本目录。
2. 在 `chrome://extensions` 点插件的「重新加载」。
3. 关闭旧的注册标签页，再开始新任务。
4. 确认插件弹窗（以及未隐藏的网页进度卡片）显示 **0.6.0**。

HubStudio（ChroBrowser）：在该环境的 `chrome://extensions` → 本插件详情 → 开启「允许在无痕模式下运行」，再按 `Ctrl+Shift+N`，在新开的无痕窗口使用插件。

推荐在环境「启动参数」里加上（多开时被遮挡的窗口不降速，并隐藏「正在调试此浏览器」提示条）：

```
--disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-background-timer-throttling --silent-debugger-extension-api
```

## 操作模式

| 模式 | 行为 | 时限 |
| --- | --- | --- |
| 自动填写并提交（默认） | 自动填写和点击；检测到你手工改字段时暂停。 | 10 分钟 |
| 自动填写，手动提交 | 每步填好后等你点网页按钮。 | 30 分钟 |
| 手工填写，确认后由插件提交 | 不填写、不取邮件；你填好后点进度卡片「已填好，提交本步」。 | 30 分钟 |
| 全程手工，仅记录流程 | 不填写、不点击、不取邮件，只观察步骤和最终登录状态。 | 30 分钟 |

- 自动模式可展开「注册资料」指定姓名与生日，留空则随机；可勾选隐藏网页进度卡片，在弹窗中暂停/继续/停止。
- 暂停和刷新会保留任务；不要在注册中途切换模式。暂停时可用「刷新网页并继续」。
- 完成后可复制脱敏诊断记录（不含邮箱、密码、验证码、Cookie、令牌或页面原文）。

## Team48 接入（可选）

打包时带上 Team48 令牌后，弹窗多出「注册完成后自动接入团队」，默认「自动识别邀请的团队（推荐）」。注册完成后插件依次：同步团队成员 → 确认已加入后接入本地 → 在同一标签页完成 Codex 授权 → 拦截 `localhost:1455` 回调交给 Team48 换票 → 推送 Sub2API、今日切换次数 +1。识别不到或匹配多个团队时停下，由你手动选团队后点「重新接入」。

插件调用 `/api/ext` 下 5 个接口：`GET /ping`、`GET /workspaces`、`POST /resolve`、`POST /handoff`、`POST /handoff/complete`。令牌不能读取账号凭据，也不能删除或邀请成员；不要外传自用包。

开启方法：

1. 生成令牌：`python -c "import secrets; print(secrets.token_urlsafe(32))"`。
2. 服务器 `/opt/team48/.env` 写入 `EXTENSION_API_TOKEN=<令牌>`（至少 24 位，否则 `/api/ext/*` 一律 404），然后在 `/opt/team48` 重建：

   ```bash
   docker compose -p team48 -f deploy/docker-compose.yml up -d --build --no-deps team48
   ```

3. 本机打包时加 `--team48-token <令牌>`（见下节），再到 `chrome://extensions` 重新加载；浏览器可能要求确认新增权限。

## 打包

```powershell
python scripts/build_signup_extension.py --local-config --unpack
```

- 用本机已有的 `private-config.mjs` 生成 ZIP 并更新 `dist/chatgpt-signup`，不连接 VPS。
- 首次没有本机配置时，不带 `--local-config` 运行：只读 VPS 上既有的 Cloudflare 配置，不修改或部署服务器。
- `--team48-token <令牌>` 写入 Team48 令牌（之后用 `--local-config` 重打包会保留）；`--no-team48` 移除令牌。
- 配置文件和自用 ZIP 不进入 Git。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 弹窗版本号不是 0.6.0 | 确认覆盖的是正确目录，重新加载插件，关闭旧注册页。 |
| 无痕窗口里没有插件 | 插件详情里开启「允许在无痕模式下运行」。 |
| 多开时窗口卡住、按钮等不到 | 加上面的 HubStudio 启动参数；未提交前的等待类暂停会自动重试。 |
| 页面顶部出现「正在调试此浏览器」 | 加 `--silent-debugger-extension-api`；点提示条取消会暂停任务。 |
| 一直等验证码 | 确认邮箱能转发到内置收件箱；120 秒没新邮件会暂停，在网页点重新发送后继续。 |
| 自动接入返回 404 | 服务器未设置 `EXTENSION_API_TOKEN`、不足 24 位，或服务器版本过旧。 |
| 提示页面脚本与后台版本不一致 | 刷新注册网页。 |
