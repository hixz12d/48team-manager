# Part 4：注册插件搬家与瘦身

**状态**：已完成
**依赖**：part-0 已完成
**所属计划**：[README](./README.md)

## 目标

把老仓库的 Chrome 注册插件搬到新仓库 `extension/`，只保留"直接授权接入 rs（不查团队）"一种接入方式和批量注册；去掉查团队、指定团队、号码池接码、服务器运行器模式等用不到的代码。打包脚本从 Python 改成 Node。插件只调授权桥的 `/api/ext/ping`、`/api/ext/rs/start`、`/api/ext/rs/complete`。

## 可改文件

- `team48/extension/**`（新建，内容从老仓库复制后修改）
- 老仓库文件一律不改

## 只读参考

- 老仓库 `C:\Projects\VPS\天理云-美西国际大宽带-8H16G\48team-manager\extensions\chatgpt-signup\`（全部文件，包括 `README.md`）
- 老仓库 `scripts/build_signup_extension.py`（打包逻辑、`private-config.mjs` 生成方式、参数）
- 老仓库 `docs/PRODUCT.md` 的"插件接入""本机插件批量注册""插件接码"三节
- 新仓库 `docs/contracts/bridge-api.md`

## 要点

- **复制来源**：以老仓库**当前工作区**的文件为准（目前有未提交的 0.11.1 改动：`README.md`、`content.js`、`manifest.json`，属于用户进行中的工作，原样带过来，不回退）。开工前 `git -C <老仓库> status` 看一眼，在完成记录写明复制时的版本号。`private-config.mjs` 含密钥，**不复制**，由新打包脚本生成。
- **保留**：单个注册、批量无痕注册（`batch.mjs`）、HME / Cloudflare 读验证码（`hme.mjs`、`cloudflare.mjs`）、注册完同页桌面版授权、拦截 `127.0.0.1:1455` / `localhost:1455` 回调交授权桥、唯一 Team 盲选（`pickSingleTeam`，≥ 2 个 Team 报 `workspace_ambiguous`、只有个人空间报 `team_missing`）、遇人机验证 / 手机验证暂停交人工、提示音、进度页、复制成功账号密码。
- **去掉**：接入方式下拉框里的"查 Team48 团队并接入"和"指定团队接入"（只剩直接接入 rs，可直接去掉下拉框）；对 `/api/ext/workspaces`、`/resolve`、`/handoff`、`/handoff/complete`、`/phone` 的调用；号码池接码全部流程（`content.js` 里手机号页的填号 / 换号 / 短信步骤，`background.js` 的接码调用和暂停原因 `phone_pool_empty` / `phone_limit` / `phone_back_missing` / `phone_relay_error`）——手机页只保留"识别到就暂停交人工"；服务器运行器模式（`runner.mjs`、`RUNNER` 配置相关分支）。
- 删代码时顺着调用链清干净，不留死分支；不改仍保留功能的行为和文案。
- `manifest.json` 版本升到 `0.12.0`，描述改成"注册 ChatGPT 并直接授权接入 codex-rs"；权限里只为删掉的功能存在的项一并去掉（逐个确认确实没用了）。
- **打包脚本** `extension/scripts/build.mjs`：用 Node 重写老 Python 打包脚本的功能（生成 `private-config.mjs`：Team48 地址 + 令牌、邮箱读码配置；输出 `extension/dist/chatgpt-signup/` 和 ZIP），参数名尽量沿用；去掉与运行器、接码相关的参数。只用 Node 内置模块（ZIP 若需第三方库，改为输出目录 + 说明用系统压缩，或在完成记录说明）。
- `extension/README.md` 改写成现状：功能、安装、打包、常见问题；删掉已去掉功能的说明。

## 步骤

1. 复制老插件目录（不含 `private-config.mjs`）到 `extension/`。
2. 按"去掉"清单删代码，逐个功能核对保留项未受影响。
3. 写 `scripts/build.mjs`，本机用假令牌打一次包验证，产物不提交，用完删掉。
4. 改 manifest、README；跑检查。

## 完成标准

- [x] `extension/` 内搜不到 `/api/ext/workspaces`、`/handoff`、`/phone`、`/resolve`、`RUNNER`、`phonePool`
- [x] 打包脚本能生成可加载的插件目录
- [x] 基本检查通过：`bash scripts/check.sh`（含 `node --check`）

## 完成记录

- 由 Gemini 施工、Claude 验收。只改了新仓库 `extension/**`，老仓库未改（老插件那 3 个未提交改动仍在老仓库原样保留）。
- 复制来源：老插件工作区 0.11.1（含未提交改动）；新插件 `manifest.json` 升到 0.12.0，描述改为"注册 ChatGPT 并直接授权接入 codex-rs"；权限逐项核对后都仍需要，没有删。`private-config.mjs` 没复制。
- 删掉：`runner.mjs`（服务器运行器）、接入方式下拉框（查团队 / 指定团队），改为勾选"注册完成后自动授权接入 codex-rs"（默认勾选，不勾则只注册，之后可点"授权接入 codex-rs"按钮手动接入）；对 workspaces / resolve / handoff / phone 的调用；号码池接码全部流程（手机页只识别后暂停交人工）；Sub2API / 切号计数相关逻辑。体量：background.js 1485→976 行，content.js 2313→1788 行。
- 打包：`node extension/scripts/build.mjs --team48-token <令牌> --hme-token <令牌> --hme-account <acc_...>`（或 `--cf-password <密码>` 用 Cloudflare 读码；`--local-config` 复用已有 private-config.mjs 重打包），只用 Node 内置模块，输出 `extension/dist/chatgpt-signup/` 和 ZIP。Gemini 用假令牌打了三种包并核对后删除了产物。
- 检查：`bash scripts/check.sh ext` 通过（Claude 复跑）；搜不到 `/api/ext/workspaces`、`/handoff`、`/phone`、`/resolve`、`RUNNER`、`phonePool`。
- 需要用户手动验证：切换上线后用真实令牌打包、Chrome 重新加载插件（开无痕权限），跑一个号的单次或批量注册，确认直接接入 rs。
- ship 时写进文档：插件用法见 `extension/README.md`；老仓库 PRODUCT.md 的"插件接入 / 批量注册"内容并入新仓库 PRODUCT.md（接码一节删除）。

