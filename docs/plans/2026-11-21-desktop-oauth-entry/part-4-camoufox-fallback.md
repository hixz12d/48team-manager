# Part 4（备选）：换 Camoufox 浏览器内核

**状态**：已完成：方案已移交新计划 [2026-11-21-camoufox-signup](../2026-11-21-camoufox-signup/README.md)

## 待讨论（part-3 实测结论，2026-11-21）

- **已验证可行**：桌面版入口 + 服务器 Playwright 自动"登录 + 授权"，对用户手工注册的号不触发手机验证（house-reducer-0y@，见 part-3 第 3 轮）。
- **问题**：自动化**注册**出来的号会被 OpenAI 当成机器人账号标记，之后无论服务器还是用户本人手工授权 Codex 都跳 `add-phone`。证据：服务器注册的 myriad-capote.2v@（干净新号，注册后 2 秒授权）、旧流程注册的 panda.liable-4i@ / banking0calmer@（用户本机复测也跳手机）、2026-10-24 本机插件注册的 7 个号；用户手工注册的新号授权直接成功。
- **还缺的一步**：授权前账号必须已接受 Team 邀请（onboarding 页 "Get started"），否则令牌是个人空间，被 `not_team_workspace` 拦下。服务器流程目前不会自动接受。
- 待定方向（未决定）：用户手工注册 + 服务器授权；用 Camoufox 等反检测环境改造注册；注册与授权之间加间隔 / 模拟真人行为；服务器授权前自动点 Get started。
- 未查明：注册环节具体是哪个信号被识别（自动化特征、填资料速度、注册后立即授权、指纹等）。
**依赖**：part-3 已完成且结论要求
**所属计划**：[README](./README.md)

## 目标

服务器 Playwright 授权流程可选用 Camoufox（基于 Firefox 的反检测浏览器，Python 库，Linux 可用，Playwright 的 Juggler 协议驱动，不走 CDP，按出口 IP 自动设时区 / 地理位置），用 `BROWSER_ENGINE=camoufox` 切换，再用新邮箱复测。

## 可改文件

- `requirements.txt`
- `deploy/Dockerfile`
- `app/core/config.py`（`BROWSER_ENGINE` 增加 `camoufox` 取值）
- `app/integrations/openai/browser/environment.py`
- `app/integrations/openai/browser/reauth.py`、`app/integrations/openai/browser/chatgpt_login.py`（只改启动 / 浏览器类型相关部分）
- `docs/plans/2026-11-21-desktop-oauth-entry/part-4-camoufox-fallback.md`（完成记录）

## 只读参考

- `C:\Projects\Github_Other_Projects\HixBrowser`（用户自己的指纹浏览器，内核就是 Camoufox；`engines/camoufox-0.5.9/shim.py`、`engines/common/hix_adapter.py` 里有参数用法）
- Part 3 完成记录（撞手机的现场截图和阶段）

## 要点

- 动手前先确认 part-3 的失败确实是手机页 / 风控，而不是页面识别错误；后者应回 Part 1 修。
- Camoufox 版本固定（参照 HixBrowser 用的 0.5.x），镜像里预下载浏览器，不在运行时联网下载。
- 用 Camoufox 的 `geoip=True` 按代理出口设时区和位置；语言 `en-US`；WebRTC 只走代理；用有头模式跑在现有 Xvfb `:99`。
- 档案目录按邮箱独立：`data/chrome-profiles/camoufox/<email>/`，与 Chromium / Chromix 档案分开，回退时互不影响。
- 回调截获：Firefox 下 `route` 拦 `127.0.0.1:1455` 可能受代理影响，需配置代理绕过本机回环（`localhost,127.0.0.1`）。
- 只接服务器 Playwright 流程；插件运行器（Chromix + 插件）不动。
- 镜像变大、依赖新增，上线按 RUNBOOK 重建并保留回退（改回 `BROWSER_ENGINE=chromium`）。

## 步骤

1. 读 part-3 完成记录和截图，确认需要本 Part。
2. 加依赖、Dockerfile 预装 Camoufox 浏览器。
3. `environment.py` 增加 camoufox 启动配置；`reauth.py` / `chatgpt_login.py` 按引擎选择启动方式。
4. 本地用 `--headed` 冒烟（打开 chatgpt.com 能加载、时区与出口一致）。
5. 基本检查；之后由 /flow-ship 部署，再用新邮箱按 part-3 步骤复测。

## 完成标准

- [ ] `BROWSER_ENGINE=camoufox` 能完成"登录 chatgpt.com → 桌面版链接 → 回调"
- [ ] 改回 `chromium` 行为不变
- [ ] 基本检查通过：`.venv/Scripts/python.exe -m compileall -q app scripts`、`.venv/Scripts/python.exe -c "import app.main"`

## 完成记录

