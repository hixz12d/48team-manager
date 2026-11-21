# Part 2：生产容器冒烟与 2 个全新 HME 邮箱实测

**状态**：进行中（2026-11-21）
**依赖**：part-0、part-1 已完成，并已用 `/flow-ship` 构建镜像部署到生产
**所属计划**：[README](./README.md)

## 目标

在生产服务器上开启 `BROWSER_ENGINE=camoufox`，先做容器内离线冒烟确认 Camoufox + Xvfb + GeoIP 库运行正常，再用用户提供的 2 个**从未被自动化碰过、已发 Team 邀请**的全新 HME 邮箱跑"rs 邮箱自动授权新号"，验证"Camoufox 拟人注册 + 自动点 `Get started` 入组 + 15～30 秒主页停留 + 桌面版快捷授权"能否不触发手机验证并成功导入 codex-rs。

## 可改文件

- `docs/plans/2026-11-21-camoufox-signup/part-2-live-run.md`（仅填写本文件完成记录与状态；不改业务代码）

## 只读参考

- `docs/RUNBOOK.md`（SSH 访问、环境变量配置、容器重建、日志查看、回滚方式）
- `docs/PRODUCT.md`（rs 邮箱自动授权新号业务规则）
- Part 0、Part 1 的完成记录

## 要点

- **部署与切换前提**：
  - 确认 `/opt/team48/.env` 已设 `BROWSER_ENGINE=camoufox` 且容器已用 `--build` 重建、`team48-manager` 状态 `healthy`。
  - 保持自动轮转、自动补号、自动重授权开关处于关闭状态。
  - **绝不碰 `/opt/sub2api` 及其任何容器与网络**；不在生产做踢人、发新邀请、改席位或计费操作。
- **容器内 Camoufox 离线冒烟（不碰真实账号）**：
  - 镜像不含 `scripts/`，参照 RUNBOOK 把 `/opt/team48/scripts/browser_environment_smoke.py` 临时拷入 `team48-manager:/tmp/`，带 `-e DISPLAY=:99` 运行 `--engine camoufox --headed`，确认输出 `"ok": true`、`"same_profile_stable": true`，测完删掉 `/tmp/browser_environment_smoke.py`。（运行期间确保没有正在执行的浏览器任务。）
- **可选前置验证（验证自动点 `Get started` 入组）**：
  - 若上一计划留下的手工注册号 `house-reducer-0y@`（未点 `Get started`、干净未被标机器人）仍在 rs 批次中或用户愿意先测它，可在 rs 弹框点一次"重试"或单独提交它，验证服务器能自动点掉 `/onboarding` 的 `Get started` 并成功拿到 Team 令牌导入 rs（不再报 `not_team_workspace`，也不撞手机）。
- **核心验收实验（2 个全新 HME 邮箱）**：
  - 向用户索要 2 个**全新、从未登录/注册过、已由母号发好 Team 邀请**的 HME 邮箱（不要用 `panda.liable-4i@`、`banking0calmer@`、`myriad-capote.2v@` 等已被标记的旧号）。
  - 在 rs 管理台"添加账号 → OpenAI → 邮箱自动授权"提交这 2 个邮箱，全程跟踪 `docker logs -f team48-manager` 中 `codex-rs onboard` 日志与阶段变化：
    1. 是否以 `Camoufox` 环境启动、经代理出口设对时区；
    2. 是否顺利收邮件验证码、拟人填完 `about-you`；
    3. 是否触发 `onboarding` 自动点掉 `Get started` 入组；
    4. 是否执行 `post_signup_dwell`（15～30 秒主页停留）后再打开 `chatgpt.com/codex/desktop-auth`；
    5. 选账号页与工作空间后是否直接拿到 `127.0.0.1:1455` 回调并导入 rs（还是跳 `add-phone` / Cloudflare）。
  - 严禁接码硬过；若任一账号失败，读取对应 `data/debug/<目录>/` 截图与 `meta.txt` 定位精确页面和原因。

## 步骤

1. `ssh tln` 检查 `/opt/team48/.env` 中 `BROWSER_ENGINE=camoufox` 已生效、`team48-manager` 健康、日志已读到 `codex desktop app version`。
2. 无浏览器任务运行时，临时拷入并执行 `browser_environment_smoke.py --engine camoufox --headed`，确认容器内 Camoufox + Xvfb + 离线地理库正常，随后清理临时脚本。
3. （可选，先问用户）若 `house-reducer-0y@` 仍待入组导入，先跑它验证 `/onboarding` 自动点 `Get started` 有效。
4. 请用户提供 2 个全新的已邀 HME 邮箱并提交 rs 自动授权，观察完整阶段日志与耗时。
5. 成功的号在 rs 核对邮箱、Team 工作区、刷新令牌与正常状态；失败的号查看 `data/debug/` 截图分辨是页面选择器细节问题（回 Part 1 修）还是仍被 OpenAI 注册风控标记跳 `add-phone`。
6. 在下方"完成记录"如实记录每个号的完整路径、耗时、结果码与最终结论：
   - 2 个新号均免手机成功导入 rs → 方案验证成功，可进入 `/flow-ship` 收尾更新核心文档。
   - 若仍被标机器人跳 `add-phone` → 按 README 对齐结论，不接码，记录现场截图证据，后续另开计划用本机 HixBrowser（真显卡 Camoufox）只跑前半段注册、服务器跑后半段入组与授权。

## 完成标准

- [ ] 容器内 `browser_environment_smoke.py --engine camoufox --headed` 通过且临时文件已清理
- [ ] 2 个全新 HME 邮箱均有明确实测结果（成功导入 rs，或附带 `data/debug/` 现场截图与精确原因）
- [ ] 完成记录写明本方案是否彻底解决注册标机器人问题，以及是否需要启动本机 HixBrowser 备选方案

## 完成记录

<!-- 执行者填写：改了什么、偏离计划的地方、上线时要注意的事、需要写进 docs 的内容。 -->

**上线时已完成的前置（2026-11-21，/flow-ship，提交 `e44c8ca`）**
- 生产已部署 `e44c8ca`，`/opt/team48/.env` 已设 `BROWSER_ENGINE=camoufox`，容器 healthy。实际生效配置：`headless=False`、停留 15～30 秒。
- 步骤 1～2 已做：容器内 `browser_environment_smoke.py --engine camoufox --headed` 输出 `ok: true`、`same_profile_stable: true`，两个档案的 GPU 指纹不同；临时脚本已删除，没有留下档案。
- 实测前已按用户要求在设置页关闭"rs 掉登录自动重新授权"（`codex_rs.recovery_enabled=false`）。自动轮转、自动补号、自动重授权本来就是关的。实测通过后提醒用户重新打开。
- 剩余：步骤 3～6（可选的 `house-reducer-0y@` 和 2 个全新 HME 邮箱实测）。
