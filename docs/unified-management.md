# 统一管理界面与检测健康

## 本轮改动

- `/accounts` 统一账号与团队入口，支持按团队、全部账号、未分配、待处理。
- `/workspaces` 以 303 跳转并保留账号/工作区深链；旧 `view=portfolio/flat` 仍可使用。
- `purpose` 保持原有用途/归档筛选语义，`health` 表示检测状态，`team` 表示列表团队筛选。`workspace` 和 `account` 用于详情上下文，不会因打开详情而改变列表筛选。
- 原生 JS/Jinja2 实现，复用原有动作注册表、Overlay Manager、Operation 轮询和危险操作确认，没有新增前端依赖。
- 登记菜单分别提供本地账号登记和已有团队登记。本地登记只创建无凭证档案，重复邮箱拒绝覆盖，不创建远端账号、不邀请成员。
- 金额只在显示层格式化，保留后端 Decimal 和精确原值。未知不显示为零；正/负小额、科学计数法、大整数有独立测试。

## 检测契约

每个 `(account_id, workspace_id)` 独立提供：

- `latest_check`：最新有效尝试，包括实际 HTTP 状态、来源、错误代码、时间和凭证版本。
- `last_success_quota`：该上下文的最后成功额度，版本不同、超过 75 分钟或最新检测失败会标旧。
- `health`：集中计算的状态、严重度和下一步动作。
- `next_check_at`、`queued`：持久任务状态。

同一凭证先 401 后超时，不会因超时清除已确认的授权问题。旧凭证/旧租约迟到响应不进入当前读模型。新凭证先显示待验证，再由额度成功响应解除该上下文问题。历史记录不补造 HTTP 状态或凭证版本。

多工作区账号在全部账号视图中去重，详情可逐个上下文查看。不能用其他工作区的额度填补当前缺失数据。官方 Owner 角色不改写本地用途，邀请中不计入官方已加入人数。无官方人数快照时显示尚未同步。

## 调度与安全

- 保留 `official_quota_probe_scan`，每分钟选取到期上下文，按最早到期处理；默认每批 1 个，可通过已有 `official_quota_probe_batch_size` 设置为 1–3。
- 成功后按完成时间加约 60 分钟和少量抖动安排下一次检查，不赶整点重复探测。
- `quota_queue_dispatch` 每 2 秒查看持久任务；数据库全局派发租约限制同时执行一个额度任务，已执行任务之间至少间隔 20 秒。
- 手动、定时和 OAuth 后检查复用同一上下文 Operation。关闭详情不会取消后台任务。明确取消任务会记录取消状态。
- 上游 429 遵守有效 `Retry-After`，手动按钮也不会提前绕过。网络、5xx 和解析失败不当作凭证失效。
- 实际 401 仅在现有授权探测配置允许时尝试一次受账号级持久租约保护的 RT 刷新，再验证新凭证；不自动开启 OAuth、轮转或成员移除。
- 额度与刷新网络调用不持有长写事务；SQLite 写入前再次核对凭证版本及租约。
- 额度探测不改本地用途、席位、成员关系或 Sub2API 调度状态。

`GET /api/quota/runtime` 返回有效开关、排队数、最大延后、未覆盖上下文、近一小时已完成探测报告的 HTTP 尝试数及失败分类。内部 HTTP 重试也计入传输层提供的次数；该统计不是出口代理全部流量统计。实际一小时覆盖能力仍取决于上下文数量、响应耗时及限流，应根据运行指标评估。

## 配置与迁移

配置优先级：数据库持久设置 > 环境 > 安全默认值。新安装额度定时检查默认关闭；手动检查仍可排队执行。设置页显示实际有效值，不开启自动重授权、自动轮转或 force refill。

旧版本无持久额度开关时，历史回退实际上关闭。首次升级会保留这一行为，避免修复配置优先级时意外开启扫描。已持久设置的开关不被覆盖。

增量增加快照证据字段，以及 `quota_probe_states`、`quota_dispatch_lease`、`credential_leases`。无工作区上下文使用显式 `account:none` 主键，避免 SQLite NULL 唯一约束漏洞。历史快照保留，不删除数据库、不清理历史来掩盖缺失信息。

最新记录在 SQL 中用窗口函数选取，需要 SQLite 3.25 或更高版本。已在本地 SQLite 测试升级、重复升级、未知版本记录、NULL 上下文和并发入队。

## 验证与本地预览

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
node --test tests/management_ui.test.cjs
.\.venv\Scripts\python.exe -m uvicorn tests.preview_app:app --host 127.0.0.1 --port 8019
.\.venv\Scripts\python.exe -m tests.browser_management
.\.venv\Scripts\python.exe -m tests.browser_zoom_check
```

预览地址 `http://127.0.0.1:8019/accounts`，账号 `preview`，密码 `preview-only`。数据库在系统临时目录 `team48-preview-unified.db`，只有 example.com 示例账号。预览禁止账号写入和非白名单 API，不使用本机业务数据库，不启动调度器。

浏览器脚本检查 1600、1440、1280、1024、768、390 px，及通过临时 Chromium 扩展调用 `chrome.tabs.setZoom(2)` 的真实 200% 页面缩放。截图输出至系统临时目录 `team48-browser`，不会向仓库写入测试图片。测试不是对真实 OpenAI/Sub2API 服务的联机验收。

本次本地验收：Python 全量 242 项通过（314.040 秒），Node 前端测试 21 项通过；六种宽度的浏览器交互检查无 page error，真实 Chromium tab zoom 为 2，1440 px 窗口的 CSS 布局宽度为 720 px。`git diff --check` 无空白错误。

## 发布与回滚

本轮未部署。发布需要另行批准，仅操作 `/opt/team48` 和 `team48` 项目，绝不能触碰仓库禁止的 Sub2API 目录、容器、数据库或 Nginx 主备。

发布前在业务数据库副本执行升级并检查人数、上下文、额度时间和配置有效值；确认没有正在运行的凭证写任务后再切换版本。回滚前停用新扫描并等待运行中的检查结束，再恢复上一应用版本；新增列和表可保留，旧代码不会消费它们。不要通过删除或覆盖数据库回滚。旧版本不会派发新队列中的额度任务，回滚时需记录并人工核对未完成任务。
