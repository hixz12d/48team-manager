# 第四批：可信候选验证与按版本恢复认证

> 第五批已补充持久化刷新协调和安全交回，见 [第五批交付](sub2api-refresh-handoff.md)。本文未完成项描述的是第四批交付时状态；可信验证及受控清错规则继续有效。

本批完成本地实现及隔离验证，尚未部署、没有请求真实生产账号的上游接口。第五批的跨副本刷新排空、旧刷新写入隔离与安全回交，以及第六批自动轮转和生产验收，仍不在本批完成范围内。

## 使用入口与效果

账号详情 Sub2API 面板增加“验证新授权并恢复认证”。先完成本地新授权，保留配套 AT、RT 和明确 client_id，然后预览并确认。该操作把新授权交给 Sub2API 自己验证；浏览器不接收令牌，不把 Team 的本地布尔标记当作验证证据。

Team 管理员接口：

```text
POST /api/accounts/{id}/sub2api/auth-recovery/preview
POST /api/accounts/{id}/sub2api/auth-recovery
```

提交完整使用预览的 preconditions，绑定本地版本/凭据指纹、远端版本、实例、绑定指纹和刷新归属 epoch。旧预览或本地凭据发生变化时阻止提交。预览、取消不推送或验证候选令牌。

本操作保持 Team 本地 auth_state 不变。远端 Codex 服务验证不能替代 Team 管理接口的授权验证；也不能替代第三批的刷新归属交接。远端恢复后，人工调度开关关闭、额度/过载/权限阻断仍可能导致账号不可用。

## 服务端可信验证范围

Sub2API 同步能力 revision 升级到 4；请求仍是 contract_version=1。只有服务端具备验证器才声明 auth_only、candidate_validation、versioned_auth_errors。Team 同时接受 revision 3 和 4 的普通凭据同步；认证恢复要求 revision 4 的完整能力声明。

`recovery_mode=auth_only` 的新操作在写入前使用候选 AT 直接请求两个固定官方 HTTPS 端点：

1. `https://chatgpt.com/backend-api/wham/usage`：要求结构化响应中 email、account_id 与目标身份及明确工作区一致，user_id 非空且在已有目标用户ID时匹配；存在明确 rate_limit，且 allowed=true、limit_reached=false。
2. `https://chatgpt.com/backend-api/codex/models?client_version=0.101.0`：要求返回非空的结构化模型清单。

复用现有出口选择与代理/TLS transport，但不经过 token provider、缓存或刷新入口；不消费 RT，不自动重试，不跟随重定向，不记录原始响应。单次响应上限 1 MiB，验证总时间上限 25 秒。网络错误、401、403、429、挑战页、缺字段、身份/工作区不匹配和空模型清单均拒绝恢复，保持原凭据。

验证范围是 `codex_identity_usage_catalog`：证明候选身份与 Codex 额度/模型清单访问。**它不证明某个模型的生成请求、计费、所有 Team 管理权限或长期可用性。** UI 与回执不宣称这些能力已验证。部分合法令牌如果官方没有返回所需身份字段，也会保守拒绝；不会用未验签 JWT 解码或本地元数据补齐缺失证据。

## 认证错误归因与事务

新增 Sub2API 迁移 `249_oauth_auth_recovery.sql`：

- 独立 `oauth_auth_errors` 表保存账号、错误种类、凭据版本、AT 哈希、受控消息、冷却截止和观察时间；不保存令牌明文，不依赖可编辑 extra 里的标记。
- `oauth_sync_operations` 新增 auth_recovery、validation_scope、validated_at，重启或重放后仍能查询验证与恢复结果。

HTTP 错误归因只接受固定官方 HTTPS 请求实际发出的 Authorization，且该 AT 与账号凭据快照相同、版本为正数。明确的 token_expired、invalid_token、token_revoked、token_invalidated 或结构化 Unauthorized 才进入版本化记录。临时错误和永久错误分别记录；数据库以完整凭据条件比较，迟到的旧请求不能封住新凭据。

原生刷新拒绝沿用已有 `SetOpenAIOAuthErrorIfCredentialsUnchanged`，只为明确的刷新拒绝隔离消息与完整正数版本凭据保存归因。其原有隔离/调度行为保持；恢复不会据此自动重新打开 schedulable。

认证恢复在同一 PostgreSQL 事务中完成凭据 CAS、匹配错误恢复、证据移除、操作回执和 scheduler_outbox。必须满足：

- 验证结果在服务端本次调用产生，且入库时不超过 30 秒。
- 新凭据版本大于出错版本，新 AT 哈希与出错 AT 不同。
- 当前错误消息/状态或临时冷却消息/时间仍与记录严格匹配。
- 原凭据与 updated_at 仍符合请求前置条件。

缺少匹配归因返回 AUTH_ERROR_UNATTRIBUTED，整笔事务回滚。历史错误、不明来源错误、管理员改写的冷却和权限/工作区停用错误不自动转换成可清除认证错误。普通 credentials_only 不验证、不清错。

数据库恢复后继续第二批缓存/调度回执流程。版本化认证冷却在读取 Redis 缓存时重新核对数据库，避免旧缓存继续阻断；不盲删可能已经更新的其他冷却。限流、过载、人工 schedulable 开关和其他账号字段不被恢复流程清除。

## 部署与回退

推荐先升级 Team（普通同步兼容 revision 3/4），再按项目双实例滚动规则升级 Sub2API；全部实例更新且迁移 249 成功后才使用第四批恢复入口。不要用旧 Team 客户端调用 revision 4 服务并假定它仍会接受能力声明。

生产发布仍需要正常备份和迁移预检。249 为追加表/列，无历史证据回填。不得根据旧错误文本批量伪造版本归因，也不要通过删记录或手改状态冒充本操作完成。回退旧应用时保留新增表/列以保存审计；已发生的上游授权和凭据写入不能靠回滚应用镜像撤销。

## 验证

- 106 项 Python 针对性与扩展回归通过；28 项 JavaScript 回归通过。
- Chromium + 隔离 FastAPI/SQLite：未登录拒绝、预览/取消/确认、暂停状态保留、无令牌进入浏览器响应、1440/390 宽度布局与关闭后停止轮询通过。
- Go 相关 service/repository/admin/middleware/routes/server 包针对性测试或编译检查通过；官方身份/工作区/用户不匹配、401/403/429、重定向、空清单、验证失败不写、重放不重复验证、旧冷却缓存等均覆盖。
- 隔离 PostgreSQL 18.1 + Redis 8.4：迁移、版本化 HTTP 错误恢复、原生刷新拒绝归因、旧 401 拒绝、未知错误整笔回滚、相同 AT 不恢复、outbox 失败回滚凭据/清错/证据/回执、人工冷却保留，以及第二批并发重放/CAS/租约恢复均验证。
- Python 扩展回归仍有既有 curl_cffi `Event loop is closed` 忽略异常和 Starlette/httpx 弃用告警；断言通过。一次并行回归触发工具 60 秒超时，改后台运行后完整 106 项通过。

未运行生产迁移、真实账号上游请求、生成能力测试或完整仓库全量测试。数据库和浏览器测试全部使用隔离数据与合成令牌。
