# Part 1：每日收入记录与总览数据

**状态**：进行中（2026-10-02）
**依赖**：无
**所属计划**：[README](./README.md)

## 目标

后端能算出"今日收入""近 7 天收入""在跑合计"，按 README 的接口约定放进 `GET /api/overview` 的 `summary`；被换掉的号当天和近 7 天赚的钱不会丢。

## 可改文件

- `app/persistence/models/revenue.py`（新增 `Sub2ApiRevenueDaily` 模型）
- `app/persistence/models/__init__.py`（导出新模型）
- `app/persistence/migrations/bootstrap.py`（导入新模型，让 `init_db` 建表）
- `app/application/revenue_daily.py`（新建：每日记录写入、补录、今日 / 近 7 天汇总）
- `app/application/sub2api_usage.py`（同步成功后写每日记录）
- `app/application/revenue_ledger.py`（`settle_binding` 里顺带写当天记录）
- `app/application/queries/portfolio.py`（返回在跑合计、今日、近 7 天）
- `app/application/queries/console.py`（把新字段放进 `summary`）
- `app/main.py`（启动后台补录，挂在现有 `backfill_revenue` 里）
- `tests/preview_app.py`（预览示例数据：几条每日记录，含一个已离队号）

## 只读参考

- `app/integrations/sub2api/client.py`：`fetch_billing_windows`（今日走 `POST /api/v1/admin/accounts/today-stats/batch`，7 天和全程走 `GET /api/v1/admin/accounts/{id}/stats?days=N`）、`fetch_account_stats_summary`
- `app/application/sub2api_usage.py`：`WINDOW_KINDS`、`_metrics`、`_window_bounds`、`aggregate`、`USAGE_STALE_AFTER`（30 分钟）
- `app/persistence/models/sub2api.py`：`Sub2ApiUsageSnapshot`（绑定删除时级联删除，所以离队号只能靠每日记录）
- `app/web/static/js/accounts-view.js` 第 18–32 行 `revenueLine`：团队卡"在跑"的口径和提示文字
- `docs/PRODUCT.md`"团队收入账本"一节

## 要点

**数据表 `sub2api_revenue_daily`**（模型 `Sub2ApiRevenueDaily`，写在 `revenue.py`，风格照抄 `Sub2ApiRevenueEntry`：不加外键）
- 字段：`id`、`remote_account_id`（String 100，非空）、`day`（Date，按 `TIMEZONE` 的本地日期，非空）、`workspace_id`（Integer，可空，记录写入时的团队）、`account_id`（可空）、`email`（可空，快照）、`user_cost`（Numeric(20,10)，非空）、`source`（String 30：`sync` / `settle` / `backfill`）、`updated_at`。
- 唯一键（`remote_account_id`, `day`）；索引 `day`。
- 一个远端账号一天只有一行。写入时**取新旧较大值**（同一天的扣费只增不减），防止某次读到 0 或部分数据把已有的数冲掉。

**写入时机**
1. `Sub2ApiUsageService.sync` 里，每个绑定的 `today` 窗口 `_record_success` 之后，用同一个 `now` 和同一份 `user_cost` 写当天这一行（`source="sync"`），`day` 取快照的 `window_start_at` 换算成的本地日期（不要重新取当前时间，避免卡在 0 点前后被记到第二天）。写失败只记 warning，不影响用量同步结果。
2. `RevenueLedger.settle_binding` 里，`allow_remote=True` 时额外读一次这个号的"今日"：用 `sub2api_client.fetch_billing_windows(db, [remote_id])` 的 `today` 结果（或者在 client 外自行调用相同接口，但 `client.py` 不在可改范围，只能复用现有方法）。读到就写当天这一行（`source="settle"`）；读不到或 `allow_remote=False`，就用本地 `today` 快照兜底（同样只在快照的日期是今天时写）。整个过程包在 try 里，失败不抛异常、不影响原记账结果。
   - 注意：`settle_binding` 在两个时间点被调用（踢人后、删旧远端前），两次都写，靠"取较大值"自动合并。
3. 启动补录（`main.py` 现有的 `backfill_revenue` 里，在 `backfill_departures` 之后调用）：
   - 先确认 Sub2API 能否按天查历史：查看 `GET /api/v1/admin/accounts/{id}/stats?days=7` 的完整返回（现在只用了 `summary`），看有没有按天的数组（如 `history`、`daily`、`trend` 之类）。**只读**，用生产配置时只发 GET；本地没有 Sub2API 时，可以在 Sub2API 源码或现有合约里查，查不到就当作不支持。
   - 支持按天：对"账本里 `settled_at` 在最近 7 天内、且每日记录缺天"的离队号补最近 7 天（`source="backfill"`）。远端已删的号查不到就跳过。
   - 不支持：不补。把"每日记录最早的一天"作为离队号的统计起点，汇总时写进 `note`。
   - 在绑定中的号不用补，它们的今日 / 近 7 天直接用快照。
   - 补录重复跑要安全（唯一键 + 取较大值）。

