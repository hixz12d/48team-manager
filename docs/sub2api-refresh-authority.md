# 第三批：远端刷新归属与访问令牌回读

> 安全交回和刷新协调已在 [第五批](sub2api-refresh-handoff.md) 实现；本文“未开放逆向交接”等说明是第三批历史状态，不应据此删除归属记录或手动回灌 RT。

> 第三批归属与回读规则继续有效。第四批已新增候选验证及按版本认证恢复，见 [第四批交付](sub2api-auth-recovery.md)。本文“尚未完成”描述的是第三批交付时状态。

已完成本地实现和隔离验证，未部署，未为现有账号启用委托。本批补上单向的刷新归属与反向访问令牌同步；完整的 grant 级全局单写、可信授权验证、受控清错和轮转仍未开放。

## 用户操作

账号详情的 Sub2API 面板新增“委托远端刷新”。先核对目标，再明确确认：采用此次远端访问令牌，移除本地刷新令牌、ID token 和会话凭据。取消确认只留下脱敏观察，不读取令牌、不修改凭据。

启用后显示“当前刷新方：Sub2API”，并提供“重新采用远端访问令牌”。此按钮是明确重新选择远端为准，可能替换本地新授权，因此每次都需要重新预览和确认。

Team 的统一刷新入口按持久化归属分流：

- 未委托账号继续原有刷新路径。
- 已委托账号只读取 Sub2API 的访问令牌，不调用上游刷新、不消费本地 RT、不在失败后回退本地刷新或自动 OAuth。
- 远端版本没有推进时等待；网络、实例、绑定或版本校验失败时保留本地凭据。
- 回读沿用现有刷新入口、探测开关和任务间隔。本批不会静默启用原本关闭的自动任务，也不会替 Sub2API 打开原生刷新配置。
- 接收成功会推进本地凭据版本，原有额度观察因此不能冒充当前授权结果。认证错误和人工运行状态不被清除；可用性需要单独验证。

## 数据保护

新增 `sub2api_refresh_authorities` 表，由既有 SQLite 启动建表流程创建。保存绑定指纹、实例 UUID、远端版本、本地版本和本地加密凭据指纹、归属 epoch、成功/失败时间及稳定错误码，不保存令牌明文。

预览前置条件同时绑定本地版本和本地凭据指纹。提交时在 SQLite 写事务中再次比较完整本地凭据字段、绑定和归属，并检查刷新租约。旧代码即使没有推进 credential_revision，其凭据变化也会被识别。

存在未结束的本地刷新时不能切换；过期但仍有 token 标识的旧刷新也视为结果未确认。后台回读必须持有有效租约，旧任务不能保存结果或释放其他任务的租约。

授权会话创建和委托也由同一个 SQLite 写锁排序。等待中的有效授权、以及结果未确认的换票会话会阻止委托/回读落库；换票已过期仍不推断它未执行。独立自动重授权入口、持久化会话创建和换票入口均核对归属。已委托账号跳过自动重授权，手动授权仍保留。

本地完成新授权后，自动回读停止覆盖，等待核对。绑定被删除也不删除归属记录，不会暗中恢复本地 RT 消费。多远端绑定账号暂时拒绝委托。

委托后的普通配置推送只发送显式配置，不把回读 AT 再推回远端，也不会因此移除 Sub2API 的 RT。新的本地授权仍需经过既有安全推送和后续明确核对。

## 接口

Team（管理员登录）：

```text
GET  /api/accounts/{id}/sub2api/refresh-authority
POST /api/accounts/{id}/sub2api/refresh-authority/preview
POST /api/accounts/{id}/sub2api/refresh-authority
```

预览/提交支持 workspace_id，但同一账号必须只有一个远端绑定。提交使用预览返回的 preconditions，包含 local_revision、local_fingerprint、remote_version、instance_id、binding_fingerprint、authority_epoch。浏览器只获得这些元数据和操作结果。

