# 48 Team Manager UI 业务状态优化实施与维护文档

> 交接对象：后续负责前端实现、修复和验收的模型或开发者
>
> 实施前基线：`main` 分支提交 `71b589d`
>
> 本文描述现有 UI 业务状态优化的真实代码落点、模块边界和继续修改时的验收要求。当前代码已经包含核心实现，不要另起第二套 Overlay Manager、授权状态判断、成员状态机或 Operation 轮询器。

## 1. 目标、现状与边界

### 1.1 业务目标

本轮 UI 业务状态优化解决以下问题：

1. `members/link -> needs_auth` 后，团队详情和授权流程不能同时显示为两个主弹层。
2. 母号授权失效时，团队列表和团队详情必须提供直接、明确的授权入口。
3. 成员管理统一为“官方成员 / 本地已接入账号 / 接入本地 / 授权 / 移出官方 / 从本地移除”，不再维护另一套“子号管理”产品概念。
4. 日常页面只消费当前用户触发的 Operation，不默认加载完整 Operation 历史。
5. 所有主弹层由唯一 Overlay Manager 管理，包括显示、替换、关闭、焦点、背景隔离和滚动锁。
6. 读取请求采用 latest-wins，写请求采用 stable key 防重，避免双击和竞态造成重复业务写入。
7. 后端稳定业务字段和错误代码直接驱动 UI，不在前端继续扩大推断规则。

### 1.2 当前实现状态

当前 `app/web/static/js/app.js` 已有以下核心实现：

- `overlayState`、`overlayRegistry` 和统一 Overlay Manager。
- `authStatus()` 和仅用于旧响应兼容的 `legacyAuthStatus()`。
- `workspacePrimaryAction()`、`runWorkspacePrimaryAction()`。
- `presentTeamMember()`、`teamMemberKind()` 和统一成员动作表。
- `linkTeamMember()`、`showTeamAuthStep()` 和团队详情内联授权流程。
- `readControllers`、`readVersions`、`inFlightWrites` 和 `fetchEntity()` 读写分流。
- `currentOperations`、`startCurrentOperation()`、`pollCurrentOperation()` 和 `resumeCurrentOperations()`。
- `RequestError`、`parseJsonResponse()`、`friendlyError()`。

后续修改应在这些函数上增量修复，不得创建 `overlayManager2`、第二个 `needsAuth` 判断器、第二套成员 presenter 或全局 Operation 轮询器。

### 1.3 必须保留

- 后端持久化 Operation runner、`operation_id`、恢复、幂等、重试、审计和失败记录。
- `GET /api/operations/{id}` 和 `/operations` 诊断页。
- 现有 Workspace、Account、Membership、官方成员快照和账号 reauth endpoints。
- 所有危险操作现有确认步骤。
- `entity-sheet` 作为团队详情以及团队详情内联授权的同一个主容器。
- `reauth-sheet` 作为账号列表等独立上下文发起授权的主容器。

### 1.4 禁止事项

- 通过增加 `z-index` 修复双弹层。
- 根据 `owner_email` 猜测、创建或绑定母号账号。
- 在前端新增第二套母号授权 API。
- 授权失败后重新执行 `members/link`。
- 普通页面默认请求最近 7 天、30 天或全部 Operation 列表。
- 用 `AbortController` 取消写请求后再次发送来实现防重。
- 在 DOM 中直接显示后端堆栈、长 JSON 或完整原始 HTTP body。
- 修改 `/opt/sub2api`、其 Compose、数据库、Redis 或 Nginx。

## 2. 模块与文件总览

| 模块 | 主要文件 | 关键符号或区域 | 文件职责 |
| --- | --- | --- | --- |
| 授权状态规范化 | `app/application/presenters.py` | `build_auth_status()` | 生成统一 `auth_state/needs_auth/auth_action/auth_reason` |
| 团队和账号读模型 | `app/application/queries/identity.py` | `workspaces_query()`、`accounts_query()` | 输出母号授权字段、成员对账状态和本地账号字段 |
| Console 读模型封装 | `app/application/queries/console.py` | `workspaces()`、`accounts()` | 补充额度等 Console 展示数据，不重新推断授权状态 |
| 成员接入业务 | `app/application/console_maintenance.py` | `link_remote_only_member()` | 校验官方快照、创建/复用 Account、建立 Membership、防重复接入 |
| 授权业务 | `app/application/reauth.py` | `start_manual_reauth()`、`complete_manual_reauth()` | 生成授权会话、校验回调、保留旧授权直到成功 |
| Console 动作门面 | `app/application/console_actions.py` | `account_reauth()`、`account_reauth_complete()` 及 maintenance re-export | 给 API route 提供稳定动作入口 |
| HTTP API | `app/web/routes/api.py` | `link_workspace_member()`、`reauth_account()`、`complete_account_reauth()`、`operation_detail()` | 鉴权、schema 入参、HTTP 状态和结构化错误 |
| API schema | `app/web/schemas/resources.py`、`app/web/schemas/workspaces.py` | `WorkspaceLinkMemberRequest`、`WorkspaceRemoveChildRequest`、`WorkspaceMemberRolePatch`、`CompleteAccountOAuthRequest` | 限制前端请求字段和类型 |
| 前端业务控制器 | `app/web/static/js/app.js` | 本文后续列出的所有函数 | 状态机、渲染、请求、弹层、当前任务和用户反馈 |
| 共享模板 | `app/web/templates/console.html` | `entity-sheet`、`reauth-sheet`、其他 `.sheet`、`action-menu`、`toast-region` | 提供唯一稳定 DOM 容器和可访问性语义 |
| 组件样式 | `app/web/static/css/components.css` | `.sheet*`、`.team-*`、`.row-actions*`、`.toast*`、移动断点 | 只负责布局和状态视觉，不承载业务判断 |
| 静态 UI 合同 | `tests/test_ui_contract.py` | `UIContractTests` | 防止旧 UI 概念和旧弹层 API 回归 |
| 成员接入测试 | `tests/test_management_context.py` | `ManagementContextTests` | 验证官方成员接入、重复接入、母号保护和 Membership 唯一性 |
| 团队读模型测试 | `tests/test_official_member_logic.py` | `WorkspaceQueryAndSyncTests` | 验证母号授权字段和缺失状态 |
| 授权业务测试 | `tests/test_reauth.py` | `ManualReauthLinkTests` | 验证回调错误、旧授权保留和团队健康更新 |
| Operation API 测试 | `tests/test_operation_api.py` | `OperationApiTests` | 验证详情、取消和安全重试白名单 |

