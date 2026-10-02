---
name: 48 Team Manager
description: 自用 ChatGPT Team 运营控制台：平涂、直角、钱绿是唯一强调色、异常优先
---

# 设计规范：48 Team Manager

参照 sub2api 的"钱是那块颜色"规范，按本项目（数据密集的运维后台）改造。所有颜色、字号、间距都写成 CSS 变量，定义在 `app/web/static/css/tokens.css`；组件 CSS 只引用变量，不写死颜色。

## 设计原则

**平涂、直角、钱绿是唯一强调色、异常优先。**

- 容器是一块平涂色块，没有圆角、阴影、渐变、毛玻璃；层次只靠底色深浅：页面底 → tile → tile-2。
- 全站只有钱绿一种饱和色，用在主按钮、当前导航、选中状态、焦点环。
- 一眼先看到异常：危险 / 警告用文字色 + 符号表达，不刷大色块。
- 默认深色（纯黑底），可切浅色 / 跟随系统；两套主题都要检查。
- 技术约束：Jinja2 + 原生 JS / CSS，不引入框架、UI 库、图标库或构建链。

## 配色

保留变量名，只换值。中性灰都略带绿调，不混入 slate 蓝灰。

| 角色 | 变量 | 浅色 | 深色（默认） |
|---|---|---|---|
| 页面底、侧栏 | `--bg` `--sidebar` | `#FFFFFF` | `#000000` |
| tile：卡片 / 面板 / 次按钮 | `--surface` `--surface-subtle` | `#F1F2F0` | `#141514` |
| tile-2：悬停 / 卡内二级块 / 标签底 | `--surface-muted` `--surface-hover` | `#E6E8E4` | `#1D1F1D` |
| tile-3：tile-2 上再悬停 | `--surface-strong` | `#D2D5D0` | `#2A2D2A` |
| 浮层底（抽屉 / 确认框 / 下拉 / 任务中心） | `--surface-overlay` | = 页面底 | = 页面底 |
| 正文 | `--text` | `#0A0B0A` | `#FFFFFF` |
| 次要文字 | `--text-secondary` | `#464A46` | `#B9BEB9` |
| 说明、表头 | `--muted` | `#6A6F6A` | `#8D938D` |
| 细线 | `--border` `--border-subtle` | `#DFE1DD` | `#262826` |
| 输入框边框 | `--border-strong` | `#A3A8A2` | `#464A46` |
| 钱绿实心（主按钮底、焦点环） | `--accent` / `--accent-hover` | `#00703C` / `#005C31` | `#00873F` / `#00A14C` |
| 钱绿文字（当前导航、链接） | `--accent-text` | `#00703C` | `#00A14C` |
| 成功 / 警告 / 危险 文字 | `--success-text` `--warning-text` `--danger-text` | `#00703C` / `#A24B00` / `#B4002A` | `#34D17A` / `#FFA53A` / `#FF6B85` |
| 危险按钮实心底 | `--danger-solid` / `-hover` | `#B4002A` / `#8F0021` | `#B4002A` / `#D0103F` |
| 遮罩 | `--overlay` | `rgba(0,0,0,.45)` | `rgba(0,0,0,.6)` |

- `--primary-text`（主按钮文字）两种主题都是白色。
- `--success-*` / `--warning-*` / `--danger-*` / `--accent-*` 的 `-bg` / `-border` 是弱化版（浅色很淡的底，深色 15% / 35% 透明度），只给少数必须铺底的地方用（如提示条、选中行）。默认状态展示不铺底。
- 次按钮底色走 `--button-bg` / `--button-hover-bg`（默认 tile / tile-2）。按钮放在 tile 卡片里时，容器把它们改成 `--surface-muted` / `--surface-strong`，保证按钮和卡片分得开。

## 字体

- 拉丁字符：Anthropic Sans（族名 `"Team48 Sans"`）/ Anthropic Mono（族名 `"Team48 Mono"`），文件在 `app/web/static/fonts/`，本机装了就优先用本机。Sans 是 400 单字重，粗体由浏览器合成；Mono 可变 300–800。
- 中文回退：`"PingFang SC", "Hiragino Sans GB", "Microsoft YaHei"`。
- 全站数字 `font-variant-numeric: tabular-nums`；数字列右对齐。
- Mono 只用于邮箱、ID、令牌、日志这类标识符，不当装饰。

## 字号

| 变量 | 大小 | 字重 | 用途 |
|---|---|---|---|
| `--text-meta` | 12px | 400 | 表头、标签、提示、说明（下限） |
| `--text-body` | 14px | 400 | 正文、表格、按钮（600）、输入框 |
| `--text-heading` | 16px | 600 | 面板 / 抽屉标题 |
| `--text-title` | 28px | 400，字距 -0.02em | 页面 h1 |
| `--text-figure` | 28px | 400 | 汇总数字 |

- 粗体只用在 16px 及以下；大字靠字号不靠粗细。
- 中文不做大写变换、不加字距；标题上方不加"眉题"小字。

## 形状与层次

