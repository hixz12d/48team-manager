# 接码中转协议（插件 ↔ 服务器）

插件遇到 OpenAI "添加手机号 / 验证手机号"页面时，向服务器领号、读短信验证码、报告结果。号码池、锁号、用量和接码链接只在服务器；插件只拿到号码和 `phoneId`，页面填写与提交由插件完成。本文是插件（`background.js` / `runner.mjs` / `content.js`）与服务端（`app/application/resources/phone_relay.py`、`app/web/routes/extension.py`、`app/web/routes/runner.py`）共同遵守的唯一标准。

## 1. 入口与鉴权

两个入口请求体相同，鉴权不同：

| 模式 | 地址 | 鉴权 | 额外字段 | 插件超时 |
|---|---|---|---|---|
| 本机插件 | `POST /api/ext/phone` | `Authorization: Bearer <EXTENSION_API_TOKEN>`（同其他 `/api/ext/*`） | `session`、`email`、`workspaceId` | 30 秒 |
| 服务器运行器 | `POST /api/ext/runner/{run_id}/phone` | 本次运行令牌（同 [extension-runner.md](extension-runner.md) 第 3 节） | 无（服务端从运行登记取） | 15 秒 |

- 请求只从后台 Service Worker 发，`credentials: 'omit'`、`cache: 'no-store'`、`Content-Type: application/json`；响应带 `Cache-Control: no-store`。
- 请求体 JSON，≤ 4KB；过大 `413`，字段不合法 `422`。
- 本机入口：令牌不对 `401`。
- 运行器入口：只有 `run_id` 未知 / 令牌不对 / 运行已结束才 `404`（插件照旧当"运行已结束"处理）；**业务失败一律 `200` + `ok:false`，不能回 404**。本次运行未开放接码 → `200 {"ok": false, "error_code": "phone_relay_disabled"}`。
- 接码期间的请求也刷新运行器心跳（`last_event_at`）。

## 2. 请求体

