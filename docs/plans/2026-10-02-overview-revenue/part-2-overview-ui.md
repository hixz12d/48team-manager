# Part 2：总览顶部收入格

**状态**：进行中（2026-10-02）
**依赖**：无（按 README"接口约定"开发；part-1 完成后用预览核对一次真实数据）
**所属计划**：[README](./README.md)

## 目标

总览顶部的运营摘要条依次显示四个收入格：**今日收入**、**近 7 天收入**、**本月入账**、**团队收入**（`已入账 $X ＋ 在跑 $Y`），悬停能看到覆盖率和说明。

## 可改文件

- `app/web/templates/console.html`（总览 `overview-summary` 区块，第 67–74 行）
- `app/web/static/js/app.js`（总览摘要渲染，第 1329–1342 行附近的 `set(...)`）
- `app/web/static/css/components.css`（`.summary-item` 一节，第 502–540 行及响应式部分第 1161–1164、1416–1423 行）

## 只读参考

- `docs/plans/2026-10-02-overview-revenue/README.md`"接口约定"：`summary.revenue_running` / `revenue_today` / `revenue_seven_day` 的字段
- `app/web/static/js/formatters.js`：`window.Team48Format.formatCost`（`null` / 非法值返回"—"）
- `app/web/static/js/accounts-view.js` 第 18–32 行 `revenueLine`：团队卡"已入账 ＋ 在跑"的文字和悬停提示写法，总览照这个口径
- `app/web/static/css/runtime.css` 第 20–21 行 `.board-revenue`

## 要点

- 摘要条最终顺序：团队、账号、待处理账号、身份冲突、今日收入、近 7 天收入、本月入账、团队收入。原"累计收入"格改成"团队收入"，`data-summary="revenue_total"` 的元素保留或改名都行，但 JS 要对应。
- 每格的值：
  - 今日收入：`formatCost(summary.revenue_today?.user_cost)`
  - 近 7 天收入：`formatCost(summary.revenue_seven_day?.user_cost)`
  - 本月入账：`formatCost(summary.revenue_month)`（不变）
  - 团队收入：`已入账 ${formatCost(summary.revenue_total)} ＋ 在跑 ${formatCost(summary.revenue_running?.user_cost)}`；两者都没数（`null` 或 0）时整格显示"—"，和团队卡 `revenueLine` 的规则一致。
- 悬停提示（`title`，挂在整个 `.summary-item` 上）：
  - 覆盖率：`total > 0 && synced < total` 时写"x/y 个号已同步，部分号未同步"；`stale` 为真时加"旧快照"；`note` 非空时原样附上。多条用"；"连接。
  - 今日收入固定说明："北京时间 0 点起，所有团队的用户扣费（U），含今天已换掉的号"。
  - 近 7 天收入固定说明："今天及前 6 天，含期间已换掉的号"。
  - 本月入账固定说明："本月离队号入账之和，不含在跑"。
  - 团队收入固定说明："已入账 = 所有离队号全程 U 之和（含已删团队）；在跑 = 当前在席成员（含母号）全程 U 之和"。
- `stale` 或覆盖不全时，数值旁加一个低调的提示标记（如数值后的小号 `·部分` 或改用次要文字色），不要用红色警告样式（`is-alert` 只给待处理 / 冲突用）。具体样式跟随现有 `.summary-item small`（待处理格已经用 `<small id="overview-attention-breakdown">`）。
- 团队收入格文字比其他格长：允许这一格更宽或数值字号略小，保证桌面端一行不换行、窄屏可换行；不要把其他格挤成两行。响应式规则里的 `nth-child` 是按格数写的，格数从 6 变 8 后要一起调整，确认手机宽度下边框不错乱。
- 后端字段还没有时（part-1 未完成），各格显示"—"，不报错。
- 只改总览，不动团队卡、抽屉和账号页。

## 步骤

1. 改 `console.html`：新增"今日收入""近 7 天收入"两格，"累计收入"改为"团队收入"，放到约定顺序。
2. 改 `app.js`：渲染四格的值和 `title`；`set()` 现有写法会把 `null` 写成 0，收入格要单独处理，保证没数时显示"—"。
3. 改 `components.css`：团队收入格宽度、提示标记样式、8 格时的响应式边框。
4. 用预览 `python -m uvicorn tests.preview_app:app --port 8019` 打开总览，看桌面和窄屏（浏览器缩到 400px 左右）两种宽度。part-1 未完成时确认四格显示"—"不报错；完成后确认有数。
5. 跑基本检查。

## 完成标准

- [ ] 总览顶部按约定顺序显示四个收入格，团队收入显示为"已入账 $X ＋ 在跑 $Y"
- [ ] 后端缺字段或值为 `null` 时显示"—"，不显示 $0.00
- [ ] 悬停能看到口径说明；覆盖不全 / 旧快照 / `note` 会出现在提示里
- [ ] 桌面一行排开不换行，窄屏下边框不错乱
- [ ] 基本检查通过：
  ```bash
  .venv/Scripts/python.exe -m compileall -q app scripts
  .venv/Scripts/python.exe -c "import app.main"
  for f in app/web/static/js/*.js; do node --check "$f"; done
  ```

## 完成记录

<!-- 执行者填写：改了什么、偏离计划的地方、需要写进 docs 的内容（PRODUCT 功能清单"团队收入"一行）。 -->