**汇总（`revenue_daily.py` 提供一个函数，如 `overview_totals(db, usage_by_context, bindings)`，供 `portfolio_query` 调用）**
- 本地"今天" = `TIMEZONE`（`load_settings().timezone`，默认 `Asia/Shanghai`）的当前日期；近 7 天 = 今天及前 6 天。
- 在绑定中的号（已验证的 Sub2API 绑定，含母号、未分配的号）：
  - 今日：取该绑定的 `today` 快照；只有 `last_success_at` 非空**且**快照的 `window_start_at` 本地日期等于今天才算"已同步"，否则计入未同步（不算 0）。
  - 近 7 天：取 `seven_day` 快照，同样要求快照窗口的起始日 = 今天往前 6 天。
  - 快照超过 30 分钟（`USAGE_STALE_AFTER`）→ `stale=true`。
- 不在绑定中的号：每日记录里 `remote_account_id` 不属于任何当前已验证绑定的行，今日 = 今天那行，近 7 天 = 7 天内各行之和。
- 同一个号不能重复算：按 `remote_account_id` 去重，在绑定中的一律用快照，不再叠加每日记录。
- 在跑合计：把各团队在席成员（含母号）的 `lifetime` 窗口加起来，口径和 `portfolio_query` 里每组的 `usage.windows.lifetime` 完全一致（可直接对 `groups` 的 `usage.windows.lifetime` 求和，覆盖率用各组 `coverage` 相加；`stale` 任一为真即真）。
- 输出三个对象 `revenue_running` / `revenue_today` / `revenue_seven_day`，形状见 README"接口约定"；`user_cost` 用 `_money_text` 输出字符串；在绑定中的号一个有效快照都没有、每日记录也没有时为 `null`。
- `note`：近 7 天有离队号统计起点时写"离队号从 MM-DD 起统计"（起点在 7 天窗口内才写）；核对后发现 Sub2API "今日"的日边界不是 `TIMEZONE` 0 点时，在今日和近 7 天的 `note` 里写清实际边界。
- `portfolio_query` 返回值里加 `revenue_overview: {running, today, seven_day}`；`console.py` 第 182–183 行附近写入 `summary["revenue_running"]`、`summary["revenue_today"]`、`summary["revenue_seven_day"]`。现有 `revenue_total`、`revenue_month` 不动。

**预览数据**（`tests/preview_app.py`）
- 给现有在绑定的示例号补 `today` 快照（如果预览已有就不重复），再加 2～3 条每日记录：其中一条属于已离队的示例号（`remote_account_id` 用 `900`，对应现有示例账本），日期分别为今天和 3 天前，让总览四格都有数。
- 不放开任何新的 API 写接口；`/api/overview` 已在 `local_reads` 里。

## 步骤

1. 先核对 Sub2API 两件事并记到完成记录里：`today-stats` 的日边界（看 Sub2API 源码或接口说明，确认是不是按 Sub2API 服务器时区或请求参数的 0 点）；`/accounts/{id}/stats` 有没有按天数据。对不上 `TIMEZONE` 时按"要点"写进 `note`，不改日边界算法。
2. 写模型、导出、bootstrap 导入。
3. 写 `revenue_daily.py`：`record_day(...)`（取较大值的 upsert，带 `begin_nested` + `IntegrityError` 重试，照抄 `revenue_ledger._upsert` 的做法）、`backfill_recent(...)`、`overview_totals(...)`。
4. 接入 `sub2api_usage.sync` 和 `revenue_ledger.settle_binding`。
5. 接入 `portfolio.py`、`console.py`、`main.py`。
6. 改预览数据，用预览确认 `/api/overview` 返回三个新对象且数值合理。
7. 跑基本检查。

## 完成标准

- [ ] 启动后 `sub2api_revenue_daily` 表自动建好，重复启动不报错
- [ ] 5 分钟用量同步后，每个已验证绑定当天都有一行；同一天重复同步不会让金额变小
- [ ] 离队记账后，即使绑定被删，这个号当天的金额仍算在今日收入里
- [ ] `GET /api/overview` 的 `summary` 有 `revenue_running`、`revenue_today`、`revenue_seven_day`，形状与 README 一致；拿不到数时 `user_cost` 为 `null`，不是 `"0"`
- [ ] 预览 `python -m uvicorn tests.preview_app:app --port 8019` 下 `/api/overview` 三个对象都有数
- [ ] 基本检查通过：
  ```bash
  .venv/Scripts/python.exe -m compileall -q app scripts
  .venv/Scripts/python.exe -c "import app.main"
  ```

## 完成记录

<!-- 执行者填写：改了什么、Sub2API 日边界和按天历史的核对结论、偏离计划的地方、上线注意、需要写进 docs 的内容（ARCHITECTURE 数据表 / 收入账本一节、PRODUCT 团队收入一行和规则）。 -->
