# Bybit2 新版注册实测记录（2026-09-19）

**最新结果：新 HME 已从 ChatGPT 主页注册成功，并以 Owner + Premium 加入 Bybit2；同浏览器、同页面继续 OAuth 时进入 `auth.openai.com/add-phone`，页面明确显示 `Phone number required`。授权未完成，没有 refresh token，未发送短信。** 用户已纠正注册入口：官方邀请存在后即可从主页注册，邀请邮件链接不是新版流程的前置条件。

## 主页注册续测（最新）

- 任务 `9793986e9dce`，保留同邮箱 `48.pickets-arrant@icloud.com` / 账号300，没有再次踢人、领 HME 或发送邀请。
- 新版 `BROWSER_SIGNUP_FLOW=extension` 改为确认官方邀请后从 `https://chatgpt.com/` 注册，`invite_entry=False`；按目标 Team 处理页面上的工作空间确认。身份检查、官方成员/角色/席位门禁、同页 OAuth 均保留。
- 42项相关单元测试、6项真实 Chromium 离线浏览器回归通过，含主页注册/接受工作空间/同页交接。仅更新既有 VPS 隔离目录源码，没有替换线上服务。
- 真实阶段：`signup → fill_email → email_otp → about_you → session → oauth_same_session → browser_open → manual_required`。本轮成功读取并提交邮箱验证码；此前邀请邮件缺失不能推断验证码也无法收取。
- 官方回读：母号 `hixz2616@gmail.com` 保留；新子 `48.pickets-arrant@icloud.com` 已加入，官方用户 `user-MSGKNROwfQ3j0tJ4M5qprOun`，Owner + Premium；待邀请数0。
- 新账号 `operational_state=active`、membership `joined`、auth `oauth_required`。已保存注册登录态 access token，没有 OAuth refresh token。任务状态 `partial`，错误 `phone_verification_required`。
- OAuth诊断保存于隔离目录 `data/debug/20260919-191640`；仅提取主机和路径确认为 `auth.openai.com/add-phone`。截图明确显示手机号必填，支持 Text Message / WhatsApp 验证；未填写号码或提交发送。
- HME 本地标签已在官方入组回读后改为 `Bybit2` 并核对；未改 Apple 原生标签。注册和续接进程均已退出，没有 Sub2API 推送或外部绑定。
- 账号、浏览器档案及邀请/注册记录保留；下一步只需续做该账号 OAuth，手机号要求不能通过本次“主页注册后同页授权”假设排除。

用户授权：在 `Bybit2` / `hixz2616@gmail.com` 空间移除唯一子号，领取一个新的 HME，使用新版注册逻辑注册入组后完成 OAuth。未授权购买或自动更换短信号码。

## 已确认对象与操作

- Workspace：`17`，`Bybit2`，官方 ID `50178e5e-2a3c-43e3-ba15-55f5d8e0ed32`。
- 母号：`hixz2616@gmail.com`，本地账号 `93`，官方用户 `user-VXkze2r1hl9nPtmVrtLwV4Fv`；Owner + Standard。未移除。
- 旧子号：`are.tapirs_1u@icloud.com`，本地账号 `292`，官方用户 `user-PqfWeyncx4yLDgnAu52irMKE`；Owner + Premium。
- 踢人前两次核对官方成员与待邀请列表，只有上述两个成员且没有待邀请。只按旧子号精确 ID 移除；官方回读确认只剩母号后才继续。
- 旧账号未删除，本地标记 standby / removed；未使用会联动 Sub2API 的通用轮转入口。
- 领取的新 HME：`48.pickets-arrant@icloud.com`，本地账号 `300`。仅领取这一个别名。
- 官方邀请：`5c5ca135-2365-41b6-802d-58b1e5cb98cb`，Owner + Premium（wire `account-owner` / `prolite`）。邀请已确认存在。

## 运行环境

