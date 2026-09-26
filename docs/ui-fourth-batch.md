# UI 第四批实施记录

对应 `UI改动大纲.md` 第 13 章第 4 批（第 2.2 章方案 B）：邀请、补充、轮转、踢人四个接口立即返回任务 id，前端按 id 追踪。仅本地实现与隔离验证，未部署。

## 已实施

- 新增 `app/application/jobs/commands.py`（`command_runner`）：在应用进程内运行已加锁的团队命令，每个命令使用独立数据库会话，随应用生命周期启动/停止（`app/main.py`）。
- `console_actions.py` 的四个入口新增 `background` 参数。HTTP 路由传 `background=True`：校验、加团队锁、提交 Operation 后立即返回 `202 {accepted: true, status: "running", operation_id, workspace_id, account_id, email}`；流程在后台跑完后写入结果。
  - 不传 `background` 的调用（`scripts/signup_one.py`、已有单元测试）行为不变，仍同步返回完整结果。
  - 轮转原本不加团队锁（由流程内部检查冲突）。改为后台后，返回 id 之前先检查同团队是否已有 `rotate/onboard/reregister` 任务在跑，避免两个轮转同时被接受。
- 失败与中断：
  - 流程抛出未预期异常：记为 `failed / command_failed`，只写中文通用提示，异常原文只进服务日志，不进任务记录。
  - 流程在安全点响应取消（`browser_progress` 抛出 `CancelledError`）：记为 `cancelled`。
  - 服务停止或重启时仍在跑：记为 `manual_required / resume_manual`，与现有 `recover_stale_operations` 的口径一致，不自动续跑。
- 前端：
  - `runtime-store.js`：`track()` 收到 `accepted` 后直接按真实 id 登记为进行中，不再按团队和类型反查。新增 `waitFor(id)`：等任务进入终态并读到详情（包含 result）后才 resolve；若 runtime 已显示结束，最多每 5 秒补读一次详情。
  - `app.js` `postAction`：四个接口收到 202 后等待 `waitFor`，再把详情里的 result 交给原有调用方。邀请、补充、轮转、踢人表单的结果摘要、重试入口、刷新抽屉逻辑都不用改。
  - 旧的同步响应仍兼容（未收到 `accepted` 时走原路径）。

## 效果

- 请求不再挂住几分钟：浏览器刷新、网络抖动、反向代理超时都不影响后台流程。
- 进度从提交那一刻起就绑定到真实任务 id。同一团队同时有其他任务结束时，不会串到别的任务上。
- 冲突时返回的 `operation_id` 指向正在跑的那个任务，前端沿用原有冲突提示，不会把别人的任务标成失败。

## 边界

- 没有做持久化队列：进程重启不续跑，任务交给人工确认。这和原来"请求中途断开"的效果相同，但现在有明确记录。
- 没有新增业务取消点：取消仍然只在流程已有的安全点生效（浏览器阶段回报、轮转的检查点）。
- 单进程部署（`team48-manager` 单容器）下直接可用。如果以后改成多 worker，团队锁仍由数据库唯一索引保证，但每个 worker 只能管理自己启动的命令。

## 验证

- 新增 `tests/test_async_commands.py`（5 项）：四个接口都在流程结束前返回 202 和运行中的 id，runtime 可见，完成后结果写回；同一团队第二个踢人/轮转请求返回冲突并指向正在跑的任务；异常不泄露细节，且锁会释放；安全点取消记为已取消；服务关闭时运行中的任务标为需人工。
- `tests/task_progress_ui.test.cjs` 新增 1 项：`accepted` 登记和 `waitFor` 只在读到终态详情后 resolve。
- 其余结果见下方"回归"。

## 回归

- Node：`task_progress_ui`、`runtime_ui`、`operation_notifications`、`invite_seat_ui`、`management_ui`、`ui_first_batch`，73 项通过。
- Python：`test_batch3_actions`、`test_invite_flow_seats`、`test_onboard`、`test_replenish`、`test_official_member_logic`、`test_operation_api`、`test_runtime_status`、`test_console_actions`、`test_operations`、`test_automatic_rotation`、`test_async_commands`，共 113 项，112 项通过。
  - 唯一失败的是 `test_official_member_logic.UIActionMatrixTests.test_success_sync_uses_concise_feedback_without_task_link`，它断言 `app.js` 里不出现"查看任务"。暂存本批改动后在原代码上重跑同样失败，是前几批留下的，和本批无关，本批没有处理。
- 浏览器：`browser_task_progress`（隔离 TestClient）、`browser_runtime_lifecycle`、`browser_accounts_layout`（8019 隔离预览）通过，无页面报错，写请求全部被拦截。
- `node --check` 通过。未跑全量测试，未访问生产 VPS，未部署。