## 3. 后端 API 合同与具体改动

前端必须优先消费后端显式字段。授权、成员状态和错误原因的业务真相在后端，前端只负责展示和交互阶段。

### 3.1 账号授权字段

接口：

- `GET /api/accounts`
- `GET /api/accounts/portfolio`
- 团队读模型中的本地账号和 reconciliation item

标准字段：

```json
{
  "auth_state": "oauth_required",
  "needs_auth": true,
  "auth_action": "authorize",
  "auth_reason": "missing_token"
}
```

字段语义：

- `auth_state`：后端规范化后的持久状态。
- `needs_auth`：是否需要用户处理授权。
- `auth_action`：`authorize | reauthorize | null`。
- `auth_reason`：稳定原因代码或 `null`。

具体文件改动：

- `app/application/presenters.py`
  - `AUTH_NEED_STATES` 维护需要人工授权处理的持久状态集合。
  - `build_auth_status()` 是唯一后端授权 presenter。
  - 没有账号对象时返回调用方传入的 `missing_reason`，不得猜测账号。
  - 没有 Access Token 时返回 `missing_token`，并根据当前状态给出 `authorize` 或 `reauthorize`。
  - `deactivated` 不提供可执行 `auth_action`，避免 UI 对停用账号继续发起授权。
- `app/application/queries/identity.py`
  - `accounts_query()` 对每个账号展开 `**build_auth_status(account)`。
  - `workspaces_query()` 对母号调用 `build_auth_status(owner, missing_reason="owner_account_missing")`。
  - 成员 `managed.accounts` 和 `reconciliation.items` 同样携带规范化授权字段。
- `app/web/static/js/app.js`
  - `authStatus()` 优先读取显式字段。
  - `legacyAuthStatus()` 只为旧接口响应提供最小兼容，不再添加新的推断条件。
  - `needsAuth()` 只是 `authStatus(item).needsAuth` 的薄封装。
- `tests/test_official_member_logic.py`
  - 验证账号和母号返回相同授权判断。
  - 覆盖 `oauth_required -> authorize`、健康状态和母号档案缺失。

旧字段 `auth`、`owner_auth`、`has_access_token` 可以暂时保留兼容，但新 UI 不得优先依赖它们。

### 3.2 工作区母号字段

`GET /api/workspaces` 正常返回：

```json
{
  "owner_account_id": 123,
  "owner_email": "owner@example.com",
  "owner_auth_state": "refresh_due",
  "owner_needs_auth": true,
  "owner_auth_action": "reauthorize",
  "owner_auth_reason": "refresh_due"
}
```

无本地母号档案时返回：

```json
{
  "owner_account_id": null,
  "owner_auth_state": "owner_account_missing",
  "owner_needs_auth": false,
  "owner_auth_action": null,
  "owner_auth_reason": "owner_account_missing",
  "health": "owner_account_missing"
}
```

具体文件改动：

- `app/application/queries/identity.py`
  - `workspaces_query()` 从 `workspace.owner_account_id` 查找真实 Account。
  - 输出 `owner_account_id` 和完整 `owner_auth_*` 字段。
  - 将母号缺失映射到 `health=owner_account_missing`。
  - 不按 `owner_email` 自动创建 Account。
- `app/application/queries/console.py`
  - `workspaces()` 只附加母号额度快照，不改变 `owner_auth_*`。
- `app/web/static/js/app.js`
  - `workspacePrimaryAction()` 和 `renderTeamDetails()` 直接读取这些字段。
- `tests/test_official_member_logic.py`
  - `test_workspace_owner_auth_fields_use_canonical_presenter`。
  - `test_workspace_without_owner_has_explicit_missing_state`。

母号档案缺失时，UI 显示“母号本地档案缺失”和“去账号核对”，不得调用授权接口或团队登记接口。

### 3.3 成员接入响应

接口：`POST /api/workspaces/{workspaceId}/members/link`

成功响应：

```json
{
  "ok": true,
  "workspace_id": 1,
  "account_id": 23,
  "email": "member@example.com",
  "created": true,
  "status": "managed",
  "auth_state": "oauth_required",
  "needs_auth": true,
  "auth_action": "authorize",
  "auth_reason": "missing_token"
}
```

具体文件改动：

- `app/web/schemas/resources.py`
  - `WorkspaceLinkMemberRequest` 只接受 `email` 和可选 `account_id`。
- `app/web/routes/api.py`
  - `link_workspace_member()` 调用 `console_actions.link_remote_only_member()`。
  - `not_found/account_not_found` 返回 `404`，其他业务拒绝返回 `400`。
  - 使用 `_error_detail()` 时保留 `message` 和 `error_code`。
