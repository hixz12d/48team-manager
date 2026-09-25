# 插件接入 Team48 与授权后续

自用注册插件注册完成后，可以一次跑完原来手工做的几步：同步团队 → 接入本地 → 生成授权链接 → 授权 → 粘贴回调 → 推送 Sub2API → 今日切换 +1。控制台的手动授权也加了同样的「授权后续」。邀请仍由人工发送。

## 控制台

团队详情里的「账号授权」页（接入并授权、重新授权）新增：

- 「授权成功后」：推送到 Sub2API、今日切换 +1。默认勾选，选择记在浏览器本地。母号授权时不显示。
- 「粘贴回调地址后自动提交」：粘贴完整的 `localhost:1455/auth/callback?...state=...` 后直接提交，不用再点「完成授权」。账号页的「手动授权」弹窗也支持。

结果合并成一条提示，例如「授权：…；Sub2API：推送完成；切换次数：今日切换 +1，现为 3 次」。任一步失败时提示变为警告色，授权本身不会回滚。

## 后端

`POST /api/accounts/{id}/reauth/complete` 新增可选字段 `workspace_id`、`push_sub2api`、`count_switch`。不传时行为与原来完全一致。后续步骤在换票成功后执行，任何失败都只写入 `followups`，不影响授权结果：

| 条件 | 推送 Sub2API | 切换 +1 |
| --- | --- | --- |
| 母号 | 跳过 | 跳过 |
| 账号不是该团队已加入成员 | 跳过 | 跳过 |
| 新令牌的 `chatgpt_account_id` 与团队 `official_workspace_id` 不一致 | 跳过，`workspace_mismatch` | 跳过 |
| 已有 verified Sub2API 绑定，且换票后已同步凭据 | 不重复推送，报告「已更新凭据」 | 正常 |
| 已有绑定，但凭据同步失败 | 不推送，提示去账号页核对 | 正常 |
| 其他 | 调用原 `account_sub2api_push`（设置里的默认分组/代理） | 正常 |

计数按成员去重：`workspace_memberships.switch_counted_at` 第一次授权成功时写入，并与团队计数在同一事务内提交。同一成员重试或重新授权都不再计数。升级时，已持有令牌的现有成员会被标记为已计数；已接入但尚未授权的成员，第一次授权时仍计一次。北京时间按天清零的规则不变。

## 插件接口 `/api/ext/*`

用 `EXTENSION_API_TOKEN`（至少 24 位）作 Bearer 鉴权，不接受管理员会话。未配置时全部返回 404。

| 接口 | 作用 |
| --- | --- |
| `GET /api/ext/workspaces` | 列出可用团队：有母号令牌、有官方 workspace ID、未停用 |
| `POST /api/ext/handoff {email, workspace_id[, sync_operation_id]}` | 第一次排队团队同步（与控制台同步共用去重队列），返回 `syncing`；带同步任务 ID 轮询，同步完成后核对成员快照，必要时接入本地，再生成授权链接 |
| `POST /api/ext/handoff/complete {account_id, workspace_id, ticket, callback_url}` | 走标准的 state/PKCE/邮箱校验换票，然后推送 Sub2API 并计数 |

`handoff` 只处理官方快照中**已加入**的成员：`invited` 返回 `member_not_joined`，快照中没有返回 `member_not_found`，母号返回 `owner_account`；这些情况都不会创建本地账号。同步失败返回 `sync_failed`，不使用旧快照。插件发起的同步任务在任务页的来源显示为「插件」。

## 插件行为

见 `extensions/chatgpt-signup/README.md` 的 0.5.0 一节。要点：

- 授权在注册所用的同一个无痕标签页进行（同一 HubStudio 环境和代理出口）。登录、验证码、团队选择和同意复用原有的 CDP 输入。
- 回调通过 `webNavigation` 拦截，只接受注册标签页里、`localhost` / `127.0.0.1:1455/auth/callback` 且带 `state` 的地址，每个授权会话只提交一次。
- 只有无痕上下文的 service worker 驱动接入（`incognito: split` 下普通窗口的 worker 看不到无痕标签页）。
- 状态保存在会话存储；service worker 被挂起后，由闹钟恢复进行中的接入。票据和回调地址用完即删，诊断记录只增加 `oauth_started` / `oauth_callback` 事件名。

## 部署

1. 生成令牌，写入 `/opt/team48/.env` 的 `EXTENSION_API_TOKEN`。
2. 按 VPS.md 只重建本项目：`docker compose -p team48 -f deploy/docker-compose.yml up -d --build --no-deps team48`。表结构（`switch_counted_at` 列）在启动时自动补齐。
3. 本机执行 `python scripts/build_signup_extension.py --local-config --unpack --team48-token <令牌>`，然后重新加载插件。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_member_handoff -v
node --test tests/signup_extension.test.cjs
.\.venv\Scripts\python.exe -m unittest tests.browser_signup_handoff -v
```

- `tests.test_member_handoff`：令牌鉴权与未配置时隐藏；完整的同步 → 接入 → 授权 → 推送 → 计数；重复授权不再计数；未加入/不存在/同步失败/母号的拒绝；错误邮箱或 state 不推送、不计数；工作空间不一致时跳过后续；已绑定账号不重复推送；控制台的后续步骤需显式开启；升级回填。
- `signup_extension.test.cjs`：插件 worker 的接入状态机；拦截回调只提交一次；来自其他标签页、主机或路径的回调被忽略；等待接受邀请；网络失败后保留回调并重新提交；回调已用过后停止；关闭标签页后失败并可重新接入；未配置令牌时不发请求。
- `browser_signup_handoff`：真实加载的插件在本地模拟站点完成注册，接着在同一个无痕标签页完成登录（注册密码）、选择正确团队、同意授权，并向本地 Team48 模拟服务提交回调。

测试不连接 OpenAI、Team48 生产环境或 Sub2API；真实授权页面的文案和结构以实测为准。
