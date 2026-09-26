# 总览团队看板

需求：把总览换成一眼能看清的看板——哪个账号在 Sub2API、哪个不在，上次授权、上次推送是什么时候，每个团队今天切换了几次。

## 页面

总览页保留顶部 4 个汇总数字和"后台运行状态"，原来的"团队资产 / 待处理"两栏换成**团队看板**：

- 每个团队一块：团队名、席位、**今日切换 N 次**、**在 Sub2API x/y**、到期剩余天数；有账号不在 Sub2API 时用警告色写出数量；右侧"管理团队"直达账号页的团队视图。
- 每个账号一行：邮箱（点击进账号详情）、母号 / 子号 / 邀请中、**Sub2API 状态**（在 Sub2API / 不在 Sub2API / 已暂停调度 / 远端已删除 / 远端异常…）、授权是否有效、**上次授权**、**上次推送**。
- 时间显示为"2 小时前 / 3 天前"，悬停显示精确时间；超过 7 天用警告色。
- 顶部 4 个筛选：全部 / 不在 Sub2API / 远端异常 / 超过 7 天未授权。
- Sub2API 没配置时，状态列统一显示"未配置"，并在看板顶部提示。
- 数据每 30 秒刷新一次；远端状态本身由后台每 15 秒核对。
- "资源与任务提醒"（手机号、HME、任务）收进看板底部的折叠区。

## 数据来源

| 字段 | 来源 |
|---|---|
| 是否在 Sub2API | 已有的远端状态快照（`remote_status.state`），后台每 15 秒和 Sub2API 核对 |
| 上次推送 | **新增** `external_bindings.last_pushed_at`。推送成功（写入 + 复读身份一致）时记录；时间取 **Sub2API 返回的账号 `updated_at`**，也就是远端自己记下的写入时间，取不到时才用本地时间 |
| 上次授权 | 该账号最近一次**成功完成**的授权会话时间（`oauth_sessions.consumed_at`）。插件授权、手动粘贴回调、自动重新授权都会产生这条记录；未完成或失败的会话不算 |
| 今日切换 | 已有的团队计数（北京时间 0 点清零） |

- 新字段启动时自动加到现有数据库（`bootstrap.py`），已验证旧库升级、重启后数据保留。
- **历史数据**：`last_pushed_at` 从现在开始记录，之前推送过的账号显示"无记录"，下次推送后就有了。上次授权读的是已有记录，历史数据直接可用（只要授权会话还在库里）。
- 两处推送成功的路径都会记录：手动 / 授权后跟进的推送（`account_sub2api_push`），以及重新授权后自动推送新令牌（`push_refreshed_tokens_to_bound_sub2api`）。

## 改动文件

- `app/persistence/models/identity.py`、`app/persistence/migrations/bootstrap.py`：新增 `last_pushed_at` 列，重建绑定表时一并保留。
- `app/application/sub2api_publish.py`：`remote_pushed_at()`，推送成功时写入。
- `app/application/sub2api_status.py`：远端状态里带上 `pushed_at`。
- `app/application/queries/portfolio.py`：每个账号带上 `last_authorized_at`（一次分组查询）。
- `app/web/static/js/team-board.js`（新）、`app/web/static/css/runtime.css`、`console.html`、`app.js`：看板界面。

## 验证

- 新增 `tests/test_team_board.py` 4 项通过：取最近一次成功授权（忽略未完成和失败的会话）；从没授权过显示空而不是 0；推送时间来自绑定；Sub2API 时间解析（含 `Z`、`+08:00`、格式错误回退）。
- 相关测试通过：`test_auth`、`test_ui_contract`、`test_management_context`、`test_sub2api_status`、`test_sub2api_republish`、`test_sub2api_management`；Node 122 项；浏览器 `browser_theme`、`browser_runtime_lifecycle`、`browser_ui_polish`。
- 截图：`dist/ui-board/overview-{dark,light}-{1440,1024,390}.png`，三档宽度都没有横向溢出。
- 预览数据里没有授权和推送记录，所以截图上这两列都是"无记录"，有真实数据后才会显示时间。
- 未部署。
