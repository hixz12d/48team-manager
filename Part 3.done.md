# Part 3 交付记录

页内操作不再 `location.reload`。首屏仍 SSR，之后列表走 JSON 局部补丁。视觉不大改。

## 改了什么

### 子号池一键体验

- 进度区增加只读 `邮箱` / `接码`，来自 job 快照；空则显示「领取中」
- job 增加 `phone`，领取 HME / 号码池后写回
- 邮箱和接码都空时，主按钮改成「自动领邮箱和接码并邀请 / 注册 / 轮转」
- 失败也会刷新本地子号库；离开页面停止轮询

### 工作台不再整页刷新

- 刷新、删除、编辑保存、批量操作、导入、重新授权结束后拉 `GET /admin/teams/list`
- 删除成功后列表就地更新；批量刷新结束后补丁卡片而不是白屏

### 兑换码 / 记录 / 续期 / 设置

- 兑换码增删改、批量操作、生成后走 `GET /admin/codes/list`
- 状态筛选 / 搜索用 `history.replaceState`，不再整页跳
- 记录撤回、续期处理只改当前行
- 设置保存后该表单显示「已保存」；风格/配色客户端应用，不再 reload
- 改密码仍跳登录页（对的）

### 共用 helper

- `main.js`：`postJSON`、`withButtonState`、`refreshAdminViewIfPossible`、`markFormSaved`
- 静态资源 `?v=20260401-part3`

## 新 JSON 端点

- `GET /admin/teams/list`（`page` / `per_page` / `search` / `status_filter` / `pool_type`）
- `GET /admin/codes/list`（`page` / `per_page` / `search` / `status_filter`）

旧操作接口没改地址。

## 刻意没改

- 登录成功跳 `/admin` 仍整页
- 分页链接、每页条数仍整页（换页）
- 导航切页仍整页
- 兑换前台 `redeem.js`
- HME / 号码池业务规则、Sub2API、没有上 React

## 验收命令

```bash
.venv/Scripts/python.exe -m unittest
```

未部署。上机仅在用户要求时：`/opt/team48`，`docker compose -p team48 up -d --build --no-deps team48`。
