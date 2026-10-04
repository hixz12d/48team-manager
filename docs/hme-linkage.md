# 48team 与 iCloud HME 联动

改领号、打标、占用判定前先看这份。HME 侧部署见 `../icloud-hme/docs/RUNBOOK.md`（本机相对路径），机器隔离见 [RUNBOOK.md](RUNBOOK.md)。密码、Cookie、服务 token 不进本文。

## 分工

| 动作 | 谁做 | 落点 |
|---|---|---|
| 空邮箱拉人 / 补位 / 自动轮转补位 | 48team `app/application/resources/hme.py` | 向 HME 要一个未占用别名，写本地租约 `hme_alias_leases` |
| 开号后打标 | 48team `finalize_claim` | HME `POST /api/aliases/:id/label` |
| 标签存储 | HME | VPS `/data/icloud-hme/accounts.json` 的 `local_labels` |
| 读验证码 | 48team 走 Cloudflare 临时邮箱；备用号池拉入的登录验证码走 HME 收件（不读邀请邮件） | HME `/api/inbox` |
| 备用号池 | 48team `standby_pool.py` | 导入检测收件成功后，原标签为空 / 序号的别名打本地标签 `GPT号池`（算已占用）；拉入成功改团队标签；移出号池恢复原标签 |
| 补库存 | HME 自动创建 | 只造序号标签别名 |

禁止：改 iCloud 原生备注、为打标调 iCloud generate/update（只走 `SetLocalLabel`）、本机和 VPS 同时对同一 Apple 账号开自动创建、动 `/opt/sub2api`。

## 三套"标签"

| 位置 | 是什么 | 领号认不认 |
|---|---|---|
| iCloud 原生 label | 创建别名时的备注，Apple 侧 | 认；没有本地覆盖时列表显示它 |
| HME `local_labels` | `anonymousId -> 文案`，只在 VPS `accounts.json` | **认，覆盖展示，是唯一占用库** |
| 浏览器快捷标签 `hme.quickTags` | 本机 localStorage | 不认，不同步到 VPS |

本机 `48team-manager/data/accounts.json` 和 HME 仓库里的 `data/accounts.json` 都是旧备份，**不是占用库**，领号路径里也不能读它。HME 列别名时已用本地标签盖住 iCloud 值，48team 看到的 `label` 就是覆盖后的。

## 未占用判定

与 HME 前端 `isSerialLabel`、48team `is_unoccupied_label`（`app/domain/resources/__init__.py`）一致：

- **未占用**：启用中，有 `anonymousId`，且标签为空 / 纯数字 / `别名 12`（允许中间空格）。
- **已占用**：任何其它文案，包括 `GPT已使用`、`已使用`、团队名、`GPT Team .2026.12`。
- 另外排除：本地租约中的邮箱，以及 `accounts` 表里仍在用的账号（`occupied_account_emails`：状态不是 unused/archived/disabled、用途不是 free/disabled）。
- 领取顺序：`createdAt` 升序，相同按邮箱 `zh-CN` 排序，不按序号数字。

历史手填邮箱如果没打本地标签、也不在 accounts 表里，仍可能被当成未占用领出去。

## 领号链路

```
maybe_claim_alias
  已填邮箱 → 原样返回，不领、不打标
  空邮箱   → claim_next_alias：列别名 → 排除租约和在用账号 → pick_next_unoccupied
             → 写租约（25 分钟，状态 reserved），冲突换下一个，最多 8 次
开号过程中 mark_signup_started：进入邮箱 / 短信 / 年龄页后租约转 signup_started
finalize_claim
  成功且有标签                → 打标，租约转 consumed 后删除
  失败但已真正开号            → 打标占用（默认 GPT已使用），同上
  失败且还没开号（代理握手、缺配置、取消等，见 should_occupy_failed_claim）→ 不打标，删租约
  打标失败                    → 保留租约并标 label_sync_pending，避免被再领
```

`signup_started` / `quarantined` / `manual_review` 状态的租约不会被自动删除，需要在 HME 资源页（`/resources/hme`）核对后处理（reconcile / retry-label / release）。租约表空着是正常的。

团队标签由 `resolve_workspace_tag` 生成：团队名 → 设置里的映射 → 母号邮箱 local-part。

## 改代码时

1. 占用规则改一处，HME 前端和 48team `is_unoccupied_label` 必须一起改，并补 `tests/test_hme_actions.py`。
2. 手填邮箱注册成功**不会**自动打标；新占用必须走空邮箱领取，或事后补 `local_labels`。
3. 失败是否打标看 `should_occupy_failed_claim`：还没开号的不打；过了邮箱 / 短信 / 年龄页的打。

## 不开号的链路检查

本机：`python -m unittest tests.test_hme_actions`

VPS（只探测、只选号，不打标、不开 ChatGPT）：

```bash
docker exec team48-manager python -c "import socket; print(socket.gethostbyname('icloud-hme'))"
docker logs team48-manager --since 30m | grep -i hme
```

预期：HME 资源页能看到未占用数量；干跑能选到序号标签、且不在 accounts 表里的地址；领租约后失败释放，标签不变、租约表为空。