- `app/application/console_maintenance.py`
  - `link_remote_only_member()` 必须先确认邮箱存在于该 Workspace 的官方成员快照。
  - `invited` 成员在真正加入前不能接入本地。
  - 有 `account_id` 时校验账号存在且邮箱一致；无账号时按邮箱复用或创建 child Account。
  - 不允许把 Workspace 母号作为普通成员接入。
  - 已存在 joined/invited Membership 时返回 `already_linked`，不重复创建。
  - 成功后返回 Account ID、Membership ID、`created` 和统一授权字段。
- `tests/test_management_context.py`
  - 覆盖新建账号、重复接入、Membership 唯一、invited 拒绝和母号保护。

### 3.4 授权 API

只使用：

```text
POST /api/accounts/{accountId}/reauth
POST /api/accounts/{accountId}/reauth/complete
```

具体文件改动：

- `app/web/routes/api.py`
- `app/web/schemas/workspaces.py`
  - `CompleteAccountOAuthRequest` 约束 `ticket` 和完整 `callback_url`；授权 complete 的 schema 不在 `resources.py`。
  - `reauth_account()` 开始授权会话。
  - `complete_account_reauth()` 接收 ticket 和完整 callback URL。
  - 错误通过 `_error_detail()` 返回对象型 `detail`。
- `app/application/console_actions.py`
  - `account_reauth()`、`account_reauth_complete()` 只做 Account 存在性检查并委托给 `reauth_service`。
- `app/application/reauth.py`
  - `start_manual_reauth()` 生成 ticket 和 `authorize_url`，记录原授权状态，但不覆盖账号原有 token 和 `auth_state`。
  - `complete_manual_reauth()` 验证 ticket、账号、邮箱和 callback；只有换票成功后才更新 token。
  - ticket 不存在返回 `callback_expired`。
  - callback 缺 code 或 OAuth 错误返回 `callback_invalid`。
- `tests/test_reauth.py`
  - `test_start_and_failed_complete_preserve_existing_auth` 防止失败授权破坏旧凭证。
  - `test_expired_callback_has_stable_error_code` 固定过期错误码。
  - `test_owner_reauth_updates_workspace_auth_health` 验证成功后工作区读模型更新。

授权成功后前端重新请求 `/api/workspaces`。不要自动执行额度探测，也不要重新执行 `members/link`。

### 3.5 Operation API

普通页面只允许按 ID 查询当前操作：

```text
GET /api/operations/{operationId}
```

完整诊断页才使用：

```text
GET /api/operations?...filters...
```

具体文件改动：

- `app/web/routes/api.py`
  - `operation_detail()` 提供单任务轮询。
  - `operations()` 提供诊断页筛选、分页和日期范围。
  - `operation_cancel()`、`operation_retry()` 保留取消及白名单重试。
- `app/application/console_actions.py`
  - `get_operation_detail()` 返回步骤、日志、可取消和可重试能力。
  - retry 必须继续受服务端白名单约束，前端按钮不是安全边界。
- `app/application/queries/console.py`
  - `operations()` 负责诊断列表查询；普通页面不调用它。
- `tests/test_operation_api.py`
  - 验证按 ID 详情、取消和安全重试白名单。

## 4. 唯一 Overlay Manager

### 4.1 修改文件

- 主实现：`app/web/static/js/app.js`
- DOM 容器：`app/web/templates/console.html`
- 布局样式：`app/web/static/css/components.css`
- 回归合同：`tests/test_ui_contract.py`

### 4.2 `app.js` 具体改动

文件顶部只保留一份全局状态：

```js
const overlayState = {
  active: null,
  returnFocus: null,
  context: null,
  previousContext: null,
};
```

`overlayRegistry` 统一登记以下主弹层：

- `entity` -> `#entity-sheet`
- `register` -> `#register-sheet`
- `reauth` -> `#reauth-sheet`
- `phone-import` -> `#phone-import-sheet`
- `proxy-add` -> `#proxy-add-sheet`
- `proxy-edit` -> `#proxy-edit-sheet`

唯一允许直接控制主弹层显示状态的函数：

- `overlayElement()`、`overlayNameOf()`：查找 registry。
- `showOverlayElement()`、`hideOverlayElement()`：内部 DOM 显示/隐藏。
- `openOverlay()`：打开一个主弹层；已有不同主弹层时报告错误。
- `replaceOverlay()`：保存当前上下文、无焦点回跳地关闭，再打开目标弹层。
- `closeOverlay()`：关闭 active overlay。
- `restoreOverlayContext()`：恢复 `previousContext`。
- `getActiveOverlay()`：键盘和其他控制器查询当前弹层。
- `deactivateOverlay()`：集中清理状态、焦点陷阱和背景锁。
- `setOverlayLock()`：设置 `.shell.inert`、`aria-hidden`、body class 和滚动锁。
- `activateFocusTrap()`、`handleFocusTrap()`、`clearFocusTrap()`：处理 Tab 焦点循环。
- `assertSinglePrimaryOverlay()`：开发期检查同一时刻至多一个主弹层。

必须删除并持续禁止：

```text
overlayReturn
activeModal
openModalOverlay
closeModalOverlay
```

页面底部事件绑定必须满足：

- 点击遮罩只关闭当前 registry 中的 overlay。
- `Escape` 先关闭 `#action-menu`，否则只关闭 `getActiveOverlay()`。
- `Ctrl/Cmd+K` 只在没有主 overlay 时打开命令面板。
- 各业务 close helper，例如 `closeSheet()`、`closeReauth()`，最终都委托 `closeOverlay()`。

