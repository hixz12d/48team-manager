# UI 第五批实施记录

对应 `UI改动大纲.md` 第 8 章（字号与排版）和第 13 章第 5 批。保留现有配色、布局、选择器和 aria 名称。仅本地实现与隔离验证，未部署。

## 字号体系

`tokens.css` 新增 5 个字号角色，全站 CSS 中的文字字号都改用这些变量：

| 变量 | 值 | 用途 |
|---|---|---|
| `--text-meta` | 12px | 次要信息、表头、徽章、提示（全站下限） |
| `--text-body` | 14px | 正文、表格、按钮、输入框（原 13px） |
| `--text-heading` | 16px | 面板、抽屉标题（h2） |
| `--text-title` | 20px | 页面标题 h1（原账号页 24px、其他页 18px，统一） |
| `--text-figure` | 24px | 汇总数字（原 22px / 28px） |

- 原来 10px 及以下的 7 处、11px 的 18 处全部提到 12px。渲染后逐页检查，4 个页面、3 档宽度、深浅两套主题，小于 12px 的可见文字为 0。
- 标题层级：h1 20px > h2 16px（半粗）> h3 14px（半粗），解决 `flat-type-hierarchy`。
- 抽屉分组标题 `.sheet-section h3` 去掉大写转换，只保留 12px 灰色小标题加 0.02em 字距（中文没有大小写，原来的转换对中文没效果，只会把 Workspace 之类的英文强行变大写）。
- 表格、时间、计时器、额度条、汇总数字统一用等宽数字（`tabular-nums`），任务计时刷新时宽度不跳。
- 剩下 5 处写死的字号（18–22px）都是"⋯"、展开箭头、刷新这类图标字符，不是文字，保留原值。

## 字体

- 拉丁字母、数字、常用符号用本机已安装的 Anthropic Sans（正文）和 Anthropic Mono（代码 / ID）。
- 引用方式是 `@font-face { src: local(...) }`，字体文件不进仓库、不随站点发布，没有授权风险。`unicode-range` 限定只接管拉丁字符，中文和全角标点照常用系统中文字体（微软雅黑等）。
- 没装这套字体的机器（包括 VPS 上的浏览器）自动回退到原来的系统字体栈，界面不受影响。
- 实测（Chromium 平台字体检查）：侧栏品牌名、邮箱由 Anthropic Sans Variable Text 渲染，中文标题由 Microsoft YaHei 渲染，符合预期。
- 限制：Anthropic Sans 这个文件只有常规字重（400），界面里的半粗 / 粗体是浏览器模拟加粗。Mono 支持 300–800 可变字重。

## 断点

从约 20 种写法收敛到 3 档：1280 / 1024 / 720（另保留 `pointer:coarse`、`prefers-reduced-motion`、`forced-colors` 这类非宽度查询，以及账号表格吸顶用的 `min-width:1400px`）。

- 1279 → 1280，1039 / 960 → 1024，1200 → 1280，768 / 719 / 640 / 540 → 720。
- `components.css` 表格吸顶的 `min-width:1120px` → `1280px`。

## 其他

- 焦点环只剩一套：`app.css` 中的 `outline: 2px solid var(--accent); outline-offset: 2px`，覆盖 summary。删除了 `management.css` 里 3px 的第二套，以及原来 box-shadow 的写法。
- `management.css` 从每条规则一行的压缩写法格式化为标准多行写法（291 → 约 1340 行）。格式化前后逐条比对 766 条声明完全一致，只改格式、不改规则。
- `::selection` 选中文字改用主题色背景。
- `cramped-padding`：账号页工具栏底部补内边距，账号表格滚动容器补底部内边距。
- `PRODUCT.md` 更新了字体决定（原为"未决"）。

## 检测器结果（渲染后的 4 个页面）

共 17 条（原 20 条）：

- `undersized-ui-text` 从 4 页都有降为 0。
- `gpt-thin-border-wide-shadow` 12 条、`repeating-stripes-gradient` 4 条：分别属于第 7 章的卡片阴影和第 2 批的任务进度斜纹，不属于本批范围。
- `cramped-padding` 3 条：
  - 设置页 `sub2api-default-groups` 是误报：它的计算样式是无边框、无内边距（被 `settings.css` 的 fieldset 重置覆盖），检测器读到的是被覆盖前的规则。
  - 总览"最近记录"区块和账号表格顶部是有意贴边的分隔线布局（上边框分隔、内容左对齐），不是卡片。
- `clipped-overflow-container` 2 条：`html` / `body` 的 `overflow-x: clip` 是为了保证 sticky 表头生效而有意设置的。
- 截图：`dist/ui-batch5/{overview,accounts,settings,operations}-{dark,light}-{1440,1024,390}.png`。

## 验证

- 渲染检查（4 个页面 × 1440 / 1024 / 390 × 深浅两套主题，共 24 张截图）：正文 14px、h1 20px（390 宽下 16px）、h2 16px；没有横向溢出；小于 12px 的可见文字为 0；账号表格最大行高 60px，没超过 64px 的限制。
- 浏览器回归通过：`browser_theme`（对比度全部 ≥ 6.7:1）、`browser_accounts_layout`（240 行，三档宽度）、`browser_management`（6 档宽度）、`browser_task_progress`、`browser_zoom_check`（200% 缩放）、`browser_workspace_expiry`、`browser_runtime_lifecycle`。均无页面报错。
- 3 项失败，已确认和本批无关：
  - `browser_ui_polish`：账号表头吸顶的位置断言。换回本批之前的 CSS 重跑，同样失败（数值 128 vs 本批 119）。
  - `browser_sub2api_defaults`：分页行数期望 20，实际 23。换回旧 CSS 同样失败，原因是第 3 批改成"整团队分页"后，这个测试的期望值没有更新。
  - `test_ui_contract.test_console_has_one_product_and_no_legacy_assets`：断言 `app.js` 里不出现"查看任务"，是 JS 文案问题，不涉及 CSS。和第 4 批记录里提到的那一项同源。
- 未跑全量测试，未部署。
