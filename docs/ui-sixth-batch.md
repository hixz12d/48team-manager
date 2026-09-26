# UI 第六批实施记录

对应 `UI改动大纲.md` 第 9 章（一致性与文案）、第 10 章（无障碍与键盘）、第 11 章（清理）和第 13 章第 6 批。仅本地实现与隔离验证，未部署。

## 文案统一

- **团队**：界面里的"工作区 / 工作空间 / Team"统一为"团队"（如"补充团队""邀请加入团队""官方团队席位""团队资产""选择团队后启用"）。保留不改的有三类：技术字段"Workspace ID"、品牌名"48 Team Manager"，以及 Sub2API 交接里的"交回 Team"。最后这个指的是本系统，不是团队这个概念。
- **角色**：Owner / Member 改为"所有者 / 成员"。邀请和重新邀请的角色下拉写成"所有者（Owner）/ 成员（Member）"中英对照，其余地方只用中文。
- **英文残留**："Membership"改为"成员状态"（并且值也翻译了），"Binding"改为"Sub2API 绑定"，"Official user/account ID"改为"官方用户 / 账号 ID"，"dispatcher 会执行"改为"后台会处理已就绪的账号"。
- **关闭按钮**：3 处"关掉"统一为"关闭"。表单里的撤销保持"取消"。
- **总览副标题**：原来是"工作区资产在左，待处理在右……"，描述的是布局，改为"先处理异常，再看团队资产。"
- **侧栏**："任务记录"改为"任务"，与页面标题和命令面板一致。
- **em-dash**：检查后没有出现在句子里的。所有"—"都是空值占位，属于正确用法，没有改。
- **禁用原因可见**：批量删除按钮禁用时，选中计数旁边直接写原因（"其中 N 个是母号或仍属于团队，只有未分配的账号能删除"），不再无提示地变灰。

## 无障碍与键盘

- **"⋯"菜单**（全站共用一个 `#action-menu`）：
  - 支持 ↑ ↓ Home End；Esc 关闭并把焦点还给触发按钮；Tab 关闭菜单。
  - 用鼠标打开时焦点留在按钮上，按方向键才进入菜单，这样原有的鼠标操作习惯不变。
  - 在屏幕右边缘改为向左展开，并限制在视口内。
  - 页面滚动或窗口缩放时自动关闭，避免 fixed 定位的菜单和按钮错位。
  - 手机号页的"更多"改用统一的菜单按钮，补齐了 `aria-haspopup` 和 `aria-expanded`，也能被点击外部关闭的逻辑识别。
- **表单标签**：任务页的搜索框、4 个筛选下拉、每页条数，以及命令面板输入框，补上了 `aria-label`。
- **当前页**：侧栏当前页加 `aria-current="page"`。
- **提示播报**：去掉了 toast 容器上的 `aria-live` + `aria-atomic`。每条 toast 自己带 `role=status/alert`，原来的写法会让读屏器每来一条新提示就把整组提示重读一遍。
- **搜索防抖**：账号页搜索加了 200ms 防抖，几百行数据时不会每敲一个字就重新渲染一次。
- **命令面板**：
  - "去团队"原来指向已废弃的 `/workspaces`，改为 `/accounts?view=teams`。
  - "进行中的任务"原来用的是 `status=running` 参数，任务页根本不认，实际没有筛选效果。改为 `state=running,queued,waiting`。
- **分页**：账号页（原 10/20/50/100）和任务页（原 25/50/100）统一为 20/50/100。任务页默认 50，账号页默认 20。

## 清理

以下每一项删除前都由独立的只读核查确认没有调用方：

- `app.js` 删除约 540 行，4875 → 4338：
  - 旧 `/workspaces` 列表页用的 `bindSearch`、`renderPortfolio`、`portfolioAccountRow`、`paintList`、`bootWorkspaces`；
  - 只被这些函数使用的 `workspaceRow`、`accountRow`、`quotaCell`、`quotaMeter`、`usageWindowLine` 等 8 个渲染函数；
  - `meterTone`、`createAutoReauthButton`、`shortId`、`workspaceOfficialSummary`、`matchesQuery`、`filterItems`。
  - 这些功能在账号页都由 `accounts-view.js` 自己实现，删掉的是重复的旧代码。
- `console.html`：删除 `page == 'workspaces'` 的两个模板分支（这个页面早已只做跳转，从不渲染）。总览"团队"数字的链接改为直接指向账号页团队视图。
- `pages.py`：删除没用的 `NAV` 元组和 `PAGES["workspaces"]`。`/workspaces` 路由保留，只负责把旧书签跳转过去。
- CSS：删除 `skeleton-row`（含动画）、`row-actions-contextual`、`row-action-host`、`account-auto-reauth-button`，这几个 class 只被上面删掉的旧行使用。
- "控制台已连接"、`watchInviteProgress` 在前几批已删，这次复核确认没有残留。
- 这次没有删除的：核查列出的其他零散 CSS class（`badge-*`、`portfolio-*`、`quota-*` 等约 40 个）。它们引用关系分散，有些可能被动态拼接，逐个核实的成本高、收益低，留到以后需要时再处理。