### 4.3 `console.html` 具体改动

共享模板只提供稳定容器，不在模板里放业务状态判断：

- `#entity-sheet`：账号详情、任务详情、团队详情和团队内联授权共用。
- `#reauth-sheet`：账号列表等独立上下文的授权。
- `#register-sheet`：登记团队。
- `#phone-import-sheet`、`#proxy-add-sheet`、`#proxy-edit-sheet`：资源表单。
- 每个 `.sheet-panel` 必须有 `role="dialog"`、`aria-modal="true"` 和有效的 `aria-labelledby`。
- close 按钮保留稳定 `data-close-*` 属性供 `app.js` 绑定。
- `#action-menu` 是菜单，不注册为主 overlay。
- `#toast-region` 保留 `aria-live="polite"`。

不要为 Team 授权再新增 `team-reauth-sheet`。团队详情内授权直接替换 `#sheet-body` 内容。

### 4.4 `components.css` 具体改动

- `.sheet`、`.sheet[hidden]`、`.sheet-panel`：负责遮罩和侧边抽屉布局。
- `.menu`、`.menu[hidden]`：独立菜单层，不能伪装成第二个 dialog。
- `body.overlay-open` 如需补充样式，只处理滚动，不写业务状态。
- `@media (max-width: 720px)` 下 `.sheet-panel` 宽度为 `100%`，不产生横向滚动。

不得通过提高某个授权 sheet 的 `z-index` 隐藏双弹层问题。

### 4.5 Overlay 验收

- 任意时刻 `.sheet:not([hidden]) [aria-modal="true"]` 数量不超过 1。
- Tab 和 Shift+Tab 不离开 active dialog。
- 关闭后焦点返回触发按钮；触发按钮因列表刷新被替换时，回退到同一实体新行或 `#page-root`。
- 背景 `.shell` 在打开时 inert，关闭后恢复。
- Escape 不会按 DOM 顺序批量隐藏多个 sheet。

## 5. 团队母号主要动作

### 5.1 修改文件

- 后端字段：`app/application/presenters.py`、`app/application/queries/identity.py`
- 前端动作和渲染：`app/web/static/js/app.js`
- 行内动作样式：`app/web/static/css/components.css`
- 测试：`tests/test_official_member_logic.py`、`tests/test_ui_contract.py`

### 5.2 `app.js` 具体改动

`workspacePrimaryAction(workspace)` 是唯一选择器：

```js
function workspacePrimaryAction(workspace) {
  if (workspace.owner_auth_reason === "owner_account_missing" ||
      workspace.owner_auth_state === "owner_account_missing") {
    return { id: "owner-missing", label: "去账号核对" };
  }
  if (workspace.owner_needs_auth && workspace.owner_account_id) {
    return {
      id: "owner-auth",
      label: workspace.owner_auth_action === "authorize" ? "授权" : "重新授权",
    };
  }
  if ((workspace.official?.sync_state ?? "never") === "never") {
    return { id: "sync", label: "同步" };
  }
  return { id: "manage", label: "管理" };
}
```

调用位置：

- `workspaceRow()`：团队表格每行显示主要动作，并保留 `menuButton("workspace", item)` 作为次级入口。
- `runWorkspacePrimaryAction()`：
  - `owner-missing` 跳转 `/accounts`。
  - `owner-auth` 打开团队详情后调用 `showTeamAuthStep()`。
  - `sync` 复用 `entityActions.workspace` 中的 `workspace.sync`。
  - `manage` 调用 `openWorkspaceDetails()`。
- `renderTeamDetails()` 的“母号”区域：再次调用相同 selector，保证列表和详情行为一致。

样式落点：

- `.row-action-host`、`.row-actions-contextual` 控制表格行内动作。
- `.team-mother-grid`、`.team-mother-aside` 控制详情中的母号信息和动作。
- 移动端将 `.team-mother-grid` 改为单列。

禁止：

- 母号健康时继续显示授权按钮。
- 将删除、踢出或轮转设为主要动作。
- 母号缺失时调用 `/api/workspaces/oauth/start` 或按邮箱创建母号。

## 6. 成员状态和动作统一

### 6.1 修改文件

- 成员数据源：`app/application/queries/identity.py`
- 接入业务：`app/application/console_maintenance.py`
- 请求 schema 和路由：`app/web/schemas/resources.py`、`app/web/routes/api.py`
- 前端 presenter 和动作：`app/web/static/js/app.js`
- 布局：`app/web/static/css/components.css`
- 测试：`tests/test_management_context.py`、`tests/test_ui_contract.py`

### 6.2 后端数据结构

`workspaces_query()` 输出两类数据：

- `managed.accounts`：已有本地 Account 和 Membership 的成员。
- `reconciliation.items`：官方快照与本地关系对账后的 `managed/remote_only/local_only/invited/conflict/owner`。

每个可操作成员至少提供：

```text
email
status
remote_state
local_account_id 或 candidate_account_id
auth_state / needs_auth / auth_action / auth_reason
role / official_role
user_id / official_user_id
```

### 6.3 前端具体改动

`app.js` 中的职责必须保持单一：

