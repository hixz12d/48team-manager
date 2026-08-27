# Part 1 · iCloud 邮箱池融合（新窗口施工提示词）

> 本窗口**只做 Part 1**。不要改电话池，不要做 Web 大重构。读完本文再动手。  
> 完成后在本仓库留下简短交付说明（改了什么、VPS 上怎么跑、怎么验收），供 Part 2 / Part 3 窗口接着做。

## 0. 你是谁、先读什么

你是 48 Team Manager 的工程代理。宿主是 Windows + PowerShell 7，默认中文回复。

**必读（按顺序）：**

1. [AGENTS.md](/C:/Projects/Github_Other_Projects/48team-manager/AGENTS.md)
2. [VPS.md](/C:/Projects/Github_Other_Projects/48team-manager/VPS.md)
3. [CONTEXT.md](/C:/Projects/Github_Other_Projects/48team-manager/CONTEXT.md)
4. [README.md](/C:/Projects/Github_Other_Projects/48team-manager/README.md)
5. 本文件

**对照仓库（只读参考，不要把它们的代码整份拷进 48team）：**

- 本地 iCloud HME（已二开，标签系统在这里）：[C:/Projects/Github_Other_Projects/icloud-hme](/C:/Projects/Github_Other_Projects/icloud-hme)
- 关键契约：[icloud-hme/API.md](/C:/Projects/Github_Other_Projects/icloud-hme/API.md)
- 序号 vs 业务标签：[icloud-hme/web/src/components/QuickTags.tsx](/C:/Projects/Github_Other_Projects/icloud-hme/web/src/components/QuickTags.tsx)
- 本地标签只写 `accounts.json`、不改 iCloud：[icloud-hme/internal/account/local_labels.go](/C:/Projects/Github_Other_Projects/icloud-hme/internal/account/local_labels.go)

**48team 现状入口：**

- 拉人 / 免费号 API：[app/routes/seats.py](/C:/Projects/Github_Other_Projects/48team-manager/app/routes/seats.py)（`OnboardRequest` / `FreeOnboardRequest` / `seats_onboard` / `seats_onboard_free`）
- 拉人引擎：[app/services/onboard.py](/C:/Projects/Github_Other_Projects/48team-manager/app/services/onboard.py)
- 邮箱解析与读码：[app/services/mail_otp.py](/C:/Projects/Github_Other_Projects/48team-manager/app/services/mail_otp.py)
- Cloudflare 读码（纯 iCloud 别名已经走这条）：[app/services/cloudflare_mail.py](/C:/Projects/Github_Other_Projects/48team-manager/app/services/cloudflare_mail.py)
- 子号池表单：[app/templates/admin/seats/index.html](/C:/Projects/Github_Other_Projects/48team-manager/app/templates/admin/seats/index.html)
- 测试：`tests/test_cloudflare_mail.py`、`tests/test_onboard_flow.py`、`tests/test_email_otp_resend.py`

## 1. 用户要什么（原话压缩）

现在拉人 / 注册免费号都要**手填邮箱**。麻烦。

本地已有 iCloud Hide My Email 管理系统。要融合，就必须：

1. **把这套 HME 部署到 VPS**（不是塞进 48team 进程里）
2. 流程：本机推 GitHub → VPS 拉代码 + **另通道同步自己的 data**（`accounts.json` / Cookie，绝不进 Git）
3. 深度融合到 48team：拉人或注册免费号时，**自动取下一个未占用别名**
4. 「下一个」**不要按序号大小排序**。HME 列表本来就不是按「别名 1、2、3」排的。过滤后取稳定顺序里的下一个即可
5. **未占用 = 没有任何业务标签**。纯数字 / `别名 12` 这种序号不当标签
6. 用出去之后再打标：
   - 拉进某个 Team → 打上**对应 Team 的标记**
   - 注册免费号 → 打 `GPT已使用`

成功后的体验：子号池里邮箱可以留空，点「邀请并授权」/「注册并授权」就能自己领号、读 OTP、用完打标。

## 2. 已核实的现状（不要再从零猜）

### 2.1 48team 邮箱怎么用

