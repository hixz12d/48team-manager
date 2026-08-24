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

本项目用独立 Compose 项目名 `team48`、独立网络 `team48_net`、独立容器名 `team48-manager`。  
禁止 `docker compose down`、`--remove-orphans`，禁止进 `/opt/sub2api` 执行任何 compose。

## 端口

本项目只绑本机回环：

- `127.0.0.1:8018` -> 容器 `8008`
- 公网域名：`https://48team.xiaozhudf2026.foo`，Nginx 只反代本项目，不改 Sub2API

不要占用 `8100`、`8101`、`8080`、`8200`、`8317`。本项目域名单独走 80/443 的独立 server_name。

同机访问 Sub2API 用 `http://127.0.0.1:8101`，容器里写 `http://host.docker.internal:8101`。这条是直连，不走代理。

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