- `teamMemberRows(workspace)`：合并 `managed.accounts` 与 `reconciliation.items`，按规范化邮箱去重，排除母号。
- `teamMemberKind(row)`：只做后端状态到规范类别的兼容归一。
- `presentTeamMember(row)`：唯一决定状态文案、主要动作和次级动作。
- `teamMemberState(row)`：将 presenter 结果转换为视觉状态。
- `renderTeamMember()`：只根据 presenter 结果创建 DOM，不再重复判断业务状态。
- `teamMemberMenu()`：只渲染 presenter 给出的 `secondaryIds`。
- `entityActions.team`：保存所有成员命令和 endpoint，不在多个渲染函数里复制请求逻辑。

统一状态矩阵：

| 状态 | 文案 | 主要动作 | 次级动作 |
| --- | --- | --- | --- |
| `remote_only` | 官方已加入，未接入本地 | `team.member.link` | 无 |
| `managed + needs_auth` | 已接入，需授权 | `team.member.reauth` | 角色、移出官方、仅移出本地 |
| `managed + healthy` | 已接入 | 无 | 角色、移出官方、仅移出本地 |
| `invited` | 等待接受邀请 | 无 | 撤回邀请 |
| `local_only` | 本地有记录，官方未找到 | 无自动动作 | 仅移出本地 |
| `owner` | 母号 | 授权/重新授权或同步 | 不允许普通成员删除 |
| `conflict` | 身份冲突 | 无自动关联 | 仅允许核对或安全清理 |

`entityActions.team` 的稳定命令 ID：

- `team.member.invite`
- `team.member.link`
- `team.member.reauth`
- `team.member.role`
- `team.member.local-remove`
- `team.member.official-remove`
- `team.member.purge`

危险动作必须继续经过 `confirmDanger()`，服务端仍需独立验证母号保护、成员存在性和 Operation 冲突。

### 6.4 文案清理

- 页面主要命令使用“邀请成员”“接入本地”“移出官方席位”“仅移出本地”。
- 不再出现“创建子号”“管理子号”“接入子号”“更换子号”入口。
- 不新增独立的子号管理页、子号管理 sheet 或第二套成员表。
- 后端旧兼容 endpoint 可以保留，但新 UI 不暴露旧概念。

### 6.5 CSS 落点

- `.team-member-list`：成员列表边界。
- `.team-member-row`：桌面三列布局，固定状态列和动作列。
- `.team-member-identity`、`.team-member-state`：允许内容收缩，防止长邮箱撑破布局。
- `.member-action-menu`、`.member-action-options`：次级命令菜单。
- `renderTeamMember()` 给目标成员添加 `.is-selected`；`components.css` 使用 `.team-member-row.is-selected` 提供背景、左侧强调条和边界高亮。
- `@media (max-width: 720px)`：成员行改为身份/状态加右侧动作的两列布局。

## 7. `members/link -> reauth` 状态机

### 7.1 修改文件

- 前端状态机：`app/web/static/js/app.js`
- 共用容器：`app/web/templates/console.html` 的 `#entity-sheet`
- 接入 API：`app/web/routes/api.py`、`app/application/console_maintenance.py`
- 授权 API：`app/web/routes/api.py`、`app/application/console_actions.py`、`app/application/reauth.py`
- 测试：`tests/test_management_context.py`、`tests/test_reauth.py`、`tests/test_ui_contract.py`

### 7.2 状态和上下文

状态流：

```text
workspace_detail
  -> linking_member
  -> authorization_required
  -> authorizing
  -> authorization_completed
  -> workspace_detail
```

状态保存在 `teamDetailState`：

```js
{
  workspaceId,
  workspace,
  view,
  account,
  selectedMemberEmail,
  scrollTop,
  stage,
}
```

具体函数职责：

- `openWorkspaceDetails(trigger, workspace)`：初始化团队上下文并打开 `entity` overlay。
- `renderTeamDetails(workspace)`：渲染团队概览、母号、邀请、成员和危险区。
- `fetchTeamDetails(workspaceId)`：刷新团队列表并取回原 Workspace。
- `reloadTeamDetails()`：保留滚动位置，按 `selectedMemberEmail` 定位成员。
- `linkTeamMember(workspace, row, button)`：只负责一次 link 请求和后续分支。
- `showTeamAuthStep(account)`：在当前 `entity-sheet` 内替换内容，不打开 `reauth-sheet`。

### 7.3 精确流程

1. 点击“接入本地”。
2. `linkTeamMember()` 设置 `stage=linking_member` 和 `selectedMemberEmail`。
3. 调用 `setButtonBusy()`，并给成员行设置 `aria-busy=true`。
4. 使用 stable key `workspace:${workspace.id}:member:${email}:link` 调用一次 `/members/link`。
5. 请求成功后重新读取该 Workspace。
6. `needs_auth=false` 时直接 `renderTeamDetails(refreshed)`，成员状态变为“已接入”。
7. `needs_auth=true` 时保存响应的 `account_id`，设置 `stage=authorization_required`，调用 `showTeamAuthStep()`。
8. `showTeamAuthStep()` 保存当前 sheet 的 `scrollTop`，在同一个 `#sheet-body` 渲染授权表单。
9. 开始授权使用 stable key `account:${account.id}:reauth:start`。
10. 完成授权使用 stable key `account:${account.id}:reauth:complete:${ticket}`。
11. 完成按钮通过 `setButtonBusy()` 禁用，重复 submit 由 `inFlightWrites` 返回同一 Promise。
12. 成功后设置 `stage=authorization_completed`，调用 `reloadTeamDetails()`，恢复原 Workspace、原成员和滚动位置。
13. “返回团队详情”直接调用 `renderTeamDetails(teamDetailState.workspace)`，不提交成功状态。
14. Escape 关闭当前 `entity-sheet`；不能再额外关闭一个隐藏的授权 sheet。

