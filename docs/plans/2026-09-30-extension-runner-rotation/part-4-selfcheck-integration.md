# Part 4：环境自检与集成

**状态**：待开始
**依赖**：part-1、part-2、part-3 已完成
**所属计划**：[README](./README.md)

## 目标

在设置页加"浏览器环境自检"：一键启动一次与轮转完全相同的浏览器和插件，打开指纹检测页，把关键信号、发现的问题和截图显示出来，不注册、不碰官方团队。同时把新路由、任务类型、镜像改动都接上，使整个计划可部署。

## 可改文件

- `app/main.py`（挂载 `build_runner_router`）
- `app/application/console_actions.py`（新增 `start_runner_selfcheck`）
- `app/web/routes/api.py`（新增 `POST /api/runner/selfcheck`、`GET /api/runner/selfcheck/{public_id}/screenshots/{name}`）
- `app/web/schemas/settings.py`（自检请求体 `{platform: "linux"|"windows"|null}`，如需要）
- `app/application/presenters.py`（`OPERATION_TYPE_LABELS` 加 `runner_selfcheck: "浏览器环境自检"`；`BUSINESS_STEP_LABELS` 加 part-2 用到的 `runner_*` 阶段中文）
- `app/domain/automation/__init__.py`（`BROWSER_ACTIONS` 加 `runner_selfcheck`）
- `app/web/templates/console.html`（设置页新增"浏览器环境"卡片）
- `app/web/static/js/app.js`（卡片交互）
- `app/web/static/css/settings.css`（卡片样式，如需要）
- `deploy/Dockerfile`、`docker-compose.yml`、`deploy/docker-compose.yml`、`.dockerignore`
- `tests/test_managed_signup.py`（只改第 212-215 行对 Dockerfile `COPY` 行的断言，使其与新 COPY 一致；不新增测试）

## 只读参考

- `app/application/extension_runner.py`（`run_selfcheck` 返回结构）
- `app/web/routes/runner.py`（part-2）
- `app/application/console_actions.py:611-635` `_run_command`、`:776-798` `start_controlled_rotate`（后台命令写法）
- `docs/PRODUCT.md` 体验约定（字号、确认、状态不只靠颜色）

## 要点

### 自检任务

- `start_runner_selfcheck(db, *, platform)`：`operation_store.create(op_type="runner_selfcheck")`，后台执行 `run_selfcheck(db, proxy_url=<代理>, platform=platform, job_id=public_id)`；结果写进任务 `result_snapshot`。
- 代理来源：设置页选一个团队（下拉，默认第一个启用团队），用其母号 `owner.proxy`，与轮转一致。未启用 runner（`RUNNER_BROWSER_EXECUTABLE` 未配）时直接返回错误提示，不创建任务。
- 浏览器槽被占用时返回 `browser_busy`。
- 截图接口只允许 `data/selfcheck/<public_id>/` 下 `[a-z0-9_-]+\.png`，需管理员登录。

### 设置页卡片"浏览器环境"

- 显示当前配置：轮转注册方式（`playwright` / `extension`，只读展示，改需改 `.env`）、Chromix 路径是否存在、伪装平台、显卡模式。
- 操作：选团队（决定出口代理）+ 平台（跟随配置 / Linux / Windows）→"开始自检"。危险度低，不需要二次确认，但按钮文案写明"不会注册或改动官方团队"。
- 结果区：出口 IP 国家与时区、UA 平台、WebGL 渲染器、WebRTC 候选、`webdriver`；`findings` 逐条列出（问题用文字 + 图标，不只靠颜色）；三张截图缩略图，点开看大图。
- 进度沿用现有任务进度组件（任务中心可见）。

### 镜像与部署

- `deploy/Dockerfile`：
  - Xvfb 改 `-screen 0 1920x1080x24`。
  - `COPY extensions/chatgpt-signup /app/extensions/chatgpt-signup`（整个插件目录，供运行器复制）；`.dockerignore` 继续排除 `extensions/chatgpt-signup/private-config.mjs`。
  - 装常用字体让伪装更自然：`fonts-dejavu-core fonts-noto-color-emoji fonts-noto-cjk`（已有 liberation / noto 时只补缺的）。
- 两个 compose 文件：不写死新变量（走 `.env`），只确认 `data` 卷覆盖 `data/runner-runs`、`data/selfcheck`、`data/browsers`。
- `deploy.env.example` 已由 part-0 更新，本 Part 不改。

## 步骤

1. 挂载路由、登记任务类型与步骤中文。
2. 写自检命令与两个 API。
3. 设置页卡片 + JS + 样式。
4. Dockerfile / compose / dockerignore；同步 `tests/test_managed_signup.py` 的 COPY 断言。
5. 本地预览看一眼卡片（`python -m uvicorn tests.preview_app:app --port 8019`），关闭预览进程。
6. 基本检查。

## 完成标准

- [ ] 设置页能发起自检并看到信号、问题列表和截图
- [ ] 自检不创建账号、不发邀请、不改任何官方团队数据
- [ ] 镜像包含完整插件但不含 `private-config.mjs`；Xvfb 为 1920×1080
- [ ] 基本检查通过：`.venv/Scripts/python.exe -m compileall -q app scripts`、`.venv/Scripts/python.exe -c "import app.main"`、`for f in app/web/static/js/*.js; do node --check "$f"; done`

## 完成记录
