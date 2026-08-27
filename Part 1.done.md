# Part 1 交付记录

邮箱空 = 自动领 HME；非空 = 旧路径。进度里能读到最终邮箱。HME HTTP 细节集中在 `app/services/hme.py`。

## 改了什么

### 独立仓库 `hixz12d/icloud-hme-hixz12`（私有）

本地二开（local labels、快捷标签、Cloudflare 读码、自动创建、service token、compose）推到用户自己的私有仓，**没有**强推 `xiaozhou26/icloud-hme`。`accounts.json` / Cookie / 管理员密码不进 Git。

服务端调用：请求头 `X-HME-Service-Token`（环境变量 `ICLOUD_HME_SERVICE_TOKEN`，至少 16 字符，启动后从进程环境清掉）。带 token 的 `/api/*` 视为已登录，写操作免 CSRF。Web UI 仍走管理员密码。

### 48team

- [`app/services/hme.py`](app/services/hme.py)：列账号/别名、按 `createdAt` + 邮箱排序取下一个未占用、打本地标签、健康探测
- 未占用 = `active` 且标签为空或序号（纯数字 / `别名 12`）；**不按序号比大小**
- SQLite 租约表 `hme_alias_leases`（25 分钟）。标签不当锁
- 拉人 / 免费号 / 轮转补新号：邮箱留空则领号，写入裸 iCloud 地址，读码仍走 Cloudflare
- 成功打标：Team 用 `team_name`（空则映射，再空用母号 local-part）；免费号打 `GPT已使用`。失败释放租约、不打业务标签
- 系统中心「iCloud HME」：地址、账号、token（留空沿用）、可选 team 映射、探测
- 子号池邮箱改为非必填，展示未占用数量

## VPS 怎么跑

HME 代码在 `/opt/icloud-hme`，**数据在数据盘** `/data/icloud-hme`（`accounts.json` / Cookie）。Compose 项目名 `icloud-hme`，容器 `icloud-hme`，端口 `127.0.0.1:8081`，网络 `team48_net`（external）。**不要**进 `/opt/sub2api`，不要动 `/data/sub2api-backups`，不要 `docker compose down` / `--remove-orphans`。

公网 UI：`https://icloud.xiaozhudf2026.foo`（Cloudflare Flexible，独立 Nginx `server_name`，回源 `127.0.0.1:8081`）。48team 容器内仍用 `http://icloud-hme:8081`，不走公网。

```bash
# 代码：本机 push 后
cd /opt/icloud-hme
git pull --ff-only
docker compose -p icloud-hme up -d --build

# 数据：另通道，绝不进 Git
# 本机 accounts.json -> /data/icloud-hme/accounts.json
```

`.env` 只在 VPS：`ICLOUD_HME_ADMIN_PASSWORD`、`ICLOUD_HME_SERVICE_TOKEN`、`HME_DATA_DIR=/data/icloud-hme`。

48team：

```bash
cd /opt/team48
git pull --ff-only origin main
docker compose -p team48 up -d --build --no-deps team48
```

容器内 `hme_base_url` 用 `http://icloud-hme:8081`。备选 `http://host.docker.internal:8081`。人类 UI 走 `https://icloud.xiaozhudf2026.foo`，不要改 sub2api 的 Nginx。

Cookie 会过期。更新后重新同步到 `/data/icloud-hme/accounts.json`，或打开 HME UI 更新。

## 怎么验收

1. 私有仓 diff 无私密
2. VPS `127.0.0.1:8081` 与 `https://icloud.xiaozhudf2026.foo` 可访问，data 在 `/data/icloud-hme`
3. 系统中心探测 HME，能看到未占用数量
4. 空邮箱拉人/免费号会领无业务标签的下一个别名
5. 手填邮箱走旧路径
6. 成功打 Team 名 / `GPT已使用`；失败不打标
7. 两个 job 不会领同一别名（租约）
8. `python -m unittest tests.test_hme tests.test_cloudflare_mail tests.test_onboard_flow tests.test_email_otp_resend`
9. sub2api 与 8100/8101 未动

本窗口默认**没有**在生产跑真实注册。