### 7.4 失败和重试

- link 返回 `already_linked`：显示 warning，刷新详情，不再创建 Account 或 Membership。
- link 的其他失败：允许重试 link，但 stable key 防止同一时刻重复提交。
- reauth start 失败：保留 Workspace 和 Account 上下文，允许重新生成链接。
- reauth complete 失败：保留 callback 输入、ticket 和成员定位上下文。
- 只有 `callback_expired` 显示“重新生成链接”。
- 授权重试只能调用 reauth start/complete，不得再次调用 `/members/link`。
- 授权完成后只刷新 Workspace，不自动调用 quota probe。

## 8. Current Operation Controller

### 8.1 修改文件

- 前端控制器：`app/web/static/js/app.js`
- API：`app/web/routes/api.py`
- Operation 数据和动作：`app/application/console_actions.py`、`app/application/queries/console.py`
- 测试：`tests/test_operation_api.py`、`tests/test_operations.py`、`tests/test_ui_contract.py`

### 8.2 `app.js` 具体改动

全局状态：

```js
const currentOperations = new Map();
const CURRENT_OPERATION_STORAGE = "team48:current-operations";
```

每个 entry 包含：

```text
stableKey
operationId
entityType
entityId
action
state
controller
timer
startedAt
successMessage
```

函数职责：

- `startCurrentOperation()`：仅接管带 `operation_id` 且处于活动状态的动作响应。
- `persistCurrentOperations()`：只把可序列化的最小上下文写入 `sessionStorage`。
- `resumeCurrentOperations()`：刷新页面后恢复少量当前操作，不查询历史列表。
- `pollCurrentOperation()`：只轮询 `/api/operations/{id}`。
- `operationPollDelay()`：按已运行时间递增轮询间隔。
- `finishCurrentOperation()`：停止 timer/controller、删除 Map 和 storage 记录、显示反馈并刷新相关读模型。
- `cancelCurrentOperationPolling()`：只取消前端轮询，不取消后端 Operation。
- `stopPolling()`：页面卸载或不可见时停止网络轮询。
- `openOperationById()`：用户点击“技术详情”时按 ID 打开任务详情。
- `handleActionResult()`：同步结果直接反馈；异步 Operation 转交当前任务控制器。

活动状态：

```text
pending / queued / running / waiting
```

终态：

```text
success / failed / manual_required / cancelled / partial
```

### 8.3 普通页与诊断页边界

- `bootPage()` 调用当前页面 bootstrap 后执行 `resumeCurrentOperations()`。
- `/workspaces`、`/accounts`、资源页和总览不得调用 `/api/operations` 列表。
- `bootOperations()` 是唯一正常加载 `/api/operations?${query}` 的页面 bootstrap。
- `/operations` 保留搜索、状态、类型、来源、日期范围、分页、软归档和恢复。
- 普通 UI 默认只显示友好结果；只有失败且有 `operation_id` 时提供“技术详情”。
- `beforeunload` 和 `visibilitychange` 停止前端 polling，但不发送后端 cancel。

## 9. 请求防重与竞态

### 9.1 修改文件

- 实现：`app/web/static/js/app.js`
- 静态合同：`tests/test_ui_contract.py`
- 服务端最终幂等和冲突保护：对应 application service 与 Operation store，不由前端替代。

### 9.2 读取请求

状态：

```js
const readControllers = new Map();
const readVersions = new Map();
```

相关函数：

- `abortEntity(key)`：中止相同 read key 的旧请求。
- `nextReadVersion(key)`：递增 key 的请求版本。
- `isLatestRead(key, version)`：确认响应仍是最新。
- `fetchEntity()`：GET/HEAD/OPTIONS 走 latest-wins。
- `isAbortError()`：统一识别主动中止和 stale read。

规则：

- 相同 read key 可以 abort 前一个请求。
- 只有最新 version 可以继续更新 cache 或 DOM。
- `AbortError` 不显示 toast，也不覆盖页面错误。
- 读 key 必须代表同一份可替换资源，例如 `workspace-list`、`account-list`、`operation-${id}`。

### 9.3 写请求

状态：

```js
const inFlightWrites = new Map();
```

相关函数：

- `writeEntity()`：相同 key 已在运行时返回同一个 Promise。
- `fetchEntity()`：根据 method 自动把非读取请求分发到 `writeEntity()`。
- `postAction()`、`patchAction()`、`deleteAction()`：统一生成 method/header/body。
- `setButtonBusy()`：禁用触发按钮并设置 `aria-busy=true`。

stable key 示例：

```text
workspace:${id}:member:${email}:link
workspace-sync-${id}
account:${id}:reauth:start
account:${id}:reauth:complete:${ticket}
account-quota-${id}
account-sub2api-push-${id}
workspace-role-${workspaceId}-${email}
workspace-remove-${workspaceId}-${accountId}
```

规则：

- 写请求不使用 abort 防重。
- 同一 stable key 在 Promise settle 前不得发出第二个 HTTP 请求。
- key 必须包含足以区分业务目标的实体和上下文。
- Promise settle 后在 `finally` 删除 key。
- 按钮禁用是交互反馈，`inFlightWrites` 才是前端请求防重层；后端仍需幂等和冲突检查。

## 10. 友好错误映射

### 10.1 修改文件

- 后端稳定错误：`app/application/console_maintenance.py`、`app/application/reauth.py` 及对应 service。
- HTTP 错误结构：`app/web/routes/api.py` 的 `_error_detail()`。
- 前端解析和文案：`app/web/static/js/app.js`。
- 测试：`tests/test_reauth.py`、`tests/test_management_context.py`、`tests/test_ui_contract.py`。

