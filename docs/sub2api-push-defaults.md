# Sub2API 推送默认值与账号分页

设置页的 Sub2API 卡片提供“新建账号默认值”：

- 默认并发为 **5**，可设置 1–1000。
- 默认账号分组支持多选，只列出启用的 OpenAI 普通分组；留空时沿用 Sub2API 自己的默认账号分组。
- 默认代理可选“不指定代理”“使用固定代理”“按代理分组自动分配”。
- 代理分组选项展示名称、可分配代理数及每个代理的账号上限。选择 `IPV6出口` 后，创建账号时发送 `proxy_group_id`，由 Sub2API 在账号创建事务中分配代理。Team48 不自行挑 IP，也不创建或修改代理。

默认值仅用于新建远端账号，包括已确认删除后的重建。更新已有绑定或匹配到原远端账号时，继续保留原并发、代理和账号分组；调用方显式提供的账号分组优先于保存的默认值。后台凭据刷新不应用这些默认值。

保存和新建前会核对所选目录。代理分组不存在或接口不支持时停止创建。容量不足由 Sub2API 返回错误，不回退为直连。创建后会复读代理及账号分组；分配未确认时显示部分完成，并保留已创建账号的绑定，避免把未知状态显示为成功。

目录来自已保存的 Sub2API 连接。修改服务地址或认证后请保存，再点“刷新分组和代理”。读取失败保留当前选择，不清空默认值。目录接口需要后台登录，响应不包含代理密码或 API Key。

## Sub2API 接口

对接本地 Sub2API 源码中的原生接口：

- `GET /api/v1/admin/groups`：账号分组目录。
- `GET /api/v1/admin/proxy-groups`：返回完整数组，含 `proxy_ids`、`available_proxy_ids`、`max_accounts_per_proxy`。
- `GET /api/v1/admin/proxies`：固定代理目录。
- `POST /api/v1/admin/accounts`：创建时发送 `concurrency`、可选 `group_ids`、`proxy_id` 或 `proxy_group_id`。

Sub2API 的 `resolveAccountProxyGroup` 使用事务锁，在活跃、未过期且未超过账号上限的代理中分配。Team48 的目录快照仅用于选择和核对，容量判断以创建时的 Sub2API 事务为准。

## 页面操作

- 移除设置页和账号详情中的 Codex Proxy 入口，保留 Codex JSON 文件导出。
- 账号列表默认每页 20 条，可选 10、20、50、100 条。按团队展示时，大团队可跨页，每页保留团队上下文。
- 页码保存在 URL，每页条数在浏览器中记忆；筛选变化回到第一页，删除末页数据后回退到有效页。
- 跨页保留勾选，筛选或视图切换时清空勾选。删除确认框列出全部选中邮箱。
- 批量操作栏移到列表下方，勾选后吸附在视口底部，方便直接删除。单次删除和导出仍最多 50 个。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_sub2api_defaults tests.test_sub2api_management tests.test_sub2api_republish tests.test_connection_probe tests.test_account_deletion -q
node --test tests/management_ui.test.cjs tests/account_feedback_ui.test.cjs
.\.venv\Scripts\python.exe -m tests.browser_sub2api_defaults
.\.venv\Scripts\python.exe -m tests.browser_account_feedback
```

浏览器检查使用隔离数据库与模拟外部接口，覆盖设置保存及重载、两种代理模式、目录失败保留选择、跨页选择和批量删除、页码回退、搜索重置，以及 1440/390 宽度布局。线上只读核对确认代理分组接口返回 `IPV6出口`；没有创建真实远端账号或调整其配置。
