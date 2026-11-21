# Part 3：上线后实测（2 个新邮箱）

**状态**：待开始
**依赖**：part-0、part-1、part-2 已完成，并已用 `/flow-ship` 部署到生产
**所属计划**：[README](./README.md)

## 目标

在生产上验证新入口真的不撞手机：用用户给的 2 个"已拉进 Team、未注册"的新邮箱跑 rs 邮箱自动授权新号，成功即正式导入 rs。

## 可改文件

- 无（只操作生产界面、查看日志和数据；结论写进本文件"完成记录"）

## 只读参考

- `docs/RUNBOOK.md`（访问方式、日志、rs 邮箱自动授权新号自检）
- `docs/PRODUCT.md`（rs 邮箱自动授权新号规则）
- Part 0 / 1 / 2 的完成记录（新错误码含义、版本号同步）

## 要点

- 真实写入：会在 OpenAI 真实登录、创建账号、授权，并导入 rs。开始前向用户要 2 个邮箱，并确认这两个号已经在 Team 里。
- 不在生产做踢人、邀请、改席位、计费相关测试。
- 撞手机 / 人机验证时不接码，记录现场（`data/debug/<时间>/` 截图）后停。
- 用户已不用 Sub2API，不做 Sub2API 核对。

## 步骤

1. `ssh tln` 确认 `team48-manager` 健康；日志里能看到 `codex desktop app version -> ...`（版本号已从 rs 读到）。
2. 向用户要 2 个邮箱；在 rs 管理台"添加账号 → OpenAI → 邮箱自动授权"提交（或请用户提交）。
3. 跟踪 rs 进度弹框和 `docker logs team48-manager` 里 `codex-rs onboard` 日志；每个号记录：是否先登 chatgpt.com、是否出现选账号页、是否撞手机、最终结果和错误码。
4. 成功的号：在 rs 核对邮箱、Team、有刷新令牌、状态正常。
5. 失败的号：取 `debug_dir` 截图判断原因；`desktop_session_missing` / `oauth_invalid_state` / `proxy_geo_failed` 这类可重试的点一次"重试"。页面识别问题回 Part 1 修。
6. 把结论写进完成记录：
   - 2 个号都不撞手机 → 新入口有效，part-4 标"已完成：不需要"，计划可收尾。
   - 仍撞手机 → 按 README 结论进入 part-4（Camoufox）。

## 完成标准

- [ ] 2 个邮箱都有明确结果（成功导入 rs，或带截图的失败原因）
- [ ] 完成记录写明是否需要 part-4

## 完成记录

