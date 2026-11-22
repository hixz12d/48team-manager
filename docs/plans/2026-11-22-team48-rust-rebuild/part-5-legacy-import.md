# Part 5：老库导入命令

**状态**：已完成
**依赖**：part-0 已完成
**所属计划**：[README](./README.md)

## 目标

实现看板服务的 `import-legacy` 子命令：只读打开老项目的 SQLite（`team48.db`），用老密钥解开母号凭据，再用新密钥加密，把团队、母号、到期日写进新库。默认干跑只打印将导入什么，加 `--commit` 才写入。

## 可改文件

- `team48/apps/dashboard/src/legacy_import.rs`（part-0 建的占位，本 Part 实现；需要拆子模块时放 `team48/apps/dashboard/src/legacy_import/` 目录下）

## 只读参考

- 老仓库 `C:\Projects\VPS\天理云-美西国际大宽带-8H16G\48team-manager\`：
  - `app/persistence/models/identity.py`（`accounts`、`workspaces`、`workspace_memberships`）
  - `app/persistence/models/resources.py`（`proxy_profiles`，母号代理可能存在这里）
  - `app/application/tokens.py`（`decrypt_secret`）、`app/core/crypto.py`
  - `app/application/workspaces.py`（团队名取值：`custom_name` / `official_name` / `name` 的优先级）
- 新仓库 `apps/dashboard/migrations/0001_init.sql`、`crates/kit/src/crypto.rs`、`apps/dashboard/src/main.rs`（子命令参数定义）

## 要点

- 参数：`--legacy-db <路径>`（只读打开，URI `mode=ro`）、`--commit`；老密钥从环境变量 `LEGACY_ENCRYPTION_KEY` 读，新密钥 `ENCRYPTION_KEY`，新库 `DATABASE_PATH`。
- **导入范围**：
  - 团队：`workspaces` 中 `status` 为活跃、有 `official_workspace_id` 的行 → `teams`（名称按老优先级、`seat_limit`、`manual_expires_on` → `expires_on`）。归档 / 已删除的不导。
  - 母号：每个团队的 `owner_account_id` 对应账号 → `mothers`（email、official_user_id、代理地址、access / refresh / id token；`token_expires_at` 从 access token 解析或沿用老字段）；同一母号管多个团队只建一条。
  - 成员不导：上线后点"同步"从官方重建。子号、号池、Sub2API、收入、购买信息、切换次数一律不导。
- 母号授权状态：有刷新令牌记 `unknown`（等新服务第一次续期确认），没有记 `needs_reauth`。
- 幂等：按 `official_workspace_id` / 母号 email 覆盖更新，重复运行不产生重复行。
- 干跑输出：每个团队一行（团队名、官方 ID 尾 6 位、母号邮箱前 3 位、凭据有无、到期日），最后汇总数量和跳过原因；不打印任何令牌和代理密码。
- 解密失败的账号单独列出并跳过，不中断其他行；`--commit` 用一个事务，任何写入失败整体回滚。
- **重要提醒写进命令帮助**：正式导入前必须先停掉老项目（或确认它不会再刷新母号令牌），否则两边会互相作废刷新令牌。

## 步骤

1. 读老表结构，确定团队名、母号代理、凭据字段来源。
2. 实现只读读取、解密 / 再加密、干跑输出。
3. 实现 `--commit` 事务写入。
4. 用老库的一份**本机副本**干跑验证（副本放系统临时目录，用完删除；没有本机副本就在完成记录说明待上线时在服务器干跑）；跑检查。

## 完成标准

- [x] 干跑能列出全部团队和母号，不泄露密钥
- [x] `--commit` 重复运行结果一致
- [x] 基本检查通过：`bash scripts/check.sh`（见完成记录：dashboard 包 check + clippy 通过；全量 clippy 当时卡在并行中的 rs-client 零件）

## 完成记录

- 实现文件：`team48/apps/dashboard/src/legacy_import.rs`（参数、导入计划、团队名、JWT 解析、干跑输出）、`legacy_import/legacy.rs`（只读读老库，`SELECT *` + 按列名取值，缺列当空）、`legacy_import/write.rs`（迁移 + 一个事务 upsert）。`Args` 未增删字段，只补了 `--commit` 帮助里的停老项目提醒和幂等说明。
- 字段来源：团队名 = `custom_name` → 非占位/非邮箱的 `official_name` → `name` → `未命名工作区 · <官方 ID 前 8 位>`（与老 `resolve_display_name` 一致）；只导 `status = 'active'` 且有 `official_workspace_id` 的行，`occupied_seats` 顺带写进 `seats_used`；母号代理取 `accounts.proxy`（老库明文完整地址），为空且有 `proxy_profile_id` 时按老 `compose` 规则从 `proxy_profiles` 拼（用户名/密码用老密钥解开）；`official_user_id` 老库为空时取 access token 的 `https://api.openai.com/auth.user_id`；`token_expires_at` 取 access token 的 `exp`（老库无此字段）；老库没有 device_id，新建母号生成 UUID v4，重复导入保留原值。
- 小偏差 1：sqlx 未开 `macros` 特性，`sqlx::migrate!` 编译不过（不得改 Cargo.toml）。改为手工构造等价迁移器：`Migration::new(1, "init", Simple, include_str!(0001_init.sql))`，版本、描述、SQL 原文与宏读目录结果一致，校验和相同；并设 `ignore_missing`，新库已跑过更新版本的迁移也不报错。part-7 若在 Cargo.toml 加了 `macros`，可把 `write.rs` 的 `migrator()` 换成 `sqlx::migrate!("./migrations")`；以后新增迁移文件时这里不会自动带上（服务启动会跑），导入前先启动过一次服务即可。
- 小偏差 2：母号凭据解密失败时，该母号单独列出，其名下团队也一起跳过（原因"母号凭据解密失败"），避免把团队写成无母号覆盖掉已有关联；没有关联母号或母号账号不存在的团队照常导入（`mother_id` 为空，干跑里标明）。同一官方 ID 重复的活跃行只取 id 最小的一条。
- 母号 upsert 时 `official_user_id` 用 COALESCE（新值为空不覆盖），令牌、代理、`token_expires_at`、`auth_state` 一律以老库为准覆盖，`last_refresh_error` 清空。
- 验证：本机没有老库 `team48.db` 副本（`data/team_manage.db` 是更老的库，未用），**待上线时在服务器对停服后的备份副本干跑**。用老代码 SQLAlchemy 模型在系统临时目录造了假库（WAL 模式，覆盖自定义名/邮箱名/占位名、过期团队、无官方 ID、重复官方 ID、解密失败、无母号、母号账号不存在、代理档案拼地址），在 Docker 里干跑输出正确且不含令牌和代理；`--commit` 连跑两次：第一次母号新增 2、团队新增 5，第二次全部为更新、行数和 id 不变；用新密钥解开新库凭据与原文一致。临时文件已删除。
- 注意（写进 RUNBOOK / cutover）：只读打开 WAL 模式的老库时 SQLite 会在旁边生成 `-shm` / `-wal` 小文件（不改主库内容），所以请对备份副本运行，不要直接指向老服务正在用的库；命令：`LEGACY_ENCRYPTION_KEY=… ENCRYPTION_KEY=… DATABASE_PATH=… dashboard import-legacy --legacy-db <副本>`，确认干跑后再加 `--commit`。
- 检查：`bash scripts/check.sh rust dashboard` 通过；`cargo clippy -p dashboard --all-targets --no-deps -- -D warnings` 通过；`bash scripts/check.sh rust`（全量 clippy）当时失败在 rs-client 零件（part-2 并行施工中的未使用代码警告），与本 Part 无关。
