# Part 3：看板前端

**状态**：已完成
**依赖**：part-0 已完成
**所属计划**：[README](./README.md)

## 目标

在 `apps/dashboard/web/` 用 Vue 3 + Vite + TypeScript 做出看板界面：登录页、团队看板、团队详情、邀请 / 撤回 / 踢人、母号授权、改到期日和团队名。按 `docs/contracts/dashboard-api.md` 调接口；后端还没好时用本地假数据开发。

## 可改文件

- `team48/apps/dashboard/web/**`（`dist/` 是构建产物，不手改；`src/api/types.ts` 如与合约不符，以合约为准修正）

## 只读参考

- 新仓库 `docs/contracts/dashboard-api.md`（唯一接口依据）
- 老仓库 `C:\Projects\VPS\天理云-美西国际大宽带-8H16G\48team-manager\DESIGN.md`（配色、字号、状态写法，可沿用其简约风格）
- rs 前端 `..\codex-proxy-rs\frontend\src\`：周限额度条的样式（使用率 %、进度条），参考写法自己实现，不跨仓库引用文件
- 用户截图的 rs 额度条样式：大号数字 + "使用率 25%" + 细进度条

## 要点

- **看板**：每个团队一张卡：团队名、席位 `已用 / 上限`、到期剩余天数（≤ 7 天黄色、已过期红色，未填显示"未填到期日"）、母号邮箱和授权状态（需重新授权时醒目）、上次同步时间和同步失败原因。卡内列出成员和待接受邀请。
- **成员行**：邮箱、角色、席位类型；rs 状态：`matched` 显示周限额度条（使用率 %、重置时间悬停看）；有 5 小时窗口时额外显示一条 5 小时额度条（如实显示，不加"建议踢掉"之类标注）；`not_in_rs` 显示"未接入 rs"；rs 数据旧时整块标"旧"，不显示成 0%；rs 停用 / 出错显示其状态。
- **不做**：任何"推荐替换 / 该换谁"标注、今日切换、收入、购买信息。
- **写操作**：邀请（输入邮箱 + 选 Standard / Premium）、撤回邀请、踢人、删除本地团队都弹二次确认，写明"会改官方团队"或"只删本地记录"；提交中按钮禁用防连点；返回 `write_outcome_unknown` 时提示"结果未知，已回读名单"并展示核对结果。
- **同步**：每个团队卡一个"同步"按钮，另有"全部同步"（前端逐个串行调用）；不自动同步。看板数据（含 rs 额度）每 60 秒自动刷新一次 `GET /api/board`，刷新时不得打断已打开的弹框和输入。
- **母号授权**：添加团队 / 重新授权母号：输入邮箱（新母号可填代理地址）→ 显示授权链接（可复制）→ 粘贴回调地址 → 提交后展示新增 / 更新的团队。
- 时间显示相对时间，悬停显示北京时间；界面简体中文；桌面优先，手机宽度能看不塌。
- 不引入 UI 组件库；样式用原生 CSS（CSS 变量做主题，支持深浅色跟随系统）。

## 步骤

1. 按合约建 API 封装（含 401 跳登录）和假数据开关。
2. 做登录页、看板、团队卡、成员行、额度条组件。
3. 做各写操作弹框和母号授权流程。
4. 跑 `npm run build`；跑检查。

## 完成标准

- [x] 用假数据能完整走一遍：登录、看板、邀请 / 撤回 / 踢人确认框、母号授权三步
- [x] 额度条在 matched / not_in_rs / 旧数据 / 有 5 小时窗口四种情况下显示正确
- [x] 基本检查通过：`bash scripts/check.sh`

## 完成记录

- 由 Gemini 施工、Claude 验收。只改了 `apps/dashboard/web/**`，未新增依赖。
- 结构：`src/api/client.ts`（fetch 封装，401 统一回登录页，网络错误转成 `network_error` 失败体）、`src/api/mock.ts`（假数据）、`src/api/types.ts`（补 `OkOnly`）；`src/components/` 下 LoginView、TeamCard、TeamDetailDrawer、MemberRow、QuotaBar、InviteModal、ConfirmActionModal（含 `write_outcome_unknown` 回读核对展示）、EditTeamModal、MotherAuthModal（三步）、ModalDialog；`src/styles/tokens.css`（CSS 变量，深浅色跟随系统，可手动切换）、`base.css`；`src/utils/time.ts`（相对时间 + 悬停北京时间）、`format.ts`。
- 额外做的：顶栏汇总（团队数、总席位、需关注数）、搜索 / "只看需关注"筛选；60 秒静默刷新不关闭弹框、全部同步期间跳过刷新。
- 假数据：`npm --prefix apps/dashboard/web run dev` 后打开 `http://localhost:5173/?mock=1`（或 `VITE_MOCK=1`）；任意账号密码登录（密码 `wrong` 测报错），邮箱含 `unknown` 触发"结果未知"面板，顶栏可切换模拟 rs 旧数据。生产包里也带假数据代码，只有带 `?mock=1` 才启用，不碰真实接口。
- 修了 part-0 脚手架的问题：`tsconfig.app.json` 缺 `noEmit`，`vue-tsc -b` 会把 `.js` 编译残留写进 `src/`，已补上并删掉残留。
- 额度条：rs 不给重置时间（见 part-2），悬停显示"重置时间：未知"。
- 检查：`bash scripts/check.sh web` 通过（Claude 复跑）。

