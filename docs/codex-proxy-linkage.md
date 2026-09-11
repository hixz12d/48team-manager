# Codex Proxy 联动

本地实现面向 `codex-proxy-rs` 管理 API，不替换现有 Sub2API 集成。

## 凭据归属

- Team Manager 保管 RT 并继续执行原有刷新流程。
- 导出与推送只包含 AT 和可用的 ID token，绝不读取或发送 RT、session token、密码。
- 第一版是手动快照同步，不是后台定时同步。AT 到期前在 Team Manager 刷新，再次推送。
- 同一账号原有 Sub2API 的刷新行为不在本次调整范围内；本次只保证不向 Codex 新增 RT 副本。
- 本地 JWT 解析用于一致性与过期检查，不验证签名，也不能证明账号有 Codex 权限或额度。

## 第一步：文件导出

1. 在“账号与团队”勾选本地账号，点击“导出 Codex JSON”，确认敏感凭据导出。
2. 下载 `team48-codex-at-only.json`，在 Codex Proxy 的 OpenAI 账号导入界面导入。
3. 文件相当于临时登录凭据，不要提交 Git、发送聊天或放入公共目录。

单次最多 50 个账号，重复 ID 去重；任一账号缺失、停用、待授权、AT 即将过期、OAuth client 不符或身份不一致时整个导出拒绝，不生成部分文件。仅登记邮箱或持有 RT 而没有有效 AT 的账号，需要先完成授权或刷新。

## 第二步：一键推送

1. 在设置中填写 **Codex Proxy 服务地址**与 **管理员 API Key**并保存。这里不是 `/v1` 地址，也不是客户端调用 Key；请求使用 `x-api-key`。
2. 地址只允许服务 origin，例如 `https://codex.example.com`。只有 localhost / 127.0.0.1 / ::1 可使用 HTTP；其他主机必须 HTTPS。不自动跟随重定向，不读取系统代理变量。
3. 勾选账号，点击“推送到 Codex”，核对确认框中的目标地址。
4. 首次推送创建账号并绑定；之后推送调用 `/api/admin/accounts/rotate` 更新原账号的 AT / ID token，RT 始终为 null。
5. 在账号详情的 Codex Proxy 区域查看绑定、最近状态及凭据版本是否落后。

推送不指定 Codex 分组、并发、权重或调用 Key。这些沿用 Codex 的导入默认值，正式使用前在 Codex 中确认路由配置并用一次真实请求验证。

## 失败与重复防护

- 独立 `codex_bindings` 表按本地账号保存目标、远端 ID、创建意图、租约、凭据版本和错误码；不保存明文 token。
- 发送导入请求前持久化创建意图。响应丢失时，再次点击推送先按唯一名称和身份查找；找不到则报 `import_uncertain`，不盲目重复创建。
- 手动导入的同邮箱账号不会自动接管，报 `remote_exists`。不要先手动导入、再期望一键推送自动绑定同一账号。
- 已绑定的远端账号被删除、身份变化或持有 RT 时停止，不自动重建或清除其 RT。
- 改变目标时必须重新输入管理员 Key；已有其他目标的绑定时拒绝更换。
- 浏览器逐个提交，支持部分成功。网络中断后到账号详情核对状态，再决定是否重试。
- 本地租约只协调 Team Manager 发起的推送与本地删除，不锁住其他 Codex 管理员的操作。推送期间不要同时在 Codex 手动更改这批账号的身份或凭据。
- 删除本地档案会移除本地绑定，但不会删除或禁用 Codex 远端账号；有效 AT 仍可能可用至到期。需要停用时在 Codex 端明确操作。

## 部署与验证

本次不要求修改或重新构建 Rust 项目；接口与本地检查的 `codex-proxy-rs` 版本匹配。部署 Team Manager 时，其原有 schema bootstrap 会创建新表。生产部署前备份 Team Manager 数据库，保持现有 Sub2API 部署不变。

本地验证：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_codex_export tests.test_codex_publish tests.test_account_deletion -q
node --check app/web/static/js/accounts-view.js
node --check app/web/static/js/app.js
```

浏览器测试使用 `tests.preview_app:app` 隔离预览与 `tests/browser_codex_transfer.py`，所有 Codex 请求由测试拦截，不联系真实服务。真实管理员 Key、真实 token、官方权限、分组路由及 AT 到期后的续推，需要部署后另行授权实测。