- 表单必填邮箱：`onboardEmail` / `freeOnboardEmail`，占位符已经是 `afraid-16.scepter@icloud.com`
- `OnboardRequest.email`：`邮箱，或 email----pickup_url；只填 iCloud 别名时走 Cloudflare 读码`
- `parse_mail_line()`：没有 pickup URL 且邮箱带 `@` → `use_cloudflare=True`
- OTP 实际读取：系统中心的 `cf_mail_base_url` / `cf_mail_address` / `cf_mail_admin_password`，默认 `https://apimail.xiaozhudf2026.foo` + `icloud@xiaozhudf2026.foo`
- **读码路径已经通。** Part 1 的缺口是「别名从哪来、用完怎么在 HME 打标」，不是重写 OTP 解析

### 2.2 本地 HME（必须部署这个二开树，不是上游干净版）

本地仓库 origin 是 `https://github.com/xiaozhou26/icloud-hme.git`，但 **main 上有大量未提交二开**：

- 本地标签 `LocalLabels`（`POST /api/aliases/:id/label`，只写本地，不覆盖 iCloud）
- 快捷标签默认 `GPT已使用`、`已使用`
- `isSerialLabel()`：纯数字或 `别名 12` 不是业务标签
- Cloudflare 邮箱、自动创建别名、中文 Web UI

**如果 VPS 去拉上游干净版，用户的标签系统会丢。必须推送并部署这份本地二开。**

数据在本机（gitignore，勿提交）：

- [C:/Projects/Github_Other_Projects/icloud-hme/data/accounts.json](/C:/Projects/Github_Other_Projects/icloud-hme/data/accounts.json)
- `admin_password.txt`、`data/.admin_password` 都是密钥

HME 默认监听 `:8081`，管理 API 需要 `hme_session` Cookie + 写操作 `X-CSRF-Token`。会话只在内存，重启即失效。这对 48team 服务端调用不友好，见第 4 节。

### 2.3 VPS 硬约束

- 机器：速维云美西 `root@156.238.254.8`
- 48team 只活在 `/opt/team48`，Compose 项目名 `team48`，容器 `team48-manager`，端口 `127.0.0.1:8018`
- **绝对不能碰** `/opt/sub2api`、其 Compose / `.env` / postgres / redis、容器 `sub2api*`、`127.0.0.1:8100` / `8101`
- 禁止 `docker compose down`、`--remove-orphans`
- 不要占用 `8100`、`8101`、`8080`、`8200`、`8317`
- 推送只在本机；VPS 只 `git pull`
- 密码只在本机 `VPS.local.md` 和机子登录 txt，不要写进 Git / 本 Part 文档 / 提交信息

## 3. 本窗口目标

1. 把**本地二开 icloud-hme** 整理成可部署仓库（无私密），推到用户自己的 GitHub（推荐私有 `hixz12d/icloud-hme`，**不要强推 `xiaozhou26/icloud-hme`**）
2. 在 VPS 独立目录部署 HME（建议 `/opt/icloud-hme`），拉代码 + 同步本机 `data/`
3. 48team 能作为服务调用 HME：列出别名、领取下一个未占用、成功后打标
4. 拉人 / 免费号 / 轮转补新号：邮箱留空则自动领号；手动填写仍可用
5. 领取与打标语义符合第 1 节；并发任务不能领到同一个别名
6. 补测试；本机能测的先测，上机只动 HME 和 48team，不动 sub2api

## 4. 已拍板的设计（不要再开一轮方案讨论）

### 4.1 进程隔离

| 项 | 决定 |
|---|---|
| 部署位置 | `/opt/icloud-hme`，独立 Compose 项目名 `icloud-hme` |
| 容器名 | `icloud-hme` |
| 端口 | 只绑 `127.0.0.1:8081`（HME 默认端口；不与禁口冲突） |
| 网络 | 可加入已有 `team48_net`，让 `team48-manager` 用 `http://icloud-hme:8081`。**不要**加入 `sub2api_sub2api-network` |
| 数据 | VPS `/opt/icloud-hme/data` 挂载；`accounts.json` 用 scp/rsync 从本机拷，不进 Git |
| Nginx | 本 Part **不必**公开域名。融合走内网/回环即可。若要临时看 UI，SSH 隧道，不要改 sub2api 的 Nginx |

48team 现有 `extra_hosts: host.docker.internal:host-gateway`，HME 绑回环时也可用 `http://host.docker.internal:8081`。优先 docker 网络 DNS，回环当备选。

### 4.2 不要把 HME 嵌进 48team 镜像

