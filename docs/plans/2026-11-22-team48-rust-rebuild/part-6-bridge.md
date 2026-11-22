# Part 6：授权桥服务

**状态**：已完成
**依赖**：part-1、part-2 已完成
**所属计划**：[README](./README.md)

## 目标

实现 `apps/bridge`：一个几乎无状态的小服务，兼容现有插件的 `/api/ext/ping`、`/api/ext/rs/start`、`/api/ext/rs/complete`。插件注册完新号后，授权桥生成桌面版授权链接，拿到回调后经 rs 的代理换令牌、核对身份和 Team 套餐，再写进 rs。插件不用改就能从老项目切到这里。

## 可改文件

- `team48/apps/bridge/**`（`src/main.rs` 不改；`server.rs` 及新建模块、`Cargo.toml` 可改）
- `team48/apps/bridge/README.md`（新建：做什么、不做什么、配置、接口链接到合约）

## 只读参考

- 新仓库 `docs/contracts/bridge-api.md`（接口唯一依据，字段不得改名）
- 新仓库 `crates/openai`、`crates/rs-client` 的 README 和公开接口
- 老仓库 `C:\Projects\VPS\天理云-美西国际大宽带-8H16G\48team-manager\`：
  - `app/application/extension_rs.py`（完整流程：start 挑代理 / 沿用已有号代理、建会话；complete 换票、核对、导入、恢复设置）
  - `app/web/routes/extension.py`（鉴权）
  - `app/application/codex_reauth.py` 的 `FrozenProxyClient`（换票必须走会话里冻结的同一个代理）
  - `app/application/oauth_sessions.py`（会话有效期、一次性使用）

## 要点

- **鉴权**：`EXTENSION_API_TOKEN` 少于 24 位时 `/api/ext/*` 全部 404；Bearer 不匹配 401；常量时间比较。不接受任何管理员会话。
- **start**：小写邮箱；rs 已有唯一同邮箱号就沿用它的代理，否则 `pick_proxy`；生成 PKCE + state + 桌面版链接（版本号从 rs 读，缓存 30 分钟，读不到用 `CODEX_DESKTOP_APP_VERSION`）；会话（ticket → email、verifier、state、代理快照、创建时间）**只存内存**，有效期与老代码一致，用完即删；服务重启后旧 ticket 失效，插件按老行为提示重试。
- **complete**：校验 ticket 和回调（state、回环主机）；用会话里冻结的代理换票；核对：令牌邮箱与会话邮箱一致、有刷新令牌、`is_team_plan(plan_type)`；被拒时日志记 `plan_type`。
- **写入 rs**：同邮箱已有号 → 按身份覆盖凭据，再 `restore_settings` 恢复原分组 / 代理 / 并发 / 权重；新号 → 带 `RS_IMPORT_DEFAULTS` 和所选 `proxy_id` 导入；导入后读回核对邮箱和工作区一致。错误码、响应字段与合约一致。
- 不建任何本地数据库；不记团队、不记切换次数；不接码。
- 日志只写邮箱前 3 位、阶段、错误码；不打印令牌、回调地址、代理密码。
- 每个请求有总超时（参考老代码），并发两个 complete 用同一 ticket 时只有一个成功。
- README 写明：这两步接口就是将来浏览器自动化的接入约定（自动化当作"大号插件"调用）。

## 步骤

1. 对照老 `extension_rs.py` 列出 start / complete 每一步和每个错误码。
2. 实现配置读取、鉴权中间件、内存会话、两个接口和 ping。
3. 写 README；跑检查。

## 完成标准

- [x] 三个接口的请求 / 响应与 `docs/contracts/bridge-api.md` 逐字段一致
- [x] 没有任何数据库依赖；重启后旧 ticket 返回合约规定的错误
- [x] 基本检查通过：`bash scripts/check.sh`

## 完成记录

- 由 Gemini 施工（第一次调用遇 Gemini 服务端 500，重试成功）、Claude 验收。改动：`apps/bridge/src/server.rs`（配置、鉴权中间件、90 秒总超时、`Cache-Control: no-store`、422 参数校验、三个接口）、新建 `src/server/session.rs`（内存会话，20 分钟、一次性原子占用，任何失败即作废）、`src/server/version.rs`（桌面版版本号 30 分钟缓存 + 兜底）、`README.md`；`Cargo.toml` 加了 `base64`（生成 ticket）。没有数据库依赖，无新增环境变量。
- Claude 验收时补了 `GET /health`（返回 `ok`，供 Compose 健康检查；合约之外的接口，不经鉴权）。
- Claude 核对：鉴权在请求体校验前；少于 24 位 404 / 错令牌 401 的响应体与合约一致；complete 依次判 `callback_expired` / `callback_invalid` / `callback_consumed` / `oauth_proxy_missing` / 换票失败 / `token_identity_mismatch` / `not_team_workspace` / `multiple_rs_accounts`，写 rs 三种情况（同身份覆盖 + `restore_settings`、换团队带原设置覆盖、新号带默认参数）与合约一致，最后回读核对 `remote_identity_mismatch`。
- 与合约的小差异：换票失败时 `error_code` 直接用官方零件错误码（如 `network`、`token_rejected`、`rate_limited`），只有拿不到码时才用 `oauth_exchange_failed`；插件只显示 `message`，不受影响。合约表里已写"官方零件错误码，默认 oauth_exchange_failed"，一致。
- 检查：`bash scripts/check.sh rust bridge` 通过（Claude 复跑）；全量检查在批后统一跑。

