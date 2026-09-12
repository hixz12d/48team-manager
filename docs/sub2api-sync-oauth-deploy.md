# Sub2API `sync-oauth-credentials` 部署交接

> 历史交接记录。当前实现、兼容边界与实际测试结果以 [安全同步第一批交付](sub2api-safe-sync.md) 为准：已取消隐式 PUT 降级，auth_only 暂不开放，不应按下文旧行为验收。

## 为什么本仓库不直接部署

[AGENTS.md](/C:/Projects/Github_Other_Projects/48team-manager/AGENTS.md) / [VPS.md](/C:/Projects/Github_Other_Projects/48team-manager/VPS.md) 硬约束：

- 禁止改 `/opt/sub2api` Compose、`.env`、postgres、redis、data
- 禁止重启/重建 `sub2api` / `sub2api-canary`
- Team48 只活在 `/opt/team48`

因此 **窄接口代码落在本地 Sub2API 源码树**，上线必须走 **Sub2API 自己的发布通道**，不能由 Team48 代理热更 canary。

## 本地已落地的文件（源码树）

路径根：`C:/Projects/VPS/速维云-美国-8H8G-三网/sub2api-hixz12-main`

| 文件 | 作用 |
|---|---|
| `backend/internal/service/admin_account_credential_sync.go` | 窄写凭据 + CAS 鉴权恢复 |
| `backend/internal/service/admin_account_credential_sync_test.go` | helper 单测 |
| `backend/internal/handler/admin/account_credential_sync.go` | HTTP handler + token cache invalidate |
| `backend/internal/repository/account_repo.go` | `ClearAuthErrorOnly` |
| `backend/internal/server/routes/admin.go` | `POST /:id/sync-oauth-credentials` |
| `backend/internal/service/admin_service.go` | 接口声明 |

## 契约摘要

`POST /api/v1/admin/accounts/:id/sync-oauth-credentials`

- 只写 OAuth token 字段；不改 concurrency / group / schedulable
- **不调用** `ClearAccountError`（不会清 rate-limit / temp-unschedulable）
- `recovery_mode=auth_only` 时，仅在可识别鉴权错误 + token cache 失效成功后 CAS 清 status/error
- 响应区分 `credential_write` / `auth_recovery` / `token_cache_invalidation` / `schedulable`

## Team48 行为（已上代码，待 Team 部署）

- 优先调窄接口
- 404/405 → 仅 `PUT credentials`，**不** clear-error
- 文案区分「凭据已写入」与「恢复未完成 / 调度仍关」

## 建议发布顺序（Sub2API 维护者）

1. 在 **独立 canary 构建流水线** 合入上述 diff（不要从 Team48 容器热挂）
2. canary 冒烟：
   - `POST .../sync-oauth-credentials` contract_version=1
   - credentials_only 不改 schedulable
   - auth_only 仅清鉴权错误
   - 错误 rate-limit 账号不得被清冷却
3. Team48 指向 canary 验证一次手动推送 / 后台 refresh
4. 再按 Sub2API 既有主备切换流程推进（仍禁止 Team48 执行 compose）

## 当前线上预期

窄接口未部署前，Team48 会走兼容降级：

> 凭据已按兼容路径同步至原账号 #N；远端安全恢复能力不可用，旧鉴权错误未自动清理。
