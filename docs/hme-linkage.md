# 48team 与 iCloud HME 联动

以后改领号、打标、占用判定，先看这份。HME 侧部署摘要见 [`icloud-hme/48TEAM.md`](../../icloud-hme/48TEAM.md)，机器隔离见 [VPS.md](../VPS.md)。

密码、Cookie、服务 token 不进本文。

## 各干什么

```
48team-manager  --列别名 / 领未占用 / 打本地标签-->  icloud-hme
       |                                              |
       | 读验证码                                      | Cookie 调 Apple
       v                                              v
 Cloudflare 临时邮箱                              iCloud Hide My Email
```

| 动作 | 谁做 | 落点 |
|---|---|---|
| 空邮箱拉人 / 开免费号 | 48team `app/services/hme.py` | 向 HME 要一个未占用别名，写短租约 |
| 开号成功打标 | 48team `finalize_claim` | HME `POST /api/aliases/:id/label` |
| 标签存储 | HME | `/data/icloud-hme/accounts.json` 的 `local_labels` |
| 读验证码 | 48team Cloudflare | 不走 HME IMAP |
| 补库存 | HME 自动创建 | 只造「序号标签」别名，供以后领取 |

禁止：改 iCloud 原生备注、动 `/opt/sub2api`、本机和 VPS 同时对同一 Apple 账号开自动创建。

## 三套「标签」不是一回事

| 位置 | 是什么 | 领号认不认 |
|---|---|---|
| iCloud 原生 label | 创建别名时带上的备注，Apple 侧 | 认。没有本地覆盖时，列表就显示它 |
| HME `local_labels` | `anonymousId -> 文案`，只在 `accounts.json` | **认。覆盖展示，是占用库** |
| 浏览器快捷标签 `hme.quickTags` | 本机 localStorage，例如 `GPT Team .2026.12` | **不认**，不会同步到 VPS |

本机 `48team-manager/data/accounts.json` 和 HME 仓库里的 `data/accounts.json` 都是旧备份，**不要当占用库**。生产只认 VPS `/data/icloud-hme/accounts.json`。账号卡片上的 `alias_total` 也是旧缓存，以实时列表为准。

HME 列别名时：`ApplyLocalLabels` 用本地标签盖住 iCloud 返回值。48team 看到的 `label` 已经是盖过的。

## 未占用判定

与 HME 前端 `isSerialLabel`、48team `is_unoccupied_label` 一致：

- **未占用**：启用中，且标签为空 / 纯数字 / `别名 12`（允许中间空格）
- **已占用**：任何其它文案，包括 `GPT已使用`、`已使用`、Team 名、`GPT Team .2026.12`
- 未启用、没有 `anonymousId`、租约未过期：都不领

领取顺序：`createdAt` 升序，相同则按邮箱 `zh-CN` 近似排序。不是按序号数字。

实现：

- 48team：[app/services/hme.py](../app/services/hme.py) `pick_next_unoccupied` / `claim_next_alias`
- HME：`internal/account/local_labels.go`、`POST /api/aliases/:id/label`

领号看 HME 标签 + 租约，并且排除 `child_accounts` 里已有的邮箱。历史手填邮箱如果没打本地标签、也不在子号库，仍可能被当成未占用再领出去。

## 领号链路（空邮箱）

```
maybe_claim_alias
  已填邮箱 → 原样返回，claimed=None（不领、不打标）
  空邮箱   → claim_next_alias
               列账号、列别名、跳过租约、pick_next_unoccupied
               写入 hme_alias_leases（约 25 分钟）
               冲突则换下一个，最多 8 次
拉人 / 注册免费号（ChatGPT）
finalize_claim
  成功 + 有标签 → set_local_label，再删租约
  失败且已有子号 → 打标占用（免费号用 `GPT已使用`），再删租约
  失败且没有子号 → 不打标，删租约（别名可再领）
  打标失败     → 保留租约，避免马上被别人领走
```

入口：

- Team 拉人：`OnboardService.invite_and_onboard`，成功标签 = `resolve_team_tag`（优先 `team_name`，否则系统中心映射，再否则母号 local-part）
- 免费号：`register_free_account`，成功标签 = `GPT已使用`
- 探测：车位页 `GET /admin/seats/hme-status`、系统中心「只探测不保存」

租约表空着是正常的：成功打标或失败释放后都没有行。

## 当前生产（2026-08-27 回填后）

VPS 账号 `acc_25cb7f9d`。HME `http://icloud-hme:8081`，48team 用服务 token。

从 48team 库反推，经 HME API 写了 **40** 条 `local_labels`（不改 iCloud）：

| 来源 | 标签 |
|---|---|
| 当前 Team / 邀请中 / 最近 Team | `team_name`（已 trim） |
| 子号 `status=free` | `GPT已使用` |
| 席位事件、账本用过但对不上 Team | `已使用` |

对不上的：`17workout-tidier@icloud.com` 在 Dual World 记录里，但不在这批 iCloud 别名中。  
原来 iCloud 自带的 5 个 `GPT已使用` 没动。

备份：`/data/icloud-hme/accounts.json.bak-20260827-151442`

回填后：HME 启用约 604，业务标签 45，序号标签约 559。48team 探测「未占用」约 **524**（还要有 `anonymousId` 才领），车位页以这个数为准。刷新 HME 页面才能看到分组。

## 以后改代码时

1. 占用规则改一处，HME 前端和 48team `is_unoccupied_label` 必须一起改，并补 [tests/test_hme.py](../tests/test_hme.py)。
2. 不要为了打标去调 iCloud generate/update。只走 `SetLocalLabel`。
3. 不要在领号路径里读本机 `accounts.json`。
4. 手填邮箱注册成功**不会**自动打标。新占用必须走空邮箱领取，或事后补 `local_labels`。
5. 领号排除 `child_accounts` 已有邮箱。失败但已经建了子号的别名会打标，避免下一单再领同一封。
6. 重建容器：`docker compose -p team48 up -d --build --no-deps team48`。禁止 `down`、`--remove-orphans`，禁止进 `/opt/sub2api`。

## 不创建账号的链路测试

本机：

```bash
python -m unittest tests.test_hme
```

VPS 上只探测、只选号、必要时领租约再立刻失败释放（不打标、不开 ChatGPT）：

```bash
docker exec team48-manager python -c "import socket; print(socket.gethostbyname('icloud-hme'))"
docker logs team48-manager --since 30m | grep -i hme
```

车位页应显示「HME 未占用别名」约 524。干跑应能选到序号标签、不在 48team 子号/成员表里的地址；领租约再失败释放后标签不变、租约表为空。
