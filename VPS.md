# 本机与速维云美西机的关联

后续在这个仓库里说到「上机 / 部署 / VPS」，默认就是这台，不用再贴登录信息。

## 机器

- 名称：速维云-美国-8H8G-三网
- 公网 IP：`156.238.254.8`
- SSH：`root@156.238.254.8:22`
- 登录密码不进 Git，只写在本机：
  - [C:/Projects/VPS/速维云-美国-8H8G-三网/机子登录.txt](/C:/Projects/VPS/速维云-美国-8H8G-三网/机子登录.txt)
  - 本仓库本地文件 `VPS.local.md`（已被 gitignore）

## 目录隔离（硬约束）

| 用途 | 路径 | 能不能动 |
|---|---|---|
| Sub2API 生产部署 | `/opt/sub2api` | 禁止。Compose、`.env`、`data`、postgres、redis 都别碰 |
| Sub2API 源码树 | `/opt/sub2api/source-main` | 禁止因本项目去 pull / 构建 / 重启 |
| Sub2API 主实例 | `127.0.0.1:8101` 容器 `sub2api-canary` | 禁止重启、重建、改 Nginx |
| Sub2API 备实例 | `127.0.0.1:8100` 容器 `sub2api` | 禁止 |
| 本项目 | `/opt/team48` | 只允许在这里构建和启停 `team48-manager` |
| 本项目数据盘 | `/data/team48` → `/opt/team48/data` | 50G 盘 `vdb1` 的 bind mount，浏览器档案和 SQLite 都放这里 |

本项目用独立 Compose 项目名 `team48`、独立网络 `team48_net`、独立容器名 `team48-manager`。  
禁止 `docker compose down`、`--remove-orphans`，禁止进 `/opt/sub2api` 执行任何 compose。

## 端口

本项目只绑本机回环：

- `127.0.0.1:8018` -> 容器 `8008`
- 公网域名：`https://48team.xiaozhudf2026.foo`，Nginx 只反代本项目，不改 Sub2API

不要占用 `8100`、`8101`、`8080`、`8200`、`8317`。本项目域名单独走 80/443 的独立 server_name。

宿主机访问 Sub2API 用 `http://127.0.0.1:8101`。8101 只绑回环，team48 容器要外挂现有网络 `sub2api_sub2api-network`，地址写 `http://sub2api-canary:8080`。只加入现有网络，不改 `/opt/sub2api` 的 Compose。

## 常用命令

```bash
ssh root@156.238.254.8
cd /opt/team48
docker compose -p team48 -f deploy/docker-compose.yml ps
docker compose -p team48 -f deploy/docker-compose.yml logs --tail 100
```

更新只重建自己：

```bash
cd /opt/team48
git pull --ff-only origin main
docker compose -p team48 -f deploy/docker-compose.yml up -d --build --no-deps team48
```

## Git 拉取

仓库是私有的。VPS 不要拷本机 `gh` 登录态或带 `repo` 权限的 Token，那是完整账号。

`/opt/team48` 已接成 `origin/main` 工作副本，用仓库只读 Deploy Key 拉代码：

- 密钥：`/root/.ssh/team48_deploy`（只读，不能 push）
- SSH 别名：`github.com-team48`
- remote：`git@github.com-team48:hixz12d/48team-manager.git`

推送只在本机：本机已用 GitHub CLI 登录 `hixz12d`，`git push origin main` 走本地凭据。VPS 只 `git pull`，不要在生产目录提交或回写 GitHub。

`.env` 和 `data/` 不进仓库，`git pull` 不会覆盖它们。