- 隔离目录：宿主 `/opt/team48/data/experiments/managed-signup-bybit2-20260919T104912Z`，容器内 `/app/data/experiments/managed-signup-bybit2-20260919T104912Z`。
- 上传159份项目源码/公开扩展资源；未上传扩展私密邮箱配置。源码压缩包哈希核验通过。
- SQLite 在线备份保存在隔离目录 `before-team48.db`，未回滚覆盖生产数据库。
- 使用既有 Chromix `152.0.7977.82`。归档 SHA256 `85593ab5bedbdceee2196f2d1e9c603a09cf18f5dfb9bcc949c4232e7b35295f`，同时比较归档内与现有浏览器执行文件哈希。
- 仅实验进程设置 `BROWSER_ENGINE=chromix`、`BROWSER_SIGNUP_FLOW=extension`，保留有头运行与母号代理。
- 预检验证：隔离 CDP world 用虚构邮箱在完全拦截到本地的模拟表单中完成填写/提交；网页主 world 无权调用桥接；停止后面板消失。真实认证页仅导航读取，HTTP 200。Cloudflare 邮箱接口可读。
- 未替换 `/app/app`、未重启或部署服务、未修改 Compose、未访问或写入 Sub2API 禁区。

## 首轮历史：误将邀请邮件设为前置条件

1. 初始任务 `842caba7a0c7` 完成踢旧号、领取新 HME、发出邀请；等待邮件超时，返回 `invite_link_missing`。
2. Cloudflare 邮箱接口正常返回邮件数据，但最近50封查询中没有匹配该新 HME 的邮件；最新一封时间为 `2026-09-19 10:41:47`（接口原始时间），早于本轮邀请。
3. 核对同一邮箱的官方待邀请仍存在后，重发邀请**一次**，接口返回成功；没有撤销邀请或再领别名。
4. 以固定账号300、固定邮箱启动续接任务 `8791fcbb07ba`。再次等待邀请邮件，仍以 `invite_link_missing` 停止。
5. HME 备用网页邮箱读取：带 alias 参数返回 HTTP 422 / `UNSUPPORTED_FILTER`（当前来源不支持按别名精确筛选）；去掉 alias 请求近期邮件索引返回 HTTP 502 / `UPSTREAM_FAILURE`（读取邮件失败）。未修改 Apple 账号、Cookie、原生备注或转发设置。

截至首轮及首次同邮箱续接结束（10:59 UTC），尚未进入真实注册或 OAuth。此结论已被上方主页注册续测更新；不得把邀请链接当作当前阻塞。

## 首轮结束时的回读（历史，非当前状态）

- 官方成员：仅母号 `hixz2616@gmail.com`，Owner + Standard。
- 官方待邀请：`48.pickets-arrant@icloud.com`，Owner + Premium。
- 新账号：membership `invited`，auth `oauth_required`；没有 access token、没有 refresh token；本地已保存待注册密码。没有手机号。
- 本轮没有 OAuth session，没有外部绑定、没有 Sub2API 推送。
- HME 本地标签为 `GPT已使用`（原项目失败占用规则），无未释放租约、无待同步标签，避免此邮箱被下一次空邮箱任务重新分配。该标签不表示本次注册已成功；Apple 标签未改。
- 注册/续接进程和本次浏览器进程已退出。隔离目录与 `verified-report.json` 保留用于核对。

## 后续续接要求

必须继续 **`48.pickets-arrant@icloud.com` / 账号300**。现在已注册且已入组，直接续做 OAuth；不用邀请链接、不重复注册、踢人或领取新别名。当前具体阻塞是 OAuth 手机验证。

一次性脚本有独占运行锁，禁止删锁盲目重试。`team48_bybit2_trial.py` 是原替换记录，`team48_bybit2_continue.py --homepage` 是本轮主页注册记录；后续应核对官方成员与活动任务后使用已入组账号的授权入口。此次没有提供可用的手机号或短信接收方式，未调用号码池。
