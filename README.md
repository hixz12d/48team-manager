# 48 Team Manager

48 Team Manager is a small self-hosted operations console for managing multiple ChatGPT Team / Workspace accounts and their automation resources.

这是个人自用的运营控制台，不是 SaaS、CRM、兑换码平台或质保系统。

## Purpose

日常只做这些事：

1. 管理多个 Workspace / Team 母号
2. 管理当前在席子号和 standby / unused 子号
3. 查看官方额度
4. 发现授权失效并重新授权
5. 周额度满或封禁后安全轮转
6. 管理 HME alias、手机号与本地自动化运行代理
7. 与 Sub2API 同步账号/用量，并只读查看其代理目录
8. 查看后台 Operation

官方计划、Workspace Role、本地用途三者永远不能互相推断。

## Architecture

One web container, one SQLite database, one persistent operation runner, one browser execution slot.

```text
app/
  domain/          accounts, workspaces, identity, automation, resources
  application/     queries, commands, jobs
  integrations/    openai, sub2api, hme, sms, proxy
  web/             routes, schemas, templates, static
  persistence/     models, repositories, migrations
  core/            config, errors, security, time
legacy_import/     read-only legacy DB importer, not part of runtime
```

Stack: Python, FastAPI, SQLAlchemy 2, SQLite, Jinja2, vanilla JavaScript, APScheduler, Playwright, httpx / curl-cffi.

## Local development

```powershell
cd C:\Projects\Github_Other_Projects\48team-manager
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python -m uvicorn app.main:app --reload --port 8008
```

Open `http://127.0.0.1:8008`. Unauthenticated HTML requests go to `/login`. Default credentials come from `.env`.

New schema file is `data/team48.db`. Do not point the new app at a production `team_manage.db`.

## Configuration

See `.env.example` and `deploy.env.example`.

Safe defaults:

- official quota probe: on
- auto reauth: off
- auto rotate: off
- force refill: always false unless explicitly enabled

Secrets are stored hashed or encrypted. API responses return `secret_state` (`stored` / `missing`) and never echo raw secrets.

`IDENTITY_GMAIL_POLICY` is a local policy (`owner_only` / `warn` / `unrestricted`). The identity engine itself never maps Gmail to owner.

## Database

SQLite, WAL. New tables are created by `app/persistence/migrations/bootstrap.py`.

Legacy production files are not dropped. Import is read-only and lives in `legacy_import/`.

## Migration

Copy the old database, then dry-run:

```powershell
python -c "from pathlib import Path; from legacy_import.importer import inspect_legacy_db; print(inspect_legacy_db(Path('copy-of-team_manage.db')).as_dict())"
```

Conflicts go to manual review. Email suffix, Team in a name, or family prefix is only a hint.

## Operations

Long commands return immediately:

```json
{ "success": true, "operation_id": "..." }
```

Operations persist across refresh, navigation, browser close, and container restart. Completed steps are not replayed.

## Deployment

Default machine is documented in [VPS.md](VPS.md). This project only lives in `/opt/team48`.

```bash
cd /opt/team48
docker compose -p team48 up -d --build --no-deps team48
```

- compose project: `team48`
- container: `team48-manager`
- bind: `127.0.0.1:8018`

Do not deploy until explicitly approved.

## Safety boundaries

Never touch:

- `/opt/sub2api` and its Compose, `.env`, postgres, redis, data
- containers `sub2api`, `sub2api-canary`, `sub2api-postgres`, `sub2api-redis`
- `127.0.0.1:8100` / `127.0.0.1:8101`
- `docker compose down` or `--remove-orphans`

Sub2API is used only through its HTTP Admin API.

Sub2API owns the proxy catalog and is its only source of truth. Team48 only lists and probes that remote catalog; it does not create, edit, or sync proxy records to Sub2API. Local proxy profiles remain solely as compatibility data for browser automation, account-specific runtime URLs, and frozen historical operation snapshots. Account pushes omit `proxy_id`, preserving an existing remote binding and leaving new remote accounts unbound.

Do not run real kick, invite, or OpenAI billing/seat-changing actions without approval.

## Tests

```powershell
python -m unittest discover -s tests -v
```
