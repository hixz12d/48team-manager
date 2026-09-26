# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

单一使用者：项目作者本人，作为运维操作员在桌面浏览器（Windows，1440px 以上宽屏为主）使用。生产规模约 10–40 个 ChatGPT Team 团队、几百个账号。手机端只需要"能看、不塌"，不是设计目标。

## Product Purpose

自用的 ChatGPT Team / Workspace 运营控制台。日常任务：

1. 巡查各团队母号 / 子号的官方 5h / 7d 额度，判断谁该轮转。
2. 发现授权失效（401、缺刷新凭据）并重新授权；实际授权多由浏览器插件 `extensions/chatgpt-signup` 完成，控制台主要负责总体查看数据。
3. 踢出成员 + 邀请 / 补位新成员，并跟踪这一长任务的进度（用户明确希望有进度条式的可视化）。
4. 配置外部服务（Sub2API、iCloud HME、临时邮箱、手机号池）并排查同步失败。
5. 查看后台持久化 Operation（步骤、结果、人工处理项）。

成功的标准：一眼看到异常，两三步内完成处理，长任务随时知道走到哪一步、卡在哪。

## Positioning

不是 SaaS、CRM 或质保平台。核心机制是"三个轴永远分开"：官方计划（free/plus/pro/team/business）、Workspace Role（owner/admin/member）、本地用途（mother/child/standby/free/disabled）互不推断。真相来源明确：本地用途来自本地库，官方额度 / 成员来自 OpenAI 官方 API，计费与代理目录来自 Sub2API，HME / 手机号来自本地租约。

## Operating Context

- 部署为单容器（FastAPI + SQLite + APScheduler + Playwright），端口只绑 `127.0.0.1:8018`，经 Nginx 暴露。
- 长命令一律返回 `operation_id`，后台执行并持久化步骤（`operations` / `operation_steps` 表），刷新、关浏览器、重启容器后仍可恢复。
- 自动额度检查、自动重授权、自动轮转默认关闭，需要在设置页显式开启。
- 授权闭环常在插件侧完成，控制台负责显示结果与后续同步。
- 本地预览：`tests/preview_app.py`（端口 8019，示例数据，禁止写操作）；已有 Playwright 浏览器回归脚本 `tests/browser_*.py` 与 Node 测试 `tests/*.test.cjs`，依赖现有 CSS 选择器与 aria 名称。

## Capabilities and Constraints

- 页面：总览 `/`、账号与团队 `/accounts`（含按团队 / 全部账号 / 未分配 / 待处理四个子视图）、任务 `/operations`、手机号、HME、代理（只读，来自 Sub2API）、设置、登录。
- 技术栈约束：Jinja2 + 原生 JS + 原生 CSS，没有前端构建链，不引入框架或 UI 库。
- 主题：深色为默认，浅色与跟随系统可选，跨标签同步。
- 术语：团队 = Workspace = ChatGPT Team（对外统一叫"团队"）；母号 = owner 账号；子号 = 在席成员；待命 = standby。
- 危险操作（移出官方团队、撤销邀请、受控轮转、删除本地档案 / 团队）必须二次确认，并说明会否修改官方 Team。
- 金额只在显示层格式化；未知不显示为 0。
- 未决：手机端专门布局不做。
- 字体：拉丁字符用本机已安装的 Anthropic Sans / Mono（CSS `local()` 引用，不随站点分发字体文件），中文回退系统中文字体；其他机器回退系统字体。

## Brand Commitments

名称 "48 Team Manager"，侧栏副标题"团队号后台"，logo `app/web/static/logo.svg`。界面语言为简体中文，语气直接、简短、工程化。无其他品牌约束。

## Evidence on Hand

- 真实运行截图与产品文档：`docs/*.md`（尤其 `unified-management.md`、`automatic-rotation.md`、`runtime-and-member-lifecycle.md`）。
- 业务步骤中文标签：`app/application/presenters.py` 的 `BUSINESS_STEP_LABELS`、`OPERATION_TYPE_LABELS`。
- 插件侧已有一套"正在注册 / 步骤 / 暂停 / 停止"的进度呈现（`dist/signup-extension-progress.png`），可作为控制台进度条的参照。
- 没有第三方用户、评价或案例，不得编造。

## Product Principles

1. 异常优先：正常项让位，异常项和下一步动作永远最显眼。
2. 一处一真相：同一事实（授权状态、Sub2API 状态、额度新旧）只显示一次，不在多列重复。
3. 长任务可追踪：每个后台 Operation 都能看到"走到第几步、卡在哪、下一步是什么"。
4. 危险操作说清后果：改官方 Team 与只改本地要在确认里区分。
5. 保留现有视觉与测试契约：改信息架构与细节，不换风格；保留已有选择器和 aria 名称，或同步更新测试。

## Accessibility & Inclusion

单人使用，无强制标准，但要求键盘可完成主流程（Esc 关闭、Tab 焦点环唯一、菜单方向键），正文不低于 12px，状态不能只靠颜色。