- 圆角：全部 0。`--radius-sm: 2px` 只给复选框、进度条；`--radius-pill` 只给圆点、开关、加载圈。其余 `--radius-*` 都是 0。
- 阴影：`--shadow-xs/sm/md` 都是 `none`。浮层用 `--shadow-overlay`（= `0 0 0 1px var(--border)`，即 1px 细线外框）。
- 卡片不加边框、不嵌套卡片；卡内需要再分块用 tile-2 或 1px 细线。

## 间距

| 用途 | 值 | 变量 |
|---|---|---|
| 卡片之间 | 8px | `--tile-gap` |
| 卡片内边距 | 16–20px | `--tile-pad`（16px 20px） |
| 区块之间 | 24–32px | `--space-lg` / `--section-gap` |
| 控件高度 | 36px（小号 28px） | `--control-h` / `--control-h-sm` |

一切左对齐。响应式断点只用 1280 / 1024 / 720；≤1024 侧栏变抽屉。

## 组件

### 侧栏
- 与页面同底色、无右边框，宽 240px（`--sidebar-w`，各页不得另行覆盖）。
- 导航项 14px 次要色，悬停只变正文色、不铺底。
- 当前项：钱绿文字 + 字重 600 + 左侧 8×8 实心方块（`.nav-link.is-active::before`）。
- 分组标签（"资源"）12px 说明色，不大写。

### 顶栏
- 无底色差、无下边框。左侧 h1 用 `--text-title`，副标题 14px 次要色；右侧主题选择、"任务"等按钮统一按钮样式。
- 手机端 h1 降到 22px，副标题隐藏。

### 按钮（`.button`）
- 直角，高 36px，左右 16px，字重 600，无边框。
- `primary`：钱绿底白字，悬停换 `--accent-hover`。每个区域最多一个。
- 默认（secondary）：tile 底正文色，悬停 tile-2。
- `ghost`：透明底次要色，悬停 tile 底正文色。
- `danger`：危险实心底白字，只用于删除、移出这类不可逆操作；`ghost danger`（行内小按钮）只用危险色文字。
- 悬停只换底色（150ms）；按下 `scale(.985)`；禁用 50% 透明度。

### 输入框 / select / textarea
- 直角、1px `--border-strong` 边框、页面底色、占位符说明色。
- 聚焦：边框变钱绿，不加光晕。出错（`aria-invalid="true"`）：边框变危险色，说明文字用 `.error`（危险色 + "✕"）。
- 禁用：tile 底、说明色文字。
- 复选框 / 单选用 `accent-color: var(--accent)`。

### 焦点
- 全站唯一一套：2px 钱绿 outline，偏移 2px（视觉等同 `--focus-ring`），定义在 `app.css`，组件不各自叠加焦点环。

### 卡片 / 面板
- tile 底，无边框、无阴影、无圆角，内边距 16–20px；卡片间 8px 缝隙。
- 面板标题 `--text-heading`；标题与内容之间可用 1px 细线分隔。
- 卡里不再套带边框的表格或卡片。

### 表格
- 表头：12px 说明色、字重 400、无底色、不大写，下边 1px 细线。
- 单元格：14px，行间 1px 细线，无斑马纹；行悬停 tile-2。
- 数字列右对齐、等宽；邮箱 / ID 可用 Mono。

### 状态的两种写法（全站只用这两种）
1. **状态**：符号 + 文字，只给符号和文字上状态色，**不铺底、不加边框**。符号约定：`●` 正常 / 进行中，`▲` 警告，`✕` 失败 / 危险，`○` 未知 / 停用；也可用现有 `::before` 小方块或圆点。适用 `.status`、`.badge-*`、`.tone-*`、`.management-badge`、`.task-progress-badge` 等。
2. **标签 / 计数**：tile-2 底、直角、12px、次要色文字，左右 6–8px，不带状态色。适用 `.metric-chip`、`.management-chip`、`.board-switch` 等中性信息。选中 / 激活的标签可改成钱绿文字，仍不铺绿底。

- 异常要显眼但不刷大色块：数字 / 文字用危险或警告色 + 符号，不整格、整行染色。
- 状态不能只靠颜色：必须有符号或文字。

### 抽屉 / 确认框 / 下拉 / 任务中心 / 命令面板
- 底色 `--surface-overlay`（页面底），1px `--border` 外框（`--shadow-overlay`），直角，无投影。
- 遮罩 `--overlay`。头部 / 底部与内容之间用 1px 细线。
- 危险操作的确认框：确认按钮用 `danger`，正文写明是否会改官方 Team。

### 提示条（toast / 页面反馈）
- 平涂块：默认 tile 底；需要强调时用对应 `-bg` 弱化底 + 状态色符号和文字。不加左侧彩条、不加阴影。

### 空状态
- 一行说明色文字说明"为什么空、下一步做什么"，可附一个按钮；不放插图。

## 动效

- 只有悬停、展开 / 收起、抽屉滑入这类必要过渡，150–200ms，`ease` 或 `cubic-bezier(.16,1,.3,1)`。
- 无入场动画（看板 30 秒刷新会反复播放）。
- 尊重 `prefers-reduced-motion`：关闭过渡和按下缩放。

## 浏览器自带部分

- 文字选中：钱绿底白字；输入光标钱绿。
- 滚动条：8px 细条，`--border-strong` 色，无轨道。
