# Part 3：轮转接入运行器

**状态**：待开始
**依赖**：part-0 已完成
**所属计划**：[README](./README.md)

## 目标

`ROTATION_SIGNUP_RUNNER=extension` 时，手动轮转和自动轮转的"注册 + 授权"一段改由 `extension_runner.signup_and_authorize` 完成；邀请、邀请核对、HME 领号与收尾、推送、删旧、计数全部沿用现有逻辑。设为 `playwright`（默认）时行为与现在完全一致。

## 可改文件

- `app/application/onboard.py`
- `app/application/manual_rotation.py`
- `app/application/automatic_rotation.py`

## 只读参考

- `app/application/extension_runner.py`（part-0 定下的签名与 `RunnerOutcome`；实现由 part-2 完成，本 Part 只按签名调用）
- `docs/contracts/extension-runner.md`（错误码）
- `app/application/invitation_flow.py`（`authorize_joined`，runner 模式下不再调用）
- `app/application/jobs/browser.py`（`InvitedBrowserSession.try_reserve / close`）

## 要点

### `app/application/onboard.py`

- `invite_and_onboard`、`_invite_and_onboard_impl`、`refill` 增加参数 `signup_runner: str = "playwright"`，逐层透传。
- `invite_and_onboard` 开头的配置检查：`signup_runner == "extension"` 时改为 `validate_runner_configuration()`（不再要求 `validate_signup_assets`）；失败按现有 `BrowserEnvironmentError` 分支返回。
- `_invite_and_onboard_impl` 中，官方邀请核对通过之后（现在 `:540-554` 的循环之后）、原浏览器段（`:556` 起）之前加分支：`oauth_signup and signup_runner == "extension"` 时：
  1. `await check_runner_proxy(self._child_proxy(child, workspace))`，失败返回 `{"success": False, "error_code": "proxy_auth_unsupported", ...}`。
  2. 确保持有全局浏览器槽：有 `browser_session` 时 `await browser_session.try_reserve()`，拿不到返回 `browser_busy`。
  3. `if claimed is not None: await hme_service.mark_signup_started(db, claimed, stage="browser")`。
  4. `outcome = await signup_and_authorize(db, account=child, workspace=workspace, role=requested_role, seat_intent=requested_seat.value, proxy_url=self._child_proxy(child, workspace), job_id=job_id, on_stage=<同 :616-619 的 async on_stage>)`。调用前 `await db.commit()`。
  5. 成功：`child.operational_state="active"`、`local_purpose` 设为 child（母号除外），返回 `{"success": True, "status": "active", "joined": True, "authorized": True, "pushed": False, "child": serialize_child(child), "message": "..已入组并完成授权，未推送 Sub2API"}`。
  6. 失败：`child.auth_state = "oauth_required"`（若已 joined），返回 `{"success": False, "joined": outcome.joined, "authorized": False, "partial": outcome.joined, "error_code": outcome.error_code, "error": outcome.error or "..请使用同一邮箱继续", "child": serialize_child(child)}`。
- `invite_and_onboard` 里 `authorize_joined` 的调用条件加上 `signup_runner != "extension"`（runner 已经授权过）。
- `use_phone_pool` 在 runner 模式下忽略。

### `app/application/manual_rotation.py`

- 预检（`:370-375`）：`runner_enabled()` 时改为 `validate_runner_configuration()` 并对母号代理 `check_runner_proxy(owner.proxy)`，都在踢人之前；否则保留现有 `validate_configuration + validate_signup_assets`。
- 预检结果 / ctx 记录 `signup_runner`（`"extension"` 或 `"playwright"`）。**续接时以 ctx 里记录的值为准**，不随配置改变；旧任务 ctx 没有该字段时视为 `"playwright"`。
- `stage_onboard` 调 `invite_and_onboard` 时传 `signup_runner=self.ctx.get("signup_runner") or "playwright"`；其余（`_authorized_outside` 续接、`joined/authorized` 标记、`MANUAL_MARKERS`、`release_browser`）不变。
- 确认 `phone_verification_required`、`captcha_required` 仍命中 `MANUAL_MARKERS` 得到 `manual_required`；`runner_*` 类错误得到 `partial`，都能"继续轮转"。

### `app/application/automatic_rotation.py`

- `preflight`：`runner_enabled()` 时改为 `validate_runner_configuration()` + `check_runner_proxy(owner.proxy)`，否则保留现有检查。
- `refill_and_publish`：传 `signup_runner="extension" if runner_enabled() else "playwright"`；runner 模式下 `use_phone_pool=False`。

## 步骤

1. 在 `onboard.py` 加参数与 runner 分支。
2. 改手动轮转预检、ctx 与 `stage_onboard` 调用。
3. 改自动轮转预检与补位调用。
4. 通读 `ROTATION_SIGNUP_RUNNER=playwright` 时的代码路径，确认与改动前等价。
5. 基本检查。

## 完成标准

- [ ] 默认配置下三处调用参数与行为和改动前一致
- [ ] runner 模式下邀请只发一次、授权不重复走 Playwright、手机页得到 `manual_required` 并能"继续轮转"
- [ ] 带认证 HTTP 代理在踢人前被拒绝
- [ ] 基本检查通过：`.venv/Scripts/python.exe -m compileall -q app scripts` 与 `.venv/Scripts/python.exe -c "import app.main"`

## 完成记录
