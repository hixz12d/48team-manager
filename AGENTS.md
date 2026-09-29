# AGENTS.md

## 开工必读

- 产品、术语、业务规则：`docs/PRODUCT.md`
- 代码结构与基本检查命令：`docs/ARCHITECTURE.md`
- 部署、服务器访问、回滚、排障：`docs/RUNBOOK.md`（涉及部署或服务器时必读）
- HME 领号 / 打标规则：`docs/hme-linkage.md`（改领号、打标、占用判定时必读）
- 进行中的计划：`docs/plans/`

## 工程约定

- 目录与文档：只写现状，直接改写，不追加历史；一件事只写一处；不写密钥。
- 临时文件放 `.tmp/`（已被 git 忽略），任务结束前删除。
- 不写测试；改完跑基本检查（命令见 `docs/ARCHITECTURE.md#基本检查`）。
- 计划执行：按 `docs/plans/<计划>/part-*.md` 执行，只改该 Part 列出的文件；开工把状态改成"进行中"，完成改成"已完成"并填写完成记录；执行 Part 时不改 docs 核心文档和 CHANGELOG，不提交 git。
- git：只在上线时统一提交并推送，提交说明用中文。

## 项目硬性规则

### 默认机器

后续部署、排障、SSH，默认就是速维云美西这台，细节见 [docs/RUNBOOK.md](docs/RUNBOOK.md)。密码只在本机 `VPS.local.md` 和 `C:/Projects/VPS/速维云-美国-8H8G-三网/机子登录.txt`。

### 绝对不能碰

- `/opt/sub2api` 及其 Compose、`.env`、postgres、redis、data
- 容器 `sub2api`、`sub2api-canary`、`sub2api-postgres`、`sub2api-redis`
- `127.0.0.1:8100` / `127.0.0.1:8101` 的 Nginx 主备
- 禁止 `docker compose down`、`--remove-orphans`，禁止在 `/opt/sub2api` 里执行任何 compose

本项目只活在 `/opt/team48`，Compose 项目名 `team48`，容器名 `team48-manager`，端口只绑 `127.0.0.1:8018`。

### HME 联动

空邮箱拉人会向 VPS 上的 `icloud-hme` 领未占用别名，成功后只打 `accounts.json` 本地标签，不写 iCloud。占用规则、回填记录和干跑测试见 [docs/hme-linkage.md](docs/hme-linkage.md)。不要把本机 `data/accounts.json` 当占用库，不要为打标去动 Apple。

### 其他

- 未经明确批准不部署；不在生产做真实踢人、邀请、改席位或计费相关的测试。
- 不要把服务指向旧的 `team_manage.db`。
- Sub2API 只通过其 HTTP Admin API 使用；代理目录以 Sub2API 为准，本项目只读。

## 沟通

- 用简体中文，面向非代码专业用户：先给结论，说人话，术语第一次出现时顺带解释。
