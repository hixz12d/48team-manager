# Sub2API 远端状态与后续恢复：第二批交付

> 本批远端状态与恢复行为继续有效。新增刷新归属与访问令牌回读见 [第三批交付](sub2api-refresh-authority.md)。

本地代码已完成，未部署或执行生产迁移。完整服务端合同位于 Sub2API 仓库 `docs/team-credential-sync-v2.md`。

## 使用方式

账号详情新增“Sub2API 远端状态”面板：

- “重新核对状态”：读取远端身份、凭据版本、最近同步操作及已知运行阻断，保存脱敏观察记录。
- “重试未完成步骤”：重新核对绑定和实例后，只请求处理缓存与共享调度；不重新提交凭据，不消费刷新令牌。
- “同步步骤已完成”与“仍有运行阻断”独立展示。人工暂停、认证/运行错误或限额不会被自动清除。
- 显示最后成功核对时间、最近尝试时间；失败保留旧快照。120 秒以上的成功观察标为旧快照，不允许直接重试。
- 打开的详情可短暂自动核对 pending 操作；关闭抽屉、切换账号或页面隐藏后停止相应轮询。

普通状态 GET 只读本地观察，不请求远端：

```text
GET  /api/accounts/{account_id}/sub2api/remote-state
POST /api/accounts/{account_id}/sub2api/remote-state/refresh
POST /api/accounts/{account_id}/sub2api/remote-state/retry
```

三者均要求管理员登录，可带 `workspace_id`。多个工作区绑定必须选定上下文，不能猜测使用其中一个。

## 一致性与边界

服务端 `oauth_sync.revision=3` 声明原子回执、可恢复后续任务和数据库实例 UUID。Team 在提交中传递 expected_instance_id；已观察并固定的实例发生变化时拒绝覆盖，要求先核实连接与绑定。

Sub2API 的凭据条件写入、操作记录、调度事件在同一数据库事务内提交。新回执不依赖通用幂等记录的过期策略。后台任务按带独立标识的租约领取，缓存成功立即保存；后续调度失败或进程退出后，继续处理未完成的步骤。旧租约不能覆盖接手任务的进度。

Team 观察记录以 binding_id 隔离并使用版本 CAS。身份不匹配、实例变化、绑定变更或较旧响应均不能替换已验证的新快照。重试不会自动更换目标，也不会创建替代账号。

`completed` 只确认本次缓存删除与共享调度快照处理。它不是上游授权/成员权限验证，也不保证旧请求已经排空或不会回填缓存。面板中的运行限制来自已知账号字段，不代表全部模型、预算和路由条件。

凭据写入已确认、最终身份匹配而缓存传播仍 pending 时，用户显式提交的名称等配置仍可继续处理，整体结果保持 partial。配置失败保留已写入回执；缓存/调度重试不代替配置重试。

## 数据与发布

- Sub2API 新增 `248_oauth_sync_operations.sql`：`integration_identity` 和 `oauth_sync_operations`。
- Team 新增 `sub2api_sync_observations`，通过现有启动建表流程创建，无需增加或回填 Account 列。
- 操作表只保存摘要和步骤元数据；观察表只保存白名单字段。令牌、原始远端错误和未识别响应字段不进入这些记录。
- 新回执本批不自动清理。第一批通用幂等回执仍可按原保留期只读查询，不能自动重建缺失的恢复任务。

先按 Sub2API 项目规定完成双实例滚动升级，确认两个副本返回同一 instance_id 和 revision=3，再升级 Team。两个项目保持各自部署边界；本批未执行上述发布动作。

第一批 Team 会拒绝 revision=3；本批 Team 会拒绝 revision=2 的凭据同步。滚动窗口出现明确“不支持安全同步”时应等待版本一致，不能绕过能力检查或退回 PUT。

回退前暂停自动凭据推送，保留新增表和操作记录。旧应用不处理新 pending 任务；恢复新版后可继续。克隆数据库作为独立环境时，应分配独立实例身份；共享同一数据库的应用副本使用同一身份。

## 实际验证

所有测试使用合成账号/凭据、内存或临时 SQLite、临时 PostgreSQL/Redis。浏览器请求由隔离 FastAPI TestClient 承接，没有请求生产或真实上游。

Python：**61 项通过**。

```text
.venv/Scripts/python.exe -m unittest tests.test_sub2api_remote_state tests.test_sub2api_credential_sync tests.test_sub2api_management tests.test_sub2api_sync_contract tests.test_sub2api_sync_persistence tests.test_reauth
```

JavaScript：**28 项通过**，覆盖既有轮询、页面生命周期及管理视图回归。

```text
node --test tests/runtime_ui.test.cjs tests/management_ui.test.cjs
```

真实 Chromium + 隔离 FastAPI/SQLite：通过未登录拒绝、处理中、完成但暂停、旧快照、实例漂移、窄重试、抽屉关闭后停止轮询。1440 与 390 宽度无横向溢出，无页面错误。

```text
.venv/Scripts/python.exe -m tests.browser_sub2api_state
```

Sub2API：Go 1.27.0 下六个相关包针对性测试/编译检查通过，含生命周期接线；**3 项 PostgreSQL 集成测试通过**：并发重放、整体回滚、过期租约接管与旧租约拒绝。

```text
go test -tags=unit ./internal/service ./internal/repository ./internal/handler/admin ./internal/server/middleware ./internal/server/routes ./cmd/server -run 'TestOAuthDurableSync|TestOAuthCredentialSync|Test.*Wire|TestProvideCleanup' -count=1
go test -tags=integration ./internal/repository -run 'TestOAuthDurableSyncPostgres' -count=1 -v
```

未执行完整测试套件、生产迁移或真实上游可用性验证。刷新权归属、可信候选授权验证、自动清错、反向令牌同步及轮转仍属于后续批次。