### 10.2 `app.js` 具体改动

- `RequestError` 保存 `status`、`payload` 和 `errorCode`。
- `parseJsonResponse()` 同时兼容字符串 `detail` 和对象 `detail`。
- `extractErrorPayload()`、`extractErrorCode()`、`extractErrorMessage()` 集中解析错误。
- `ERROR_CODE_MESSAGES` 保存面向用户的稳定中文文案。
- `friendlyError()`：
  - `AbortError` 返回空字符串。
  - 优先按 `error_code` 映射。
  - 兼容少量旧文本错误。
  - 长文本、JSON 和数组形式降级为“请求失败，请重试。”。
- `toast()` 根据 tone 设置 `role=status` 或 `role=alert`，可选“重试”或“技术详情”动作。

至少维护以下映射：

| `error_code` | 用户文案 |
| --- | --- |
| `missing_token` | 该账号尚未授权，请先完成授权。 |
| `token_revoked` / `token_invalidated` | 账号授权已失效，请重新授权。 |
| `callback_invalid` | 授权回调无效，请重新复制完整回调地址。 |
| `callback_expired` | 授权回调已过期，请重新生成授权链接。 |
| `account_not_found` | 本地账号档案不存在，请先核对账号关系。 |
| `owner_account_missing` | 该团队没有绑定本地母号，暂时无法直接授权。 |
| `identity_conflict` | 该账号身份存在冲突，请先核对账号关系。 |
| `already_linked` | 该成员已接入本地，无需重复接入。 |
| `membership_not_found` | 未找到该成员的本地关系，请刷新后重试。 |
| `operation_in_progress` / `operation_conflict` | 相同操作正在进行，请等待当前操作完成。 |

## 11. 模板与 CSS 的修改边界

### 11.1 `app/web/templates/console.html`

允许修改：

- 增加稳定 ID、`data-*` hook、`aria-*` 属性和语义化按钮。
- 修正 dialog title 关联、表单 label、live region 和 close button。
- 调整页面已有容器，便于 `app.js` 渲染 Team 详情。

不要修改：

- 不在 Jinja 模板中复制授权和成员业务判断。
- 不为同一业务流程再建第二套 Team sheet。
- 不使用内联 onclick 或内联业务脚本。
- 不把后端原始错误直接插入 HTML。

### 11.2 `app/web/static/css/components.css`

允许修改：

- `.sheet*`、`.team-*`、`.row-actions*`、`.toast*` 的布局和响应式规则。
- 为 `aria-busy`、选中成员、焦点可见性增加状态样式。
- 修复 390px 移动端文字溢出、动作换行和 sheet 宽度。

不要修改：

- CSS 不决定 `needs_auth`、成员类别或可执行动作。
- 不用 `z-index` 掩盖状态管理错误。
- 不复制 tokens；颜色、间距和圆角继续使用现有 CSS variables。

## 12. 按文件执行清单

### 12.1 `app/web/static/js/app.js`

继续修改前应按以下顺序定位：

1. 文件顶部：请求 Map、Operation Map、Overlay state 和 registry。
2. `legacyAuthStatus()` / `authStatus()`：授权兼容边界。
3. `RequestError` / `parseJsonResponse()` / `friendlyError()`：错误层。
4. `fetchEntity()` / `writeEntity()`：请求竞态和防重。
5. `openOverlay()` 系列：主弹层生命周期。
6. `startCurrentOperation()` 系列：当前任务轮询。
7. `workspacePrimaryAction()` / `workspaceRow()`：团队列表主要动作。
8. `entityActions.workspace/team/account`：稳定命令和 API 调用。
9. `teamMemberRows()` / `presentTeamMember()` / `teamMemberKind()`：成员状态。
10. `renderTeamDetails()` / `openWorkspaceDetails()` / `reloadTeamDetails()`：团队详情上下文。
11. `linkTeamMember()` / `showTeamAuthStep()`：接入后授权状态机。
12. `openReauth()` / `submitReauth()`：非团队上下文独立授权。
13. `bootOperations()`：完整任务诊断页。
14. 文件底部：close handler、Escape、focus trap、beforeunload 和 `window.Team48` 测试暴露。

### 12.2 `app/web/templates/console.html`

核对以下 DOM 必须唯一存在：

```text
#entity-sheet
#sheet-title
#sheet-subtitle
#sheet-body
#action-menu
#register-sheet
#reauth-sheet
#phone-import-sheet
#proxy-add-sheet
#proxy-edit-sheet
#toast-region
```

### 12.3 `app/web/static/css/components.css`

重点核对：

```text
.sheet / .sheet[hidden] / .sheet-panel
.menu / .menu[hidden]
.row-action-host / .row-actions-contextual
.team-mother-grid / .team-mother-aside
.team-member-list / .team-member-row
.member-action-menu / .member-action-options
.toast-region / .toast-*
@media (max-width: 720px)
```

### 12.4 后端文件

- `app/application/presenters.py`：只维护统一状态 presenter，不做数据库查询。
- `app/application/queries/identity.py`：只组装读模型，不执行业务写入。
- `app/application/queries/console.py`：附加 Console 专用快照和分页数据。
- `app/application/console_maintenance.py`：成员接入与本地关系维护。
- `app/application/reauth.py`：OAuth 会话和凭证更新。
- `app/application/console_actions.py`：API 动作门面和 Operation 结果。
- `app/web/routes/api.py`：HTTP 层，不复制 application 业务。
- `app/web/schemas/resources.py`、`app/web/schemas/workspaces.py`：请求类型和字段限制；授权 complete 使用后者的 `CompleteAccountOAuthRequest`。

