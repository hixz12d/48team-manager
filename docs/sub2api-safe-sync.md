# Sub2API 安全凭据同步：本批交付

> 第一批历史记录。当前实现、迁移和验证见 [第二批远端状态与后续恢复](sub2api-sync-observations.md)。

状态：本地代码与隔离测试完成，未部署。Team 基线 `babad66`，Sub2API 基线 `349a34884`。

## 行为变化

- 更新已绑定账号前，要求 Sub2API 返回 `oauth_sync.revision=2`，并明确支持 `credential_cas`、`operation_receipts`。
- 缺少能力、管理认证失败、账号缺失或版本冲突时停止；不再降级 PUT，不自动创建替代账号。
- 向窄接口传递 client_id、明确的 workspace_id（包括显式 null）、expected_updated_at 和 operation_id。
- POST 超时或结果不明后只读取一次操作回执；仍未知则保存 unknown，不重复 POST。
- partial 的具体步骤保存到 Operation/OperationStep；不把 transient Account 属性当持久化状态。
- 手动和自动重新授权分别记录 manual_reauthorize、automatic_reauthorize。
- 显式改调度开关后再次读取；最终状态不再使用旧快照。
- 本批只支持 credentials_only。auth_only 的旧实现缺少可信候选验证与错误版本来源，Sub2API 明确拒绝；未启用跨应用 RT 单写、反向同步、自动恢复、轮转排空。

## 服务端合同

```text
GET  /api/v1/admin/integration/capabilities
POST /api/v1/admin/accounts/{id}/sync-oauth-credentials
GET  /api/v1/admin/accounts/{id}/credential-sync-operations/{operation_id}
```

能力示例：

```json
{"oauth_sync":{"revision":2,"available":true,"credential_cas":true,"operation_receipts":true,"recovery_modes":["credentials_only"],"auth_only":false,"single_writer":false,"metadata_changes":false,"instance_identity":false,"scheduler_confirmation":false}}
```

`state=recorded` 的查询结果携带历史 receipt；缺失、过期或无法确认时为 unknown。默认回执保留期为既有幂等配置的 24 小时，可由部署配置改变。

同步不会清除人工暂停、401、429、模型限制或预算，也不会证明令牌已经具备成员管理权限。client_id 不匹配或新 AT 试图继承未知关系的旧 RT 时拒绝。账号 API 的邮箱和工作区比对是元数据前置条件，不能替代未来的可信 grant 身份校验。

凭据写入与 scheduler_outbox 同一 SQL 语句；与最终幂等回执保存仍有崩溃窗口。缓存删除成功仅证明此次删除成功，不保证所有副本调度已传播或旧请求不能回填。上述情况不得显示为“服务已恢复”。

## 实际验证

使用合成凭据、模拟 HTTP 与内存 SQLite，没有调用官方账号或生产 API。

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_sub2api_credential_sync tests.test_sub2api_management tests.test_sub2api_sync_contract tests.test_sub2api_sync_persistence tests.test_reauth
```

结果：48 项通过。覆盖：缺少能力不写入、保留 client/context、账号与路由 404 区分、管理认证失败、外层业务失败、错误回执、未知阻断、处理中冲突查询、超时回执、partial/unknown 持久化、调度修改后最终读取和重新授权相关回归。

Sub2API 使用本机已有 Go 1.27.0 工具链，四个包针对性测试通过：

```text
go test -tags=unit ./internal/service ./internal/repository ./internal/handler/admin ./internal/server/middleware -run 'TestOAuthCredentialSync|TestIsRecognizedAuthError|TestFilterOAuthCredentialPatch|TestIdentityMatches|TestCompositeTokenCacheInvalidator|Test.*Audit' -count=1
```

真实临时 PostgreSQL 18.1 / Redis 8.4 环境验证通过：

```text
go test -tags=integration ./internal/repository -run '^TestOAuthCredentialSyncPostgres' -count=1 -v
```

并发两个相同快照写入仅一个成功，凭据变化只产生一条调度事件；暂停、错误、限额、临时禁调度、并发和优先级保持不变。测试容器已自动清理。未运行完整测试套件、真实上游授权、浏览器端到端或生产流量验证。

## 发布与回退边界

无需新增数据库表或迁移。Sub2API 先经自身发布通道部署到两个副本，再发布 Team。Team 工程不执行对方 Compose；Sub2API 的源码工作树和双实例滚动流程以所属项目 AGENTS.md 为准。

旧服务端下，新 Team 的绑定凭据推送会被阻止，这是预期兼容边界，必须显示升级要求。首次创建账号仍沿用现有创建流程，不属于本批幂等凭据更新协议。

回退前暂停自动推送，保留幂等记录及操作历史。不要让旧 Team 的 PUT 降级重新参与自动凭据覆盖。停止新自动化不能撤销已经发生的上游授权或成员变更。