保持两个容器。48team 只当 HTTP 客户端。

### 4.3 读码 vs 领号

- **领号 / 打标**：走 HME API
- **注册时收 OTP / 邀请链接**：继续走现有 Cloudflare 读码（纯 iCloud 别名已经是这条）
- 自动领到的别名写入 `ChildAccount.email` / `mail_raw` 时，用**裸 iCloud 地址**（不要伪造 pickup URL），这样 `parse_mail_line` 会 `use_cloudflare=True`
- 不要为了融合去改 Cloudflare 邮箱服务本身

### 4.4 「下一个未占用」算法

业务标签 = trim 后非空，且 **不是** 序号。序号判定与 HME 前端一致，直接复用这个规则（48team 里写同样的函数并单测）：

```text
isSerialLabel(s) = /^\d+$/.test(s) || /^别名\s*\d+$/i.test(s)
```

未占用：

- `active === true`（停用的别名不要领）
- `label` 为空，或 `isSerialLabel(label)` 为真
- 没有被 48team 未过期租约占用

排序：**禁止解析序号比大小**。稳定顺序用 `createdAt` 升序，同分按 `email` 的 `zh-CN` localeCompare。取第一条。

用户原话：「不是按序号大小排序的嘛，所以只要自动调取下一个」。不要做「别名 2 用完再找别名 3」。

### 4.5 打标

| 场景 | 标签 |
|---|---|
| `seats_onboard` / 轮转补**新**号成功 | Team 标记：优先 `Team.team_name`（去空白）；空则用可配置映射，再空则用母号邮箱的 local-part。不要用「Team#3」这种内部 id 当用户标签 |
| `seats_onboard_free` 成功 | 固定 `GPT已使用` |
| 失败 / 取消 | **不要**打业务标签。释放租约，别名回到未占用 |
| 复用已有子号（standby 再拉） | **不要**再领新 HME 别名，也不要改旧标签 |

打标走 HME `POST /api/aliases/:anonymousId/label`（本地标签）。不要去 iCloud 改官方 label。

### 4.6 并发租约（不要用 HME 标签当锁）

不要打「占用中」——会污染用户的快捷标签。

在 48team SQLite 加租约表（名称可自定，语义如下）：

- `email` / `anonymous_id` / `account_id` / `job_id` / `expires_at`
- 领号时插入租约；任务成功则打标并删租约；失败/取消/过期则释放
- 过期时间覆盖一次浏览器注册（建议 20–30 分钟）
- 同一别名未过期租约视为已占用

### 4.7 服务间认证（HME 小改，值得做）

浏览器 Cookie + CSRF 不适合 48team 服务端。在二开 HME 增加 **service token**：

- 环境变量例如 `ICLOUD_HME_SERVICE_TOKEN`（至少 16 字符，启动后可从进程环境清掉，与 admin password 同样谨慎）
- 请求头例如 `X-HME-Service-Token`
- 带 token 的 `/api/*` 视为已登录，写操作免 CSRF
- **不要**用这个 token 替代人类 Web 登录；UI 仍走管理员密码
- token 只放 VPS `/opt/icloud-hme/.env` 和 48team 系统中心设置，不进 Git

48team 设置建议：

- `hme_base_url`（容器内 `http://icloud-hme:8081`）
- `hme_service_token`
- `hme_account_id`（多账号时指定用哪本 iCloud；只有一个可自动选 `active`）
- 可选 `hme_team_tag_map`（team_id → 标签）；默认用 `team_name`

系统中心加一组「iCloud HME」表单项：地址、账号、token、探测按钮（`GET /api/accounts` 或 aliases 计数）。token 回显与 Cloudflare 密码一样：已保存则留空表示不改。

### 4.8 表单行为

- `onboardEmail` / `freeOnboardEmail` / `rotateEmail`：**改为非必填**
- 空邮箱 + 新号路径 → 自动领 HME
- 填了邮箱 → 旧行为，不领池
- 进度面板要写出实际领到的邮箱
- 本 Part 只加必要的一小块 UI（探测、剩余未占用数量）。不要做整页 SPA

### 4.9 GitHub 与数据是两条通道

**代码：**