```json
{"action": "acquire",
 "phase": "signup",
 "phoneId": 5,
 "bound": false,
 "outcome": "no_sms",
 "session": "0f3c9a...（32 位小写十六进制）",
 "email": "new@example.com",
 "workspaceId": 12}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `action` | `"acquire"` / `"code"` / `"report"` / `"release"` | 必填 |
| `phase` | `"signup"` / `"oauth"` | 当前插件阶段；`oauth` 的接码记录记 `purpose="reauth"`，否则 `"signup"`。缺省按 `signup` |
| `phoneId` | int ≥ 0 | `code` / `report` 必填，取 `acquire` 返回值；已绑定号码为 `0` |
| `bound` | bool | 默认 `false`；`true` 表示页面只要求验证账号已绑定的号码（没有号码输入框） |
| `outcome` | 见第 4 节 | 仅 `report` 必填 |
| `session` | `^[a-f0-9]{32}$` | 仅本机模式，必填：插件 `job.id` 去掉横线后转小写 |
| `email` | string | 仅本机模式，必填：本任务账号邮箱 |
| `workspaceId` | 正整数或省略 | 仅本机模式，可选：已识别的团队 ID（`autoHandoff` / `handoff` 里的团队），用于找母号代理 |

运行器模式下 `session` / `email` / `workspaceId` 即使带了也忽略。

## 3. 动作与响应

所有响应都是 JSON 对象，`ok` 必有；失败时带 `error_code` 和中文 `message`（插件可直接展示）。**接码链接（含 key）永远不出现在任何响应里**；号码只在 `acquire` 成功时返回。

### `acquire`：领一个号

- `bound:false`：从号码池领一个号并锁住（锁 180 秒，每次 `code` 续期）。同一会话已锁着一个仍有效的号时，直接返回那个号（插件超时重试不会多占号）。
- `bound:true`：不动号码池，改用该账号本地记录的 `phone` / `sms_url`。
- 领号成功后服务器立即读一次当前短信作为"基线"，之后 `code` 只返回与基线不同的验证码（防止用到旧短信）。

| 响应 | 含义 | 插件动作 |
|---|---|---|
| `{"ok": true, "phoneId": 5, "number": "+15551234567", "bound": false}` | 领到号（`number` 为 E.164；`bound:true` 时 `phoneId` 为 `0`） | 填号 |
| `{"ok": false, "error_code": "phone_pool_empty", ...}` | 号码池没有可用号 | 暂停 `phone_pool_empty` |
| `{"ok": false, "error_code": "proxy_unknown", ...}` | 找不到读短信用的代理 | 暂停 `phone_relay_error` |
| `{"ok": false, "error_code": "bound_phone_missing", ...}` | `bound:true` 但本地没有该账号的号码 / 接码链接 | 暂停 `phone_relay_error` |
| `{"ok": false, "error_code": "phone_relay_disabled", ...}` | 运行器本次未开放接码 | 暂停 `phone`（同未启用中转） |
| `{"ok": false, "error_code": "invalid_request", ...}` | 会话标识 / 邮箱 / 动作不合法 | 暂停 `phone_relay_error` |

### `code`：读一次短信

服务器读一次接码链接（单次 ≤ 10 秒），同时给本会话的号码锁续期。接码链接从数据库取，不信任请求体。

| 响应 | 含义 | 插件动作 |
|---|---|---|
| `{"ok": true, "code": "123456"}` | 收到新验证码 | 逐位填写并提交 |
| `{"ok": true, "code": null}` | 还没到（读短信网络失败也是这个结果） | 5 秒后再问；由插件 90 秒计时决定换号 |
| `{"ok": false, "error_code": "lease_lost", ...}` | 这个号已不在本会话名下（锁过期被释放或被别人领走；`bound` 号则是服务器已没有记录） | 按"换号"处理，**不再 report** 这个号 |

### `report`：报告结果并释放锁

只接受当前仍由本会话锁着的 `phoneId`，否则回 `lease_lost`（不写任何记录）。`bound:true` 时只清服务器内存记录，不写号码池。成功回 `{"ok": true}`。

### `release`：任务停止时释放

释放本会话名下所有号，不写接码记录、不计用量。回 `{"ok": true}`。插件在本机任务停止、超时、标签页关闭时发一次，失败忽略（锁 180 秒后自动过期）。运行器模式由服务端在运行结束时自己释放，插件可不发。

## 4. 结果取值（`outcome`）

| `outcome` | 插件何时报 | 号码池记为 | 号码池影响 |
|---|---|---|---|
| `success` | 提交短信码后页面离开手机页进入后续步骤 | `success` | 用量 +1、进入 20 分钟冷却；有本地账号时把号码和接码链接写到账号 `phone` / `sms_url` |
| `invalid` | 页面说号码无效 / 已绑满 | `invalid` | 停用 |
| `recently_used` | 页面说号码刚被用过 / 请用其他号码 | `recently_used` | 进入冷却 |
| `risk` | 页面说发不出短信，或切到 WhatsApp | `risk` | 风险计数 +1（第 2 次停用） |
| `no_sms` | 号码提交后 90 秒没收到码 | `no_sms` | 连续 2 次转风险 |
| `wrong_code` | 页面说短信验证码错误 / 无效 | `provider_error` | 只记录 |
| `cancelled` | 插件放弃这个号（非号码原因） | `cancelled` | 只释放、不计用量 |

除 `cancelled` 外都写一条接码记录（`phone_attempts`），`operation_public_id` 为会话标识（运行器模式是任务 public_id，本机模式是 `session`）。

## 5. 插件节奏

- 号码页：填号后点 Continue。出现点击前没有的报错（或 `channel` 已从 `sms` 变成别的，即转 WhatsApp）→ 按报错 `report invalid / recently_used / risk`；点击后 20 秒仍在号码页、没新报错 → `report cancelled`。失败后刷新手机页换下一个号（不点"更换号码"）。
- `code`：号码提交后每 5 秒一次（网络请求放在页面任务队列之外）。
- 号码提交后 90 秒仍没码 → `report no_sms`，在短信页点"更换号码"回号码输入页换号。
- 一个账号最多换 3 个号（运行器：一次运行；本机：一次任务）。第 3 个号失败后不再领号，暂停 `phone_limit`。本机人工点"继续"后只再给 1 次机会，计数不清零。
- 请求超时：运行器 15 秒，本机 30 秒。网络错误 / 5xx：`code` 当作"还没到"继续计时；`acquire` 最多重试 2 次（间隔 3 秒），仍失败暂停 `phone_relay_error`；`report` / `release` 失败忽略（锁会自动过期）。
- 状态文字只显示号码后 4 位，不写全号、验证码、接码链接；诊断事件里也不带。

## 6. 暂停原因与阶段

新暂停原因（`shared.mjs` `PAUSE_REASONS`）：

| 原因 | 何时 | 提示 |
|---|---|---|
| `phone_pool_empty` | `acquire` 回 `phone_pool_empty` | 号码池没有可用号，去 Team48 资源页导入后点继续 |
| `phone_limit` | 3 个号都失败 | 已连续 3 个号失败，可能是账号或代理被风控，请人工处理 |
| `phone_back_missing` | 换号时找不到"更换号码 / 返回"入口 | 请手动回到填写手机号的页面后点继续 |
| `phone_relay_error` | 中转失败：代理未知、绑定号缺失、网络持续失败 | 带服务器返回的 `message` |

- 原有 `phone` 保留：未启用中转（未带 Team48 令牌的本机包、`submit` / `manual` 模式、托管注册、运行器 `phonePool:false`）时照旧以 `phone` 暂停。
- 服务器模式下这 4 个都是人工类最终暂停（`pauseFinal:true`）；服务端错误码为 `runner_paused_<原因>`，因含 `phone`，轮转判定为 `manual_required`（沿用 `MANUAL_MARKERS`）。

新阶段（`shared.mjs` `STAGES`）：`phone`（号码输入页）、`phone_otp`（短信验证码页）。运行器把 `phone` → 任务步骤 `add_phone`，`phone_otp` → `sms_otp`，注册和授权两个阶段都映射。`phone_otp` 页绝不能读邮箱验证码。

## 7. 本机接入时记号码

`POST /api/ext/handoff` 请求体增加可选 `phone_session`（与上面 `session` 相同的 32 位十六进制）。插件只在本任务有过 `success` 时带上；服务端查 `phone_attempts` 里 `operation_public_id == phone_session` 且 `result == "success"` 的最新一条，把对应号码和接码链接写到账号 `phone` / `sms_url`，并补上该记录的 `account_id`；找不到就跳过，不报错。

## 8. 服务端函数

`app/application/resources/phone_relay.py`（路由只调这些；调用方传入 `AsyncSession`，模块内部提交事务）：

```python
@dataclass(frozen=True)
class RelayContext:
    lease_key: str           # 号码池 reserved_by：运行器用任务 public_id，本机用 session
    account_id: int | None
    proxy_url: str
    phase: str               # "signup" / "oauth"

async def acquire(db, ctx, *, bound: bool) -> dict
async def poll_code(db, ctx, *, phone_id: int, bound: bool) -> dict
async def report(db, ctx, *, phone_id: int, bound: bool, outcome: str) -> dict
async def release(db, ctx) -> dict
async def handle(db, ctx, *, action, phone_id=None, bound=False, outcome=None) -> dict   # 按 action 分派
async def personal_context(db, *, session, email, workspace_id, phase) -> RelayContext | dict  # dict 即失败响应
```

- 本机模式代理查找顺序：① 该邮箱本地账号的 `proxy`；② `workspaceId` 对应团队的母号 `proxy`；③ 现查团队（`resolve_extension_workspace`，最多 25 秒）后取母号 `proxy`。找到后按 `session` 缓存 2 小时，同一任务后续请求不再现查。都没有 → `proxy_unknown`。
- 运行器模式代理用运行时的代理（子号代理，否则母号代理），由 `runner_phone_context` 提供（见 [extension-runner.md](extension-runner.md) 第 6 节）。
- 基线与已绑定号码记录在进程内存里；服务重启后基线丢失，此时任何读到的码都算新码。
- 日志不记录号码、接码链接、验证码。