## 修复前几批遗留的 3 个测试失败

- `browser_ui_polish`（账号表头吸顶）：原因是第 3 批把 1400px 以上的表格滚动容器从 `overflow: visible` 改成了 `auto`，表头只能在容器内吸顶，页面一滚动就跟着消失。已改回 `visible` 并注释原因。这是真实的界面缺陷，不只是测试问题。
- `browser_sub2api_defaults`（分页行数）：团队视图按整团队分页，测试却假设每页恰好 20 行。改为在"全部账号"视图测试跨页选择，并按新的 20/50/100 分页更新。
- `test_ui_contract` / `test_official_member_logic`："补充已结束，请查看任务结果"改为"补充已结束，结果见上方任务进度"。

## 测试契约变更

- 文案相关断言同步更新："补充团队""邀请加入团队""团队默认""改成所有者 / 改成成员""选择团队后启用""仅选中的 1 个团队""先处理异常"。
- `test_ui_contract` 删除了 7 个只断言旧代码里 class 名存在的检查（`quota-meter`、`is-collapsed`、`quota-billing-summary`、`meter-window`、`toggle-icon`、`account-auto-reauth-button`、`row-actions-contextual`），新增断言 `renderPortfolio` 已不存在。
- `management_ui.test.cjs` 的分页用例改为 20 条一页，并验证已废弃的 10 条会回退到默认值。
- `browser_management` 扩展了菜单键盘检查：方向键进入、End/Home、循环、Esc 回到触发按钮、视口内定位、滚动关闭。

## 验证

- Node：全部 `tests/*.test.cjs`，122 项全部通过（清理完成后重跑）。`node --check` 通过。
- Python：全量 `unittest discover` 共 612 项，11 项未通过。在干净的 HEAD 工作树（未含第 4–6 批任何改动）上重跑，结果完全一样，与本批无关：
  - 4 个模块无法导入（`test_identity`、`test_identity_queries`、`test_migration_skeleton`、`test_register_workspace`）：它们依赖工作区里被删除的 `legacy_import/`。这个删除不是本批做的，还没提交。
  - 7 项 Sub2API 同步 / 录制相关（`test_sub2api_sync_persistence` 3 项、`test_review_safety` 2 项、`test_round4_semantics.Sub2ApiSplitTests` 1 项、`test_hubstudio_record` 1 项）：HEAD 上原本就失败。
- 浏览器回归：先重新安装了本机被清空的 Playwright Chromium（`playwright install chromium`，装在用户目录，与项目无关）。首轮 20 个里有 5 个失败，逐一定位后处理如下：
  - **真实缺陷 1**：菜单"滚动即关闭"与"点击屏幕外按钮时浏览器先把按钮滚进视口"冲突，导致页面滚动后顶栏"登记账号 / 团队"菜单打不开。已修复：打开菜单后 150ms 内由打开动作引起的滚动不触发关闭。
  - **真实缺陷 2**：Sub2API 核对失败时，账号行只显示"待核对"，看起来和从没核对过一样，上次的已知状态也不见了。第 3 批精简行时引入。已修复：行内显示"核对失败"（警告色）加"上次：远端已删除"，完整原因保留在提示里。
  - **表头吸顶**：确认 1400px 以上 `.management-table-scroll` 必须是 `overflow: visible`，改回 `auto` 后测试会失败（表头滚出视口到 -80px）。另外测试用的预览数据只有 7 行，页面没有足够的滚动空间让表头到达顶部，测试里给页面临时加了底部留白。
  - **测试过期**（跟着第 3 批的界面调整更新）：行内健康状态改成了短徽章（完整文字在 `title`）；"推送到 Sub2API"不再是主按钮；远端筛选移进了"更多筛选"；Codex Proxy 设置卡片早在 5027a6f 就删了，`browser_codex_transfer` 还在找它（而且它默认连 8029 端口，改用 `TEAM48_PREVIEW_URL` 运行）。
  - **搜索防抖**：`browser_management`、`browser_account_deletion`、`browser_ui_polish` 输入搜索后立即断言，改为等渲染完成再断言。
- 最终一轮：Node 122 项全部通过；浏览器 20 个中 19 个通过。`browser_signup_extension` 报 `TargetClosedError`（浏览器被提前关闭），单独重跑时超过 280 秒被终止，放到干净的 HEAD 上重跑同样超时，属于插件测试本身的环境问题，与第 6 批无关。
- 未部署。
