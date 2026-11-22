# Part 1：官方接口零件

**状态**：已完成
**依赖**：part-0 已完成
**所属计划**：[README](./README.md)

## 目标

实现 `crates/openai`：带浏览器 TLS 指纹、走指定代理的官方 HTTP 客户端；Codex 授权链接（桌面版 / 旧版）、回调解析、换票、刷新令牌；从令牌解析身份和套餐；团队列表、成员、待接受邀请、邀请、撤回邀请、踢人。看板和授权桥都只通过这个零件和 OpenAI 打交道。

## 可改文件

- `team48/crates/openai/**`（含 part-0 建的 lib.rs；公开签名保持 part-0 定义的形状）
- `team48/crates/openai/README.md`（新建：这个零件做什么、不做什么、每个公开函数的行为）

## 只读参考

老仓库 `C:\Projects\VPS\天理云-美西国际大宽带-8H16G\48team-manager\`：

- `app/integrations/openai/chatgpt.py`：请求头、设备 ID、连接池隔离、授权链接（`create_oauth_authorize_url`、桌面版双层嵌套）、换票、刷新（含回退路径）、`/accounts/check`、成员 / 邀请 / 踢人 / 撤回
- `app/integrations/openai/member_adapter.py`：成员与邀请分页、防清空保护、踢人 404 算成功、写操作结果判定
- `app/integrations/openai/oauth_sessions.py`、`app/application/oauth_sessions.py`：PKCE、state、`REDIRECT_URI = http://127.0.0.1:1455/auth/callback`、`parse_strict_callback`
- `app/integrations/openai/desktop_version.py`：桌面版版本号的用法
- `app/application/codex_rs_onboard.py`：`TEAM_PLAN_TYPES`、`TEAM_PLAN_PREFIXES`
- `app/core/proxy.py`：curl_cffi 指纹与代理用法
- `docs/contracts/openai-invite.md`：邀请请求体与席位
- 新仓库 `crates/openai/src/lib.rs`（part-0 定义的签名）

## 要点

逐项对照老代码搬，宁可照抄也不要"改良"：

- **TLS 指纹**：用 wreq 模拟老代码 curl_cffi 所用的浏览器指纹；代理支持 http / socks5（含账号密码）；连接池按"账号 + 代理"隔离，不同母号不共享连接。
- **必带请求头**：`chatgpt-account-id`、`oai-client-version`、按账号固定的 `oai-device-id` 等，以老代码为准逐个核对。
- **授权链接**：`Desktop`（默认）外层 `chatgpt.com/codex/desktop-auth?authorize_url=...&codex_streamlined_login=true&no_universal_links=1`，内层无 `prompt` / `login_hint`，带 `originator=Codex Desktop`、`codex_app_version`（调用方传入）、按邮箱派生的稳定 ID，scope 含 `api.connectors.read` / `api.connectors.invoke`；`Legacy` 为旧 CLI 链接（`prompt=login`）。参数与老代码逐字一致。
- **回调解析**：只认回环主机（`localhost` 与 `127.0.0.1` 等价）+ 端口 1455 + 路径 `/auth/callback`，校验 state。
- **刷新令牌**：按老代码的主路径与回退路径实现；返回新的刷新令牌时调用方必须覆盖保存（README 里写明"刷新令牌会轮换，旧的作废"）。
- **身份**：从 id_token / access_token 解析 email（小写）、user_id、account_id（工作区）、plan_type；`is_team_plan` 与老代码 `TEAM_PLAN_TYPES` + 前缀规则一致（含 `self_serve_business_*`）。
- **成员与邀请**：分页读全；接口报总数 > 0 但解析出 0 条时返回错误而不是空列表（防止调用方清空旧名单）。
- **写操作**：邀请 `POST` 用复数 `email_addresses`、`resend_emails: true`、角色和席位按合约（Standard / Premium 对应官方 `default` / `prolite`，以合约文件为准）；撤回邀请是 `DELETE` 且**带请求体** `{"email_address": ...}`（单数）——确认 wreq 发出了请求体；踢人遇 404 / 已不在团队算成功。写请求**绝不重试**：超时或 5xx 返回 `WriteOutcome::Unknown`；只有 GET 可做有限重试（照老代码）。
- 429 带 Retry-After 返回给调用方，不自己等待。
- 不打印令牌、代理密码；日志里邮箱只留前 3 位。

## 步骤

1. 读老代码，列出每个接口的 URL、方法、请求头、请求体、响应解析、错误判定。
2. 实现 HTTP 客户端（指纹 + 代理 + 连接隔离）。
3. 实现 OAuth、身份解析。
4. 实现团队读写接口。
5. 写 `crates/openai/README.md`；跑检查。

## 完成标准

- [x] lib.rs 中所有 `todo!()` 已实现，公开签名未变（如有变化在完成记录写明原因和影响的调用方）
- [x] README 写清每个公开函数、错误类型和"不做什么"
- [x] 基本检查通过：`bash scripts/check.sh`

## 完成记录

- 由 Gemini 施工、Claude 验收。只改了 `crates/openai/**`：`lib.rs`（接子模块、`OfficialClient` 方法）、新建 `proxy.rs`（`ProxyUrl` 解析 / 脱敏，`socks5` 规范成 `socks5h`）、`oauth.rs`（PKCE、state、桌面版 / 旧版链接、回调严格校验、JWT 身份解析）、`http.rs`（wreq Chrome 136 指纹、每个客户端独立连接池、必带请求头、GET 有限重试、写请求关闭重试、换票 / 刷新含回退路径）、`team.rs`（`/accounts/check`（404 回退 v4 路径）、成员 / 邀请分页与防清空、邀请 / 撤回 / 踢人）、`README.md`。
- 公开签名未变（`WriteOutcome`、`SeatType` 等类型移到 `team.rs` 后在 lib.rs `pub use`，形状不变）。
- Claude 核对：桌面版授权链接内外层参数、scope、稳定 ID 派生与老代码逐项一致（URL 编码同为 `+` 空格）；邀请体 `email_addresses` / `role=standard-user` / `resend_emails=true` / `seat_type=default|prolite`；撤回 `DELETE` 带 `{"email_address"}`；踢人 404 或"已不在团队"记成功；写请求 `send_once` 只发一次，发送失败或 5xx 返回 `Unknown`；429 带建议等待秒数放进 `Rejected { code: "rate_limited" }`；401 写操作返回 `Rejected { code: "unauthorized" }`。
- 检查：`bash scripts/check.sh rust`（全 workspace cargo check + clippy -D warnings）通过（Claude 复跑）。
- 上线注意：未对真实官方接口实测（按规则不在本机发真实请求），切换后用一个团队的同步和母号授权做首次实测。

