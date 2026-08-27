# Part 2 交付记录

接码空 = 自动领池；非空 = 旧路径，不碰池。进度日志会写「领取 +1…」「换号原因」。管理 API 是 JSON，系统中心号码池导入后局部刷新表格。

绑满 / 号码无效：直接 `disabled`，**不加 used_count**。成功绑定才 +1，满默认 3 次变 `maxed`。recently used / WhatsApp / 接码超时不烧次数。

## 改了什么

- [`app/services/phone_pool.py`](app/services/phone_pool.py)：导入、租约领取、结果记账、释放、统计
- 表 `phone_pool`（[`app/models.py`](app/models.py) + [`app/db_migrations.py`](app/db_migrations.py)）
- 拉人 / 免费号 / 轮转 / 重注册：表单接码留空则到手机号页再领池；浏览器拒号后换下一个（有上限）
- 系统中心「号码池」：批量导入、启用/停用、清 maxed/disabled、次数/冷却/租约/换号上限
- 子号池三个接码输入改为选填

## 验收命令

```bash
.venv/Scripts/python.exe -m unittest tests.test_phone_pool tests.test_onboard_flow tests.test_proxy_support
```

未部署。上机仅在用户要求时：`/opt/team48`，`docker compose -p team48 up -d --build --no-deps team48`。不碰 `/opt/sub2api`，不用 `docker compose down`。
