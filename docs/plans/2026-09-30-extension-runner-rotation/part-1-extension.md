# Part 1：插件"服务器模式"

**状态**：待开始
**依赖**：part-0 已完成
**所属计划**：[README](./README.md)

## 目标

同一份插件在服务器的 Chromix 里被加载时，不需要人点弹窗：它自动向服务器领任务，完成注册，等服务器下发授权链接后在同一标签页完成 OAuth，把回调交回服务器；全程把状态上报给服务器。自检任务时采集浏览器指纹信号和截图。用户本机 HubStudio 的用法**完全不变**。

## 可改文件

- `extensions/chatgpt-signup/background.js`
- `extensions/chatgpt-signup/content.js`
- `extensions/chatgpt-signup/shared.mjs`
- `extensions/chatgpt-signup/runner.mjs`（新建：服务器模式的领任务、上报、指令处理）
- `extensions/chatgpt-signup/manifest.json`（只改版本号）
- `extensions/chatgpt-signup/README.md`（只改版本号和一句"服务器模式由 Team48 自动使用，本机无需设置"）

## 只读参考

- `docs/contracts/extension-runner.md`（协议唯一来源，字段以它为准）
- `extensions/chatgpt-signup/private-config.mjs` 的导出格式（本机文件不含 `RUNNER`，服务器生成的副本才有）
- `app/integrations/openai/browser/signup.py`、`signup_bridge.js`（服务端托管模式如何复用 `content.js`，不能被本 Part 破坏）

## 要点

- **开关**：只有 `private-config.mjs` 导出了合法 `RUNNER`（`baseUrl` 为 `http://127.0.0.1` / `http://localhost`，`runId`、`token` ≥24 位）时进入服务器模式。用 `import * as PRIVATE_CONFIG` 读取（`background.js:3` 已这样导入），本机没有 `RUNNER` 时一切照旧。
- **不能破坏的现有行为**：弹窗手动任务、Team48 自动接入（resolve/handoff）、托管模式（`globalThis.__team48ManagedSignup`，`content.js:12`）三条路径行为不变。
- **无痕检查**：服务器模式下跳过 `background.js:526`、`:540`、`:301`、`:384` 与 `shared.mjs:35` 的"必须无痕"要求（服务器用普通窗口 + 全新专用档案）。本机模式仍要求无痕。
- **领任务**：`chrome.runtime.onStartup` 和 `onInstalled` 各触发一次 `runner.mjs` 的 `boot()`，另设 alarm 兜底（Service Worker 被回收后恢复）。`GET job` 失败重试 3 次（间隔 3 秒），仍失败则不做任何事。
- **启动注册**：把现有 `start` 分支（`background.js:525-558`）抽成 `startJob({email, mode, profile, hidePanel, handoff, tabId?, password?})`，弹窗和服务器模式共用。服务器模式：`mode:'auto'`、`hidePanel:true`、`handoff` 为空（不走 Team48 接入）、密码用服务器给的 `password`（让服务器能保存）、使用浏览器现有的第一个普通窗口的标签页（没有就 `chrome.tabs.create`）。
- **状态上报**：在 `saveJob`（或等价的统一保存点）后，若 `status/stage/phase/pauseReason/handoff.status/codexResult` 任一变化就 `POST event`；另外 30 秒心跳一次。上报不含邮箱、密码、验证码、页面原文；`diagnostics` 只在结束（done/stopped/paused）时附带 `diagnosticReport(job, VERSION)`。
- **人工类暂停不自动恢复**：服务器模式下 `phone`、`captcha`、`rate_limit`、`manual_step`、`email_mismatch`、`session_error`、`unknown_page`（超过自动重试次数后）一律上报 paused 并停在原地；服务端会结束运行。`AUTO_RETRY_REASONS` 的有限自动重试保留。
- **等待授权**：注册完成（`finishSignup`）后，每 5 秒 `POST event`；收到 `command:"authorize"` 且 `authorizeUrl` 的 origin 是 `https://auth.openai.com` 时，调用现有 `openAuthorization` 的等价逻辑：同一标签页、`phase:'oauth'`、`handoff.status:'authorizing'`、`workspaceNames` 用 job 里服务器给的值。可以把 `openAuthorization` 改成接受参数，而不是依赖 `job.handoff` 来自 `/handoff` 响应。
- **回调**：沿用 `webNavigation` 拦截（`background.js:950-956`）。服务器模式下 `captureCallback` 不调用 `/api/ext/handoff/complete`，改为 `POST runner callback {callbackUrl}`；网络失败每 5 秒重试，最多 12 次。成功后标 `done`，不跳转 `popup.html`（改为 `about:blank`），上报 `status:"done", phase:"oauth"`。
- **stop 指令**：`command:"stop"` → 走现有停止逻辑（与弹窗"停止"一致），之后不再请求。
- **自检（`kind:"selfcheck"`）**：按协议文档"自检"一节实现。信号采集放在 `content.js` 一个独立函数里（只在服务器模式、且 background 发来 `selfcheck-signals` 消息时运行），通过 `chrome.runtime.sendMessage` 回给 background，再由 background `POST probe`。WebGL 取 `WEBGL_debug_renderer_info`；WebRTC 用 `stun:stun.l.google.com:19302` 收集候选，只上报候选里的 IP 字符串。截图用 `chrome.tabs.captureVisibleTab(windowId, {format:'png'})`。
- **版本号**：`manifest.json` 与 `content.js` 的 `const VERSION` 一起升到 `0.6.0`（`signup.py:28` 会校验两者一致）。

## 步骤

1. 读协议文档与现有 `background.js` 的 `start`、`finishSignup`、`openAuthorization`、`captureCallback`、`saveJob`。
2. 抽出 `startJob`，弹窗路径改为调用它，确认行为不变。
3. 写 `runner.mjs`：配置校验、`boot`、`fetchJob`、`report`（节流 + 心跳）、`waitAuthorize` 轮询、`submitCallback`、`runSelfcheck`。
4. 在 background 各处加"服务器模式"分支（无痕检查、回调去向、暂停不恢复、结束跳转）。
5. `content.js` 加自检信号采集；版本号升到 0.6.0。
6. 基本检查。

## 完成标准

- [ ] 本机没有 `RUNNER` 配置时，弹窗注册与 Team48 自动接入流程代码路径与改动前一致
- [ ] 服务器模式下不依赖弹窗、不要求无痕，能按协议领任务、上报、授权、回交回调、执行自检
- [ ] `manifest.json` 与 `content.js` 版本号一致（0.6.0）
- [ ] 基本检查通过：`for f in extensions/chatgpt-signup/*.js extensions/chatgpt-signup/*.mjs; do node --check "$f"; done`（`.mjs` 若 `node --check` 不支持则用 `node --input-type=module --check < "$f"`）

## 完成记录
