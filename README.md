# 48 Team Manager

自用 ChatGPT Team 控制台。从 [team-manage-refresh](https://github.com/loLollipop/team-manage-refresh) fork 后二开，给 4 个母号做「踢人不删、7 天再拉、输入邮箱自动注册接码、推 Sub2API」。

兑换前台、质保售后和定时自动踢人不是主路径。日常只做三件事：

1. 导入 4 个母号，每个绑自己的静态 ISP
2. 输入邮箱拉人，或把 standby 旧号再拉回来
3. 手动执行「今天的 1 踢 1 拉」

## 核心语义

- 踢人 `!=` 删除。踢完子号进 `standby`，密码、token、代理、Sub2API id 都留着
- 复用旧号走短路径：邀请 → 登录/接受 → 对账成功 → 再推 Sub2API
- 新号走完整路径：邀请 → 注册 → 邮箱 OTP → api668 接码 → 对账成功 → 推 Sub2API
- 不对账成功不补位，也不推 Sub2API
- 母号请求、子号浏览器、接码必须走各自 ISP；没代理直接报错
- Sub2API 和本项目部署在同一台美西机器上，推送直连，不走代理

## 本地开发

```powershell
cd C:\Projects\Github_Other_Projects\48team-manager
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chrome
copy .env.example .env
python -m uvicorn app.main:app --reload --port 8008
```

打开 `http://127.0.0.1:8008`，根路径会进后台。默认密码见 `.env` 的 `ADMIN_PASSWORD`。

## 上机部署

1. 每个母号在「编辑 Team」里填自己的静态 ISP
2. 系统中心填 Sub2API 地址和 Admin API Key
3. 子号池里贴邮箱 / 接码，执行拉人或今天的轮转
4. 删除子号是单独按钮，不会在踢人时发生

## 测试

```powershell
python -m unittest tests.test_child_accounts tests.test_proxy_support
```
