# Part 2：rs 接口零件

**状态**：已完成
**依赖**：part-0 已完成
**所属计划**：[README](./README.md)

## 目标

实现 `crates/rs-client`：调用 codex-proxy-rs（rs）管理接口的客户端。看板用它读账号列表和额度，授权桥用它挑代理、导入账号、恢复原设置、读桌面版版本号。

## 可改文件

- `team48/crates/rs-client/**`（含 part-0 建的 lib.rs；公开签名保持 part-0 定义的形状）
- `team48/crates/rs-client/README.md`（新建）

## 只读参考

老仓库 `C:\Projects\VPS\天理云-美西国际大宽带-8H16G\48team-manager\`：

- `app/integrations/codex/client.py`：所有 rs 接口的 URL、请求头（`x-api-key`）、分页、字段映射、`export_proxy_url`、`list_proxy_accounts`、`import_accounts`、`update_account`、`detail`
- `app/application/codex_rs_onboard.py`：`_pick_proxy` 挑代理规则（启用、最近测试通过、已挂至少 1 个号、挂号最少、并列随机；取代理完整地址的绕路办法）
- `app/application/codex_rs_recovery.py`：`_restore_settings`（分组 / 代理 / 并发 / 权重被改动就改回）
- `app/application/codex_refill.py`：账号列表里 `quota.windows`（`windowSeconds`、`usedPercent`、`limitReached`）的读法
- `app/integrations/openai/desktop_version.py`：从 `GET /api/admin/dashboard/summary` 的 `wireProfiles[provider=openai].release.latestVersion` 读版本号
- rs 源码 `..\codex-proxy-rs\`（只读，确认接口字段，例如账号列表是否带工作区 ID、用户 ID）
- 新仓库 `crates/rs-client/src/lib.rs`

## 要点

- 请求：`x-api-key` 认证，不跟随重定向，不自动重试，15 秒超时；HTTP 只允许回环、`host.docker.internal` 和 Docker 服务名 `codex-proxy-rs`，其他主机要求 HTTPS（照老代码）。
- `list_accounts`：`GET /api/admin/accounts?provider=openai` 分页读全；映射成 `RsAccount`，额度窗口按 `window_seconds` 区分周限（≥ 604800 × 95%）和 5 小时窗口，由调用方挑选。字段缺失就填 `None`，不要编默认值。
- `find_by_email`：小写比较，可能返回多个（同邮箱不同团队）。
- `pick_proxy` / `proxy_for_account`：照抄老规则；代理完整地址（含账号密码）只在内存使用，不写日志。
- `import_account`：支持"新号带首次导入参数 + proxy_id"和"按身份覆盖已有号"两种；`restore_settings` 用于覆盖后改回原分组 / 代理 / 并发 / 权重。
- 导出类接口返回的刷新令牌在客户端里直接丢弃（照老代码），不向上返回。
- `desktop_app_version`：只负责读取，缓存由调用方做。

## 步骤

1. 读老客户端和 rs 源码，列出每个接口的请求与响应字段。
2. 实现客户端与各方法。
3. 写 README；跑检查。

## 完成标准

- [x] lib.rs 中所有 `todo!()` 已实现，公开签名未变（如有变化在完成记录写明）
- [x] README 写清每个方法、rs 版本依赖（字段来自哪个接口）
- [x] 基本检查通过：`bash scripts/check.sh`

## 完成记录

- 改动文件：`team48/crates/rs-client/src/lib.rs`（实现）、新建 `src/transport.rs`（地址校验、请求、信封、错误映射、分页）、`src/parse.rs`（JSON → 类型、代理地址规范化、版本号校验）、`README.md`。Cargo.toml 未改。
- 公开签名未变，只把参数名的下划线前缀去掉；没有新增公开项。
- 检查：`bash scripts/check.sh rust rs-client` 通过；`bash scripts/check.sh rust`（全 workspace cargo check + clippy -D warnings）通过。前端 / 插件不在本 Part 范围，没跑。
- 与计划 / 老代码的小偏差（自行决定）：
  - **`resets_at` 现在总是 `None`**：rs 额度窗口只给部署时区的展示文本 `resetAtDisplay`，没有 RFC 3339 原文；零件只在 rs 给出 `resetAt` 时才填。看板（part-7 / part-3）不要依赖重置时间。
  - 周限窗口加了上限 635040 秒（与 rs 的周额度判定一致），免得"多条取最长"取到月额度；5 小时窗口按 rs 的 17100 ~ 18900 秒判定（老代码没有这个判定）。
  - 只保留主额度桶（`limitId` 为 `codex` 或缺失）的窗口，按模型单独计的附加额度不进 `windows`，免得周限条显示成别的模型的额度。
  - 列表项缺 `email` 时 `RsAccount.email` 为空串（类型不是 Option）；缺 `id` 让整个列表报 `codex_invalid_list`。
  - `proxy_for_account` 返回的 `ProxyChoice.id` 为空串，表示该账号直接填的代理地址、不是 rs 保存的代理（类型不是 Option）。授权桥把空串当"没有 proxy_id"。
  - `restore_settings` 不改启用状态（照老代码）；`settings.outbound_proxy_id` 为 `None` 时不改代理（老代码会写回会话代理地址，新签名里没有地址）。回读不一致返回 `RsError::Invalid { code: "verify_failed" }`。
  - `OverwritePreserving` 的代理顺序：`proxy_id` → `settings.outbound_proxy_id` → `proxy_url`（写进账号条目的 `outboundProxyUrl`）。
  - 请求没发出去（连接失败）报 `codex_unreachable`，其余网络错误 / 超时报 `codex_response_uncertain`（老代码全部报后者）。4xx 时带上 rs 响应体的 `code`（转字符串）和 `message`。
  - 新错误码：`verify_failed`、`codex_no_desktop_version`，以及老代码已有的 `codex_list_limit`。
- ship 时写进文档：零件说明已在 `crates/rs-client/README.md`；ARCHITECTURE 无需改。