Sub2API（管理员 API Key，拒绝浏览器 JWT 会话）：

```text
POST /api/v1/admin/accounts/{id}/credential-sync-access-token
```

必须提供 expected_instance_id、expected_credential_version、expected_updated_at。只接受 OpenAI OAuth 非影子账号，版本必须大于零且快照仍一致，远端必须有明确 RT/client_id 配置。响应只含 AT、client_id 和版本元数据，不含 RT、ID token 或会话凭据；设置 no-store，请求体不进入审计。该接口不刷新、不写凭据、不清错。

普通账号 GET 保持原有脱敏。Team 不从脱敏 GET 猜测或提取令牌，不回退普通 GET。能力和状态接口新增 access_token_readback 标志，原同步 revision=3 保持兼容。

## 启用与回退

先升级 Sub2API 两个副本，再升级全部 Team 进程，核对管理员 API Key 与读取能力，再逐账号显式启用。第三批未新增 Sub2API 数据库迁移；第一、二批所需迁移仍须按发布计划执行。

启用前确认 Sub2API 的原生刷新已按预期配置；账号暂停、错误或额度阻断仍可能影响原生刷新和实际使用。

不要删除归属行来恢复本地刷新，也不要在自动任务运行时回退到不识别归属的 Team。回交 Team 需要先停止并排空 Sub2API 对对应 grant 的刷新，当前没有开放这一逆向交接流程。

本批只约束 Team 与 Sub2API 的单向职责，不保证 Sub2API 内部所有副本/入口的 grant 级互斥。因此 `single_writer` 仍为 false。跨数据库状态不能同事务提交；接收的是最后一次核对的版本，后续版本依靠再次回读收敛。

## 验证结果

- Python：100 项针对性及扩展回归断言通过，包括旧版本不推进的本地 SQL 写入、预览后修改、租约失效、绑定删除、后台只读接收、普通配置不回送 AT，以及活动 OAuth 会话阻断和自动授权归属检查。
- Go：service、admin handler、middleware、routes、server 五个包针对性测试/编译检查通过；验证错误实例/旧版本不返回 AT、只返回 AT、JWT 拒绝、API Key 允许、保留暂停和错误。
- JavaScript：28 项既有轮询和管理视图回归通过。
- Chromium + 隔离 FastAPI/SQLite：预览、取消、确认、状态更新、旧快照/实例变化、关闭后停止轮询通过；1440/390 宽度无横向溢出，无页面错误。响应断言确认 AT 未进入浏览器。
- 两仓库 diff 检查通过。未运行完整测试套件、生产迁移、真实上游授权验证或生产流量测试。

```text
.venv/Scripts/python.exe -m unittest tests.test_sub2api_refresh_authority tests.test_sub2api_access_readback tests.test_sub2api_remote_state tests.test_sub2api_credential_sync tests.test_sub2api_management tests.test_sub2api_sync_contract tests.test_sub2api_sync_persistence tests.test_reauth tests.test_quota_health tests.test_auth tests.test_oauth_security
node --test tests/runtime_ui.test.cjs tests/management_ui.test.cjs
.venv/Scripts/python.exe -m tests.browser_sub2api_state
```

扩展 Python 回归在 curl_cffi 资源清理时出现 `Event loop is closed` 的忽略异常，以及 Starlette/httpx 的弃用告警；测试断言通过。新增协议模块独立运行没有该清理告警。本批未修改相关依赖或扩展修复它们的生命周期。

## 尚未完成

1. 候选授权的真实上游身份、工作区和目标能力验证；元数据核对不能代替它。
2. 将认证错误绑定到触发它的凭据版本，并仅在新的可信授权验证后受控清错。
3. Sub2API 原生多入口/多副本的 grant 级 epoch、排空和旧刷新结果隔离，以及安全回交 Team。
4. 在以上基础上的自动轮转、排空观察和生产 canary 验收。
