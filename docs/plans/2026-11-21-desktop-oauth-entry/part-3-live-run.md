# Part 3：上线后实测（2 个新邮箱）

**状态**：已完成
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

- [x] 2 个邮箱都有明确结果（成功导入 rs，或带截图的失败原因）——另加 myriad、house 两轮和用户手工对照
- [x] 完成记录写明是否需要 part-4——需要，但范围改为"注册环节"，方案待新窗口讨论

## 完成记录

**第 1 轮（2026-11-21 20:54，批次 `b-20261121-205411`，提交 panda.liable-4i@ / banking0calmer@，设置沿用上一批：启用、并发不限、权重 1、无分组）**

- 两个号结果相同：`phone_verification_required`，截图 `data/debug/20261121-205502`、`20261121-205543`，停在 `auth.openai.com/add-phone`。
- 浏览器历史还原的路径（两个号一致）：`chatgpt.com/` → `auth/login_with` → `email-verification`（HME 验证码，无密码、无 about-you）→ 回到 `chatgpt.com/`（会话确认）→ `chatgpt.com/codex/desktop-auth` → `oauth/authorize`（桌面版内层）→ **`choose-an-account`（选账号页，自动点中目标邮箱）** → `add-phone`。
- 结论：新流程每一步都按设计走通（登录沿用、选账号页出现并选对），但授权时仍被要求手机号。
- 干扰因素：这两个号今天已被旧流程自动化反复尝试过（19:03、19:14、19:16 都在授权页走到 `log-in/password`，更早一次已撞过手机页），账号本身可能已被标记为"需绑手机"。
- 用户本机无痕窗口手工复测（rs 桌面版链接 → 选账号页 "James Smith / panda…" → 继续）：两个号都同样跳 `add-phone`。结论：这两个号已被 OpenAI 标记，第 1 轮不能用来评价新流程。名字 "James Smith" 是旧流程固定填写的。
- 下一步：用户提供一个从未被自动化碰过、已入 Team、未登录过的新邮箱，单独跑第 2 轮。

**第 2 轮（2026-11-21 21:03，批次 `b-20261121-210325`，myriad-capote.2v@，服务器无该邮箱旧档案）**

- 结果 `phone_verification_required`，截图 `data/debug/20261121-210428`。
- 路径：`chatgpt.com/` → `email-verification` → `about-you`（新号创建并自动填资料）→ `chatgpt.com/`（21:04:21 会话确认）→ 2 秒后 `codex/desktop-auth` → `choose-an-account` → `add-phone`（21:04:27）。
- 新假设（证据：用户复测截图停在 `chatgpt.com/onboarding` 的 "… invited you · Kast标准 10141" 卡片，并在该页直接打开授权链接）：所谓"已拉进 Team"只是发了邀请，账号登录后要在 onboarding 页接受邀请才成为 Team 成员；未加入时账号只有个人免费空间，Codex 授权要求绑手机。用户平时手工流程会先在 onboarding 接受邀请再授权；服务器流程和用户对 panda 的复测都跳过了这一步。
- 用户否定了"未接受邀请"假设（多次手工流程验证过与此无关）。

**对照：用户手工注册新号（2026-11-21 21:1x，本机无痕窗口）**

- 新号手工注册（onboarding 显示 "You've been added to a new workspace … Get started"）后，用 rs 桌面版链接授权，直接拿到 `127.0.0.1:1455` 回调，未要求手机。
- 结论（用户与 Claude 一致）：服务器 / 插件自动化**注册**出来的号（myriad、panda、banking，以及 10-24 插件注册的 7 个号）授权时都被要求绑手机；手工注册的号不会。触发点大概率在注册阶段。
- 未确定：自动化"登录 + 授权"本身是否也会触发。待测：用户手工注册一个号到主页即停（不授权），交给服务器只做登录 + 授权。

**第 3 轮（2026-11-21 21:16，批次 `b-20261121-211642`，house-reducer-0y@，用户手工注册、停在 onboarding 邀请卡未点 Get started）**

- 路径：`chatgpt.com/` → `email-verification` → `chatgpt.com/`（21:17:10）→ `codex/desktop-auth` → `choose-an-account` → **`127.0.0.1:1455/auth/callback`（21:17:16，未出现手机页）** → 换票成功。
- 结果 `not_team_workspace`：账号尚未接受 Team 邀请，令牌是个人空间，核对正确拦下，未导入 rs。
- 结论：
  1. 桌面版入口 + 服务器自动登录授权对手工注册的号不触发手机验证——本计划的核心改动有效。
  2. 手机验证的触发点是自动化注册（服务器 Playwright、插件注册的号都会被标记）。
  3. 授权前需先在 onboarding 接受 Team 邀请（Get started），否则令牌是个人空间。
- 待用户决定：新号注册由谁做（手工 / Camoufox 改造注册 / 两者并行）；服务器授权前是否自动接受 onboarding 邀请。

**本 Part 结论（2026-11-21，用户确认）**：自动化注册时账号被 OpenAI 当成机器人账号标记，之后任何人、任何环境授权 Codex 都要求绑手机；登录 + 授权本身不触发。解决方案另开窗口讨论，未解决问题见 [part-4](./part-4-camoufox-fallback.md) 的"待讨论"。
- house-reducer-0y@ 当前状态：已手工注册、未接受 Team 邀请、未导入 rs；接受邀请后可在 rs 进度弹框点"重试"。
- 被标记、不要再用于测试的号：panda.liable-4i@、banking0calmer@、myriad-capote.2v@。

