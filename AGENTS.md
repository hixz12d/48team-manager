# 48 Team Manager 协作规则

## 默认机器

后续部署、排障、SSH，默认就是速维云美西这台，细节见 [VPS.md](/C:/Projects/Github_Other_Projects/48team-manager/VPS.md)。密码只在本机 `VPS.local.md` 和 `C:/Projects/VPS/速维云-美国-8H8G-三网/机子登录.txt`。

## 绝对不能碰

- `/opt/sub2api` 及其 Compose、`.env`、postgres、redis、data
- 容器 `sub2api`、`sub2api-canary`、`sub2api-postgres`、`sub2api-redis`
- `127.0.0.1:8100` / `127.0.0.1:8101` 的 Nginx 主备
- 禁止 `docker compose down`、`--remove-orphans`，禁止在 `/opt/sub2api` 里执行任何 compose

本项目只活在 `/opt/team48`，Compose 项目名 `team48`，容器名 `team48-manager`，端口只绑 `127.0.0.1:8018`。

## HME 联动

空邮箱拉人会向 VPS 上的 `icloud-hme` 领未占用别名，成功后只打 `accounts.json` 本地标签，不写 iCloud。占用规则、回填记录和干跑测试见 [docs/hme-linkage.md](/C:/Projects/Github_Other_Projects/48team-manager/docs/hme-linkage.md)。不要把本机 `data/accounts.json` 当占用库，不要为打标去动 Apple。
