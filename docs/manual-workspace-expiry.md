# 手动记录团队到期日期

在「账号与团队 → 按团队」中，点击团队名称下面的「＋ 填写到期日期」。已填写的团队会显示日期、剩余天数和「手动记录」，点击即可修改。团队详情里也保留同一个编辑区。

- 使用日历选择日期，也可以用「一个月后」「一年后」从今天快捷填写。月底和闰年会取目标月份的最后一个有效日期。
- 选日期后先预览，点击「保存日期」才写入。卡片立即更新，刷新页面后仍保留。
- 「清空日期」先清空输入框，再点击「保存清空」才删除记录；保存前可以「撤销修改」。
- 保存失败保留输入，允许重试；后台列表刷新不会覆盖正在编辑的日期。
- 按北京时间的日历日期计算：还有 8 天及以上为普通提示，0–7 天为黄色，到期当天显示「今天到期」，从次日开始显示红色「已过期 X 天」。

这是本地管理提醒，不是官方账单查询结果，不会自动停用账号、停止授权、踢人、取消订阅或停止计费。也没有新增站外通知。官方同步不会覆盖手动填写的日期。

## 数据与接口

`workspaces.manual_expires_on` 保存日期，`manual_expiry_updated_at` 保存最后修改时间。现有 SQLite 启动迁移会补齐两个可空字段，历史团队默认为未填写。

管理员接口：`PATCH /api/workspaces/{workspace_id}/expiry`。

```json
{"expires_on": "2026-09-30"}
```

清空时显式传 `{"expires_on": null}`。省略字段、传时间戳或无效日期返回 422，不修改原记录。团队列表和账号分组的 `expiry` 包含 `date`、`source`、`updated_at` 和 `timezone`。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_workspace_expiry tests.test_workspace_metadata tests.test_identity_queries tests.test_subscription_evidence_gate tests.test_migration_skeleton -q
node --test tests/workspace_expiry_ui.test.cjs tests/management_ui.test.cjs
.\.venv\Scripts\python.exe -m tests.browser_workspace_expiry
```

浏览器测试自行启动随机 localhost 端口、使用临时数据库和示例账号，只允许本地查询和到期日期修改；结束后关闭进程并清理数据库。截图输出到系统临时目录，覆盖 1440、768、390 像素宽度。