## 13. 测试修改清单

### 13.1 `tests/test_ui_contract.py`

保留并扩充静态合同：

1. 不包含 `overlayReturn`、`activeModal`、`openModalOverlay`、`closeModalOverlay`。
2. 包含 `overlayState`、`openOverlay`、`replaceOverlay`、`closeOverlay`、`restoreOverlayContext`、`getActiveOverlay`。
3. `workspaceRow()` 使用 `owner_needs_auth`、`owner_auth_action`、`owner_account_id`。
4. 存在 `workspacePrimaryAction()`、`presentTeamMember()`、`teamMemberKind()`。
5. 存在 `startCurrentOperation()`、`inFlightWrites` 和 stable key。
6. `bootOperations()` 之前的普通页面代码不包含 operation list 请求。
7. 不出现旧子号管理 sheet、旧按钮文案或旧动作 ID。
8. 模板中的每个 sheet 有 `hidden` 和 `aria-modal=true`。

静态字符串测试只能防止结构回退，不能证明运行时只有一个 overlay。

### 13.2 `tests/test_management_context.py`

覆盖：

- remote-only 官方成员可以接入本地。
- 缺失本地账号时创建 child Account。
- 已存在账号时复用，且邮箱必须一致。
- 重复接入返回 `already_linked`，Membership 不重复。
- invited 成员不能提前接入。
- Workspace 母号不能作为普通成员接入。

### 13.3 `tests/test_official_member_logic.py`

覆盖：

- 账号和 Workspace 母号使用同一个 `build_auth_status()` 语义。
- 缺 token 返回 `needs_auth=true` 和 `missing_token`。
- 健康账号不显示授权动作。
- `owner_account_id=null` 返回显式 `owner_account_missing`。

### 13.4 `tests/test_reauth.py`

覆盖：

- start 不改变旧 `auth_state` 和 token。
- complete 失败不破坏旧授权。
- callback 过期和无效具有稳定错误码。
- complete 成功更新 token，并使 Workspace 母号健康状态恢复。

### 13.5 `tests/test_operation_api.py`

覆盖：

- `GET /api/operations/{id}` 返回步骤和能力。
- 可取消任务接受 cancel。
- 只有安全白名单中的任务允许 retry。
- rotate 等高风险任务不能通过通用 retry endpoint 重放。

### 13.6 浏览器交互测试

项目目前主要依赖静态合同和后端测试。继续强化时建议新建 `tests/test_ui_interactions.py`，使用 Playwright 和 mock API 覆盖真实 DOM 行为，不在生产数据上测试。

至少覆盖：

1. 打开 Team 详情后进入内联授权，始终只有 `entity-sheet` 可见。
2. 独立账号授权时只有 `reauth-sheet` 可见。
3. 任意时刻可见的主 `[aria-modal=true]` 数量不超过 1。
4. Escape 先关菜单，再关 active overlay。
5. 关闭 overlay 后焦点返回触发按钮。
6. link 每次用户动作只发送一次。
7. complete 按钮连点只发送一次。
8. 授权失败重试不再请求 link。
9. callback 普通失败保留输入；过期时显示重新生成。
10. 授权成功后回到相同 Workspace 并定位原成员。
11. 普通页面不请求 `/api/operations` 列表。
12. 当前 Operation 到终态后停止轮询。
13. `AbortError` 不产生错误 toast。
14. 桌面和 390px 移动端无横向溢出。

浏览器测试必须 mock 邀请、踢出、角色修改、轮转、授权换票、计费和 Sub2API 写入。

## 14. 验收命令

基础语法和完整测试：

```bash
node --check app/web/static/js/app.js
python -m unittest discover -s tests -v
```

静态差异检查：

```bash
git diff --check
git diff -- app/web/static/js/app.js app/web/templates/console.html app/web/static/css/components.css
```

浏览器验收至少覆盖桌面和 390px 移动宽度：

- Team 行母号异常时可直接授权。
- Team 详情母号异常时可直接授权。
- 母号档案缺失时只提供核对入口。
- remote-only 成员接入后需要授权，全程只有一个主弹层。
- 授权失败可以重试，且不会重复接入。
- 授权成功回到原 Team 和原成员。
- 取消授权回到原 Team，状态不显示为成功。
- 所有主弹层 Escape、焦点恢复、背景 inert 和 body scroll lock 正确。
- 普通页面只轮询当前 `operation_id`。
- 不执行真实邀请、踢出、角色修改、轮转、计费或 Sub2API 写入。

## 15. 完成标准

只有同时满足以下条件才算完成：

- 唯一 Overlay Manager 是所有主 sheet 的唯一生命周期入口。
- 不存在 `overlayReturn`、`activeModal` 或旧 modal helper 回归。
- 团队页母号授权入口完全基于后端显式字段。
- 母号档案缺失不会触发猜测、创建或错误授权。
- 成员流程围绕 Team 详情和统一 `presentTeamMember()`，不再重复产品概念。
- `members/link -> reauth` 在同一个 `entity-sheet` 内完成，失败重试不重复 link。
- 当前 Operation 轮询与完整任务历史解耦，但诊断能力保留。
- 写操作防重、读取 latest-wins、`AbortError` 语义正确。
- 用户默认只看到友好错误，技术详情按需打开。
- 静态合同、后端业务测试和浏览器交互验收通过。
- 未修改或重启 `/opt/sub2api` 及其任何依赖。
