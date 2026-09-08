# c6647fe 评审修复记录

参考用户提供的 `Team_review_c6647fe.md`，本轮仅修改 Team 仓库、本地离线测试和隔离预览。未访问 VPS，未修改 Sub2API 仓库，未操作真实账号或凭据。保留已有 CSS、菜单和邀请默认席位行为。

## 已落地

| 评审项 | 本轮处理 |
| --- | --- |
| F02 | 删除手动推送的 `auth_validated=True`。生产调用链默认仅同步凭据；鉴权分类不再由 OAuth、authentication 或任意包含 401 的文案触发。 |
| F03 | 共享同步入口写前核对远端 ID、邮箱、平台、OAuth 类型和工作区上下文；远端缺身份或上下文冲突时不 POST、不 PUT。手动推送要求已有绑定 verified；context-key 匹配目标也走共享校验。 |
| F04 | 写失败不再显示已同步；兼容写入和最终复读分别记录。窄接口校验操作 ID、远端 ID、契约版本及写入、缓存、恢复子步骤。最终 GET 的错误、暂停或未知状态进入 partial 和剩余阻断。 |
| F05 | 下架回执按目标 ID 检查 deleted/failed；冲突或失败回执保留 Binding，不执行本地 purge。客户端不再把批量 HTTP 成功视为所有目标已删除；单个删除要求明确成功回执或 204。 |
| F06 | 远端动作要求当前工作区下唯一 verified 绑定、合法 ID，并读取远端核对身份；暂停后读回同一 ID 且 schedulable=false 才确认成功。普通移除遇到未验证绑定也返回 partial。 |
| F07 | 传输层只对 GET 做有限重试。写请求超时或 5xx 返回未知结果，不自动连发；邀请未知后只读取邀请、成员状态，并返回禁止自动重试及同步建议。 |
| F08 | 撤邀首读旧 invited 状态继续复查；接受变 joined 不升级为踢人。复查有 15 秒 monotonic 总预算及每次剩余时间约束。待确认、状态变化持久化为 manual_required，并保留 outcome。已提交移除但未确认时不继续尝试其他成员 ID。 |
| F09（部分） | onboard/replenish 入口接入现有数据库锁；锁创建失败且查不到 blocker 时明确报错。新增独立连接的同工作区冲突、不同工作区并行测试。 |
| F10 | 搜索、用途、健康、团队筛选和视图切换清空选择；浏览器验证隐藏账号不再成为批量删除目标。 |
| 12.1 | body/email_addresses 不再误判为 seat_type，支持嵌套 detail/list。 |
| 12.2 | 席位设置生成请求局部快照，不在请求中累积覆盖全局映射；清空配置恢复默认映射。 |
| 12.3 | 远端搜索失败或重复 context-key 不再等同于未找到；绑定同步失败保留原绑定，不因普通 404 文案自动重建。 |

## 未完成及限制

- F01／跨仓库交付：未审查、提交或发布 Sub2API 服务端窄接口。本轮不调用广义 clear-error 作为替代。
- F02／自动恢复：尚未实现同次检查绑定账号、工作区、credential_revision、检查时间的完整证据链，也未接通 manual_reauthorize 原因。因此不能宣称重新授权后已自动恢复旧错误；当前选择安全的 credentials_only。
- F06／排空保证：普通移除的产品语义仍是“官方离开后停止新调度”，不是“先暂停、排空，再移除”。暂停后的固定等待也不是在途请求确已结束的证据。
- F09／过期执行者：新增入口互斥与真实连接测试不代表所有入口、父子任务、租约回收均已完成 fencing。旧 worker 晚返回仍需专项设计和验证，不能据此宣称完整多 worker 破坏性操作安全。
- 本地绑定与远端身份读写之间仍有时间窗口；旧服务兼容 PUT 不具备服务端 CAS 保障。窄接口服务端必须独立校验身份、版本和可恢复错误类别。
- 未验证生产 CSS 版本、真实官方邀请 POST／接受后席位、真实 Sub2API 集成及测试弹窗缓存。

## 本地验证

- 使用项目自带 `.venv/Scripts/python.exe`，未安装新依赖。
- 最后一轮全量 pytest：327 passed，8 warnings，22 subtests passed。
- 定向安全回归与 Sub2API 管理测试：21 passed，7 subtests passed，包含新增并发用例。最终补充业务失败回执检查后，安全回归再次通过：15 passed，7 subtests passed。
- Node UI 测试：28 passed。
- Playwright：1440px／390px 隔离预览，覆盖确认取消、服务端拒绝、重试、批量删除，以及搜索／视图切换后选择清空；无 pageerror。所有账号写请求均被拦截。
- Python compileall 与 git diff --check 通过。

测试入口：`tests/test_review_safety.py`、`tests/test_member_lifecycle.py`、`tests/test_sub2api_credential_sync.py`、`tests/test_workspace_lock_and_pause.py`、`tests/browser_account_deletion.py`。

浏览器测试支持 `TEAM48_PREVIEW_URL` 指向 `tests.preview_app:app` 隔离预览，不应指向生产服务。