1. 清理 icloud-hme：`.gitignore` 已忽略 `/data/`、`accounts.json`、`admin_password.txt`。确认不会把 Cookie 提交进去
2. 提交二开（local labels、QuickTags、CF mail、service token 等）
3. 在 `hixz12d` 下建**私有**仓库（若已有则用已有的），本机 `git push`
4. VPS 用**只读 Deploy Key** 拉（学 48team 的 `github.com-team48` 模式，另做 `github.com-icloud-hme`）。不要把本机 `gh` 登录态拷到 VPS

**数据：**

```text
本机 icloud-hme/data/accounts.json  --scp-->  VPS /opt/icloud-hme/data/accounts.json
```

Cookie 会过期。文档里写清：更新 Cookie 后重新同步 data，或以后走 HME UI（隧道）更新。本 Part 至少完成**第一次**数据落地。

## 5. 建议实施顺序

### 阶段 A · HME 可部署

1. 核对 gitignore，扫一遍即将提交的 diff，确认无 Cookie / 密码
2. 加 service token 认证 + 测试
3. 补 `docker-compose.yml`（独立项目、数据卷、`127.0.0.1:8081`、`team48_net` external、restart）
4. 本机提交、推私有仓库
5. VPS：`/opt/icloud-hme` 拉代码，scp data，写 `.env`（`ICLOUD_HME_ADMIN_PASSWORD`、`ICLOUD_HME_SERVICE_TOKEN`），`docker compose -p icloud-hme up -d --build`
6. 只重建 HME。禁止任何 sub2api compose，禁止 `--remove-orphans`

### 阶段 B · 48team 客户端

1. `app/services/hme.py`（名称可自定）：loginless 调用、列别名、过滤未占用、打标、健康检查
2. 租约表 + `db_migrations.py`
3. onboard / free / rotate 新号路径：邮箱为空则 `claim → 把 email_line 设成裸别名 → 原流程`
4. 成功回调打标；失败释放
5. 系统中心配置 + 子号池表单非必填
6. 单元测试：`isSerialLabel`、过滤/排序、租约、空邮箱领号、失败不打标、手动邮箱不领池、free 打 `GPT已使用`、team 打 `team_name`

### 阶段 C · 上机融合

1. 48team 配置指向 HME
2. 本机推 48team → VPS `/opt/team48` `git pull --ff-only` → `docker compose -p team48 up -d --build --no-deps team48`
3. 用探测按钮确认 48team 能看到未占用数量
4. **不要**在生产上随便跑真实注册，除非用户明确说可以。默认用探测 + 单测验收

## 6. 明确不做

- 不改 `/opt/sub2api` 任何东西
- 不把 `accounts.json` 提交进任何 git
- 不把 HME 和 48team 合成一个容器
- 不重写 Cloudflare 读码，除非自动领号后发现 OTP 读不到（先查 `mail_raw` 是否裸地址、CF 设置是否还在）
- 不实现电话池（Part 2）
- 不把后台改成 React/SPA（Part 3）
- 不新增公开域名（除非用户后补要求）
- 不在 VPS 上 `git commit` / `git push`
- 不 `docker compose down`

## 7. 验收

- [ ] 本地二开 HME 已推到用户私有 GitHub，diff 无私密
- [ ] VPS `/opt/icloud-hme` 独立 Compose 在跑，`127.0.0.1:8081` 可访问，data 来自本机 accounts
- [ ] 48team 系统中心能探测 HME，能显示未占用数量
- [ ] 空邮箱拉人/免费号会领「无业务标签」的下一个别名（排序不看序号）
- [ ] 手填邮箱行为与现在一致
- [ ] 成功：Team 打 team 标签，free 打 `GPT已使用`（HME 本地标签）
- [ ] 失败：不打业务标签，别名可被下次领走
- [ ] 两个并发 job 不会领同一别名
- [ ] `python -m unittest` 覆盖新逻辑；至少跑 `tests.test_cloudflare_mail`、`tests.test_onboard_flow` 以及新测试
- [ ] sub2api 容器与 8100/8101 全程未动

## 8. 交给下一窗口的接口

Part 2 / 3 会假设：

- 邮箱空 = 自动领 HME；非空 = 旧路径
- 进度里能读到最终邮箱
- HME 客户端集中在一个 service 模块，不要把 HTTP 细节散落到模板

若行为有出入，在仓库里写一段 `Part 1` 交付说明（可用 `Part 1.done.md`，或在本文件末尾追加「交付记录」）。不要静默改契约。
