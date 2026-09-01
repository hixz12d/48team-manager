# 48 Team Manager UI 优化实施规格

> 仓库：`hixz12d/48team-manager`  
> 审阅基线：`main@e67e25288231fa4e03e08a61644c303b0199667d`  
> 审阅日期：2026-09-01  
> 依据：当前仓库代码、设置页截图、现有 v3 重构规格、成熟运维后台与设计系统  
> 文档目标：给后续人工修改或 Codex/AI 修改提供一份**可以直接执行、可以验收、不会顺手重写业务逻辑**的 UI 规格

---

## 0. 最终结论

当前 UI 的大方向是对的，应该保留：

- 中性色、白色内容面、浅灰侧栏。
- 左侧固定导航。
- 细边框、轻圆角、无厚重阴影。
- 紧凑表格，而不是卡片墙。
- Jinja2 + Vanilla JavaScript，不为“现代感”强行引入 React/Vite。
- 正常状态低对比，异常和主操作才获得高对比。
- 任务、账号、工作区详情后置到右侧 Sheet，而不是把所有字段堆在主列表。

当前最需要修的不是“换主题”，而是四件事：

1. **消灭没有语义的空白。**
2. **让所有看起来能用的控件真的能用。**
3. **让关键运营参数在正确层级可见。**
4. **让加载、失败、无数据、未保存等系统状态说真话。**

一句话定义最终体验：

> 像一套克制、可信、异常优先的小型账号运维控制台，而不是一张字段表，也不是一个 AI 风格卡片仪表盘。

---

## 1. 本次审阅范围

### 1.1 当前关键前端文件

```text
app/web/templates/console.html
app/web/static/css/tokens.css
app/web/static/css/app.css
app/web/static/css/components.css
app/web/static/js/app.js
```

### 1.2 与 UI 读取模型直接相关的文件

```text
app/application/queries/console.py
app/application/queries/identity.py
app/application/settings.py
app/web/routes/api.py
app/web/schemas/settings.py
```

### 1.3 当前产品边界

本项目是个人自用的运维控制台，核心任务是：

- 管理 Workspace / Team 母号与子号。
- 查看官方额度。
- 发现授权失效并处理。
- 安全轮转与补位。
- 管理 HME、手机号、代理。
- 与 Sub2API 对接。
- 查看持久化 Operation。

因此，UI 必须优先回答：

- 现在有什么异常？
- 哪些任务正在运行？
- 哪些工作区或账号需要我决定？
- 当前数据来自哪里、多久前更新？
- 这个操作会造成什么后果？

而不是优先回答：

- 数据库里一共有多少字段？
- 每个对象的完整技术 ID 是什么？
- 所有历史功能入口在哪里？

---

## 2. 当前 UI 值得保留的部分

### 2.1 视觉骨架是正确的

现有 Token 已经接近合适的方向：

```css
--bg: #f7f8fa;
--surface: #ffffff;
--sidebar: #f2f3f5;
--border: #e4e4e7;
--text: #18181b;
--muted: #71717a;
--accent: #2563eb;
--radius: 6px;
```

这些不需要推倒重做。建议继续坚持：

- 背景与卡片对比很轻。
- 主内容是视觉中心，侧栏退后。
- Accent 只用于焦点、链接、进度和少量主操作。
- 正常状态不铺绿色背景。
- 危险状态不铺整行红底。
- Panel、Sidebar、Table 不使用悬浮阴影。

### 2.2 页面读取已经按功能拆开

当前不是一个全量 mega-board 首屏，而是按页面调用：

```text
/api/overview
/api/workspaces
/api/accounts
/api/operations
/api/resources/phones
/api/resources/hme
/api/resources/proxies
/api/settings
```

这对后续做分页、筛选、局部刷新是好基础。

### 2.3 使用 AbortController 是正确的

当前 `app.js` 已按实体请求维护 AbortController，可避免旧请求覆盖新请求。这个模式应该保留，并扩展到搜索、筛选、详情 Sheet 和任务摘要。

### 2.4 密钥不直接回显是正确的

“留空不修改”和不返回原始密钥的原则必须保留。后续只需要改进“已配置/未配置”的表达，不应增加查看明文密钥能力。

---

## 3. 截图中大空白的准确原因

### 3.1 不是普通留白，而是 Grid 行高造成的“无主空白”

当前设置页 DOM 顺序是：

```text
1. 对接（很高）
2. 自动化（很矮）
3. 手机号池
4. 后台密码
5. 保存区
```

当前 CSS 是：

```css
.settings-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
```

浏览器自动排布为：

```text
第一行：对接       | 自动化
第二行：手机号池   | 后台密码
第三行：保存区跨两列
```

CSS Grid 的第一行高度由最高的“对接”卡片决定。于是右侧“自动化”卡片下面必须等待第一行结束，形成截图中的巨大空白。

### 3.2 仅增加 `align-items: start` 不能解决

下面这种补丁不够：

```css
.settings-grid {
  align-items: start;
}
```

它只阻止矮卡片被拉伸，不会改变第一行的行高。右侧下一张卡依旧要等到左侧高卡结束后才能进入第二行。

### 3.3 正确修法：两个独立纵向栈

设置页应该是两个独立 column stack，而不是一个四卡片自动排布 Grid。

推荐结构：

```html
<form id="settings-form" class="settings-page">
  <div class="settings-columns">
    <div class="settings-column settings-column-main">
      <!-- Sub2API -->
      <!-- HME -->
      <!-- 临时邮箱 -->
    </div>

    <div class="settings-column settings-column-side">
      <!-- 自动化 -->
      <!-- 手机号池 -->
    </div>
  </div>

  <div class="settings-savebar">
    <!-- 未保存提示 / 保存状态 / 保存按钮 -->
  </div>
</form>

<form id="password-form" class="settings-security">
  <!-- 后台密码：独立提交 -->
</form>
```

推荐 CSS：

```css
.settings-page,
.settings-security {
  width: min(100%, 1200px);
  margin-inline: auto;
}

.settings-columns {
  display: grid;
  grid-template-columns:
    minmax(0, 1.18fr)
    minmax(320px, 0.82fr);
  gap: 16px;
  align-items: start;
}

.settings-column {
  display: grid;
  gap: 16px;
  align-content: start;
}

@media (max-width: 1040px) {
  .settings-columns {
    grid-template-columns: 1fr;
  }
}
```

效果：

```text
┌─────────────────────────────┐  ┌──────────────────────┐
│ Sub2API                     │  │ 自动化               │
└─────────────────────────────┘  └──────────────────────┘
┌─────────────────────────────┐  ┌──────────────────────┐
│ HME                         │  │ 手机号池             │
└─────────────────────────────┘  └──────────────────────┘
┌─────────────────────────────┐
│ 临时邮箱                    │
└─────────────────────────────┘

──────────── sticky 保存区 ────────────

┌─────────────────────────────────────┐
│ 后台账号与密码                      │
└─────────────────────────────────────┘
```

右侧卡片会自然贴着排列，不再被左侧高度绑住。

### 3.4 可接受的临时补丁

在不改 HTML 结构的情况下，可以用显式 Grid Area 暂时消除空白：

```css
.settings-grid {
  grid-template-columns: minmax(0, 1.18fr) minmax(320px, 0.82fr);
  grid-template-areas:
    "connections automation"
    "connections phone"
    "connections security"
    "actions actions";
  align-items: start;
}

.settings-grid > :nth-child(1) { grid-area: connections; }
.settings-grid > :nth-child(2) { grid-area: automation; }
.settings-grid > :nth-child(3) { grid-area: phone; }
.settings-grid > :nth-child(4) { grid-area: security; }
.settings-grid > :nth-child(5) { grid-area: actions; }
```

这能快速修截图问题，但不推荐长期依赖 `nth-child`。最终还是应该改为有语义的 wrapper 和 class。

---

## 4. 当前代码审计：优先问题清单

| 优先级 | 问题 | 当前表现 | 用户影响 | 建议 |
|---|---|---|---|---|
| P0 | 设置页 Grid 形成大空白 | 右侧自动化下方空出半屏以上 | 视觉失衡，像页面没做完 | 改成双独立纵向栈 |
| P0 | 搜索框看起来可用但没有绑定行为 | 团队、账号页都有搜索输入 | 用户输入后无反应，降低信任 | 立即实现或暂时隐藏 |
| P0 | 任务抽屉打开后没有填充数据 | JS 只切换 `hidden` | “任务”按钮像坏了 | 实现摘要渲染或先移除按钮 |
| P0 | 请求失败只写 `console.warn` | 页面没有可见错误 | 用户分不清无数据与加载失败 | 加页面级错误状态与重试 |
| P0 | 初始文案先显示“还没有数据” | 请求完成后才替换 | 慢请求时产生误导 | 初始状态改为“正在加载” |
| P0 | 保存时没有禁用按钮 | 可重复提交 | 可能重复请求，不知道是否保存中 | 保存期间禁用并显示状态 |
| P1 | 总览 API 返回很多数据，但 UI 只画 `attention` | workspaces、accounts、active operations、recent events 未使用 | 首页不能承担运营总览 | 增加摘要条、运行任务、工作区健康 |
| P1 | 设置页所有集成都挤在一个“大对接”卡片 | 10 个字段连续排列 | 扫描困难，服务边界不清 | 拆成 Sub2API/HME/临时邮箱三块 |
| P1 | 掩码无法表达“已存还是没存” | `_secret_view()` 总是返回圆点 | 空密钥也可能看起来已配置 | 返回 `configured` 布尔状态，不返回假掩码 |
| P1 | 页面副标题全部相同 | 每页都是“先看本地状态……” | 不帮助理解当前任务 | 按页面配置说明 |
| P1 | 侧栏“服务正常”是静态文案 | 无真实健康检查支撑 | 可能作出错误承诺 | 接真实状态，或改为“控制台已连接” |
| P1 | 工作区只显示成员数，不显示席位上限 | API 已有 `seat_limit` | 无法快速判断满员/空席 | 显示 `members / seat_limit` |
| P1 | 账号页隐藏了已有关键字段 | API 有 5h/7d、官方计划、identity/reasons | 关键判断需要跳到别处或无法完成 | 主表显示摘要，详情 Sheet 显示技术信息 |
| P1 | 日期与更新时间是原始字符串 | 不易快速理解新旧程度 | 无法判断数据是否过期 | 显示相对时间，tooltip 给精确时间 |
| P2 | 无实体详情 Sheet | 主表只能读，不能深入 | 主表要么信息不足，要么未来越堆越宽 | 账号/工作区/任务统一右侧 Sheet |
| P2 | 资源拆成三条一级导航 | 手机、HME、代理分散 | 导航略碎 | 合并“资源”页，用 Tabs |
| P2 | 命令菜单只支持静态页面跳转 | 不能搜实体 | 快捷入口价值低 | 后续支持账号/工作区/任务搜索 |
| P2 | 无窄屏方案 | 两列设置、宽表格可能溢出 | 笔记本分屏/手机不可用 | 加断点、表格滚动、全屏 Sheet |

---

## 5. 设计原则

### 5.1 空白必须有作用

保留以下空白：

- 页面标题与内容之间的间隔。
- 不同设置组之间的间隔。
- 表格工具栏与表头之间的层级。
- 危险操作与普通操作之间的分隔。
- 可读文本行宽限制。

删除以下空白：

- Grid 行高造成的无内容区域。
- 空卡片为了“对齐”被强行拉高。
- 只有一句话却占据半屏的 Panel。
- 没有内容、没有状态说明、没有行动入口的空容器。

判断标准：

> 用户能否说出这块空白在分隔什么？说不出来，就应删除或填入有意义的信息。

### 5.2 视觉重量必须由重要性获得

- 页面名称：清晰但不夸张。
- 当前异常：高对比。
- 正在运行：中高对比。
- 正常状态：普通文字或小圆点。
- 技术 ID：低对比，只在详情出现。
- 次要解释：muted，但对比度仍要可读。
- 主操作：每页最多一个。
- 危险操作：不常驻主页面，进入菜单或确认流程。

### 5.3 数据要同时说明来源与新鲜度

类似“额度 73%”是不完整的，应表达为：

```text
官方 7d  73% · 3 分钟前
```

类似“Sub2API 已验证”应表达为：

```text
Sub2API · 已验证 · 12 分钟前
```

页面必须区分：

- 本地状态。
- OpenAI 官方快照。
- Sub2API 状态。
- HME 对账状态。
- 最近一次检测/同步时间。

### 5.4 通过渐进披露控制复杂度

主页面显示判断所需的摘要；技术细节进入详情 Sheet；极少修改的密钥字段进入编辑模式。

不要把以下内容长期暴露在主列表：

- 完整 UUID。
- 原始错误 JSON。
- Token 或密钥掩码串。
- 详细步骤日志。
- 历史 Membership。
- 所有候选操作按钮。

### 5.5 看起来能点，就必须能点

第一阶段必须执行一个硬规则：

> 未实现的搜索、抽屉、菜单和按钮，不得以正常可交互样式出现在页面上。

可选处理：

- 实现。
- 禁用并写明原因。
- 先隐藏。
- 替换为纯文本状态。

不能继续保留“点了无事发生”的控件。

---

## 6. 推荐信息架构

### 6.1 最终导航

```text
概览
工作区
账号池
任务
资源
设置
```

资源页内：

```text
手机号 | HME | 代理
```

### 6.2 第一阶段可以先保留当前导航

为了控制改动范围，P0 阶段可以暂时保留：

```text
总览
团队
账号
任务
手机号
HME
代理
设置
```

先修真实性、布局和关键数据；之后再合并资源导航。不要在同一个提交里同时改 IA、数据合同和自动化逻辑。

### 6.3 页面统一结构

```text
Topbar
  页面名称
  页面专属的一行说明
  全局任务徽标 / 页面唯一主操作

Page
  可选摘要条
  Toolbar
  主列表 / 设置内容
  右侧 Sheet（按需）
```

---

## 7. 设置页完整重构规格

设置页是当前截图问题最明显、也最适合先做的一页。

## 7.1 页面目标

用户进入设置页后，应在 10 秒内知道：

- 哪些服务已配置。
- 哪些连接正常。
- 最近检测是什么时候。
- 自动化目前开了什么。
- 手机号池的关键阈值是什么。
- 页面是否存在未保存修改。
- 修改密钥是否会覆盖旧值。

## 7.2 页面标题与说明

推荐：

```text
设置
管理外部服务、自动化开关和资源策略。检测使用当前输入值，不会自动保存。
```

不要继续使用所有页面共享的：

```text
先看本地状态，不会主动打 OpenAI。
```

## 7.3 集成不要再放在一个巨型卡片里

拆成三张独立卡片：

### A. Sub2API

卡片头部：

```text
Sub2API
已配置 · 未检测
[检测] [编辑]
```

检测成功后：

```text
Sub2API
连接正常 · 刚刚
3 个分组 · 42 个账号
[重新检测] [编辑]
```

编辑字段：

- 地址。
- 认证方式摘要。
- API Key。
- 管理员邮箱。
- 管理员密码。

建议交互：

- 默认只显示地址、认证状态和检测摘要。
- 点击“编辑”后展开字段。
- Secret 输入框默认为空。
- Placeholder 使用：
  - `已保存，留空则不修改`
  - `尚未设置`
- 不把 `••••••` 填进 input value。
- 不提供“显示原密钥”。

### B. iCloud HME

卡片头部：

```text
iCloud HME
连接正常 · 2 分钟前
当前账号 · 37 个别名 · 8 个可用
[重新检测] [编辑]
```

字段：

- HME 地址。
- Token。
- 账号 ID。

关键参数：

- 总别名数。
- 启用数。
- 未占用数。
- 当前 Account。
- 最近一次检测时间。

### C. 临时邮箱

卡片头部：

```text
临时邮箱
连接正常 · 2 分钟前
hixz12@...
[重新检测] [编辑]
```

字段：

- API 地址。
- 邮箱地址。
- 管理员密码。

检测结果不要只写“能读到信”，还应明确：

- 连接成功。
- 当前邮箱。
- 最近检测时间。
- 是否读到样本消息可作为次要信息。

## 7.4 检测方式

当前接口一次检测 Sub2API、HME、临时邮箱。UI 可分两阶段实现。

### 阶段一：不改 API

- 每张卡都有“检测”按钮。
- 点击任意卡时，仍调用当前总检测接口。
- 只在对应卡显示其结果。
- 页面提供次要按钮“全部检测”。

### 阶段二：拆分 API

```text
POST /api/settings/probe/sub2api
POST /api/settings/probe/hme
POST /api/settings/probe/mail
```

好处：

- 某个服务慢或失败，不阻塞其他服务。
- 每张卡拥有独立 loading/error 状态。
- 用户修改一个服务时不必触发三个外部请求。
- 结果更容易记录时间与缓存。

### 检测状态文案

```text
未检测
检测中…
连接正常
认证失败
地址不可达
配置不完整
检测超时
```

不要只用红绿边框。必须有文字。

## 7.5 自动化卡片

当前只有一个 checkbox，不应让它独占一整块巨大 Panel。

建议改为设置行：

```text
官方额度探测                         [开关]
定时读取账号的官方 5h / 7d 用量。
最近成功：3 分钟前 · 下次预计：12 分钟后
```

第一阶段只有一个开关时：

- Panel 内容高度控制在 100～140px。
- 写清影响。
- 写清默认值。
- 有最近执行状态就显示；没有数据时写“尚无执行记录”。
- 不为了填满空间添加无效开关。

未来加入自动重授权、自动轮转时，每个开关都必须有：

- 功能名称。
- 影响说明。
- 当前状态。
- 依赖/阻断原因。
- 最近执行。
- 默认是否关闭。

“强制补位”不得成为持久化全局开关。

## 7.6 手机号池

当前字段是秒数，机器可读但不够人性化。

推荐 UI：

```text
每个号码最多使用                    [ 3 ] 次
失败后冷却                          [ 10 ] 分钟
任务占用保留                        [ 15 ] 分钟
```

前端负责分钟与秒的换算，后端可以继续存秒。

每个字段加短说明：

```text
冷却期间不会分配给新的注册或重授权任务。
```

输入要求：

- 使用明确单位后缀。
- 禁止仅靠 placeholder 表达单位。
- 超界时在字段旁显示错误。
- 保存失败时顶部错误摘要链接到具体字段。

## 7.7 后台密码必须独立

当前密码修改与所有设置共用一个表单和一个“保存设置”按钮，这会混淆风险边界。

推荐独立区域：

```text
后台账号
当前登录账号：hixz12

修改密码
当前密码
新密码
确认新密码
[修改密码]
```

要求：

- 独立 `<form>`。
- 独立提交。
- 不因修改普通设置而提交空密码字段。
- 成功后清空输入。
- 错误定位到具体字段。
- 可以增加密码要求说明，但不要做夸张强度动画。

## 7.8 Sticky 保存区

设置页底部使用 sticky save bar：

```text
未保存 3 项修改                       [放弃修改] [保存设置]
```

保存时：

```text
正在保存…                              [保存中]
```

成功后：

```text
已保存 · 刚刚                          [保存设置]
```

失败时：

```text
有 2 项未能保存，请检查上方错误         [重试]
```

要求：

- 只有表单变脏时才强调保存。
- 保存期间禁用重复提交。
- 成功后更新基线值，恢复 clean。
- 离开页面时有未保存内容才提示。
- 检测连接不应把表单标为已保存。
- 不混用“部分字段自动保存、部分字段手动保存”。

## 7.9 设置页推荐线框

```text
设置
管理外部服务、自动化开关和资源策略。检测使用当前输入值，不会自动保存。

┌──────────────────────────────────┐  ┌────────────────────────────┐
│ Sub2API                 连接正常 │  │ 自动化                     │
│ 3 个分组 · 42 个账号 · 刚刚     │  │ 官方额度探测          [ON] │
│                       检测  编辑 │  │ 最近成功：3 分钟前         │
└──────────────────────────────────┘  └────────────────────────────┘

┌──────────────────────────────────┐  ┌────────────────────────────┐
│ iCloud HME              连接正常 │  │ 手机号池                   │
│ 37 个别名 · 8 个可用 · 2 分钟前 │  │ 最大使用        3 次       │
│                       检测  编辑 │  │ 冷却           10 分钟     │
└──────────────────────────────────┘  │ 占用保留       15 分钟     │
                                      └────────────────────────────┘
┌──────────────────────────────────┐
│ 临时邮箱                连接正常 │
│ hixz12@... · 2 分钟前            │
│                       检测  编辑 │
└──────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│ 未保存 2 项修改                              放弃修改  保存设置 │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│ 后台账号                                                       │
│ 当前账号 hixz12                                                │
│ 当前密码  新密码  确认新密码                      修改密码     │
└──────────────────────────────────────────────────────────────────┘
```

---

## 8. 总览页规格

当前 `/api/overview` 已经返回：

- `attention`
- `running_operations`
- `recent_events`
- `healthy`
- `identity`
- `workspaces`
- `accounts`

但前端只使用 `attention`。这会浪费已经存在的读取模型。

## 8.1 Summary Strip

不要做五张大 KPI 卡。做一条紧凑摘要：

```text
工作区 6    账号 31    待处理 3    运行任务 1    身份冲突 1
```

样式：

- 一条 Panel 或无卡片的 summary bar。
- 每项 1 行或 2 行。
- 数字使用 `font-variant-numeric: tabular-nums`。
- 正常值不染色。
- “待处理”有数量时使用 warning/danger 文字。
- 点击摘要可跳到带筛选的页面。

## 8.2 需要处理

按风险排序：

1. 身份冲突、Binding conflict、Workspace UUID 错位。
2. deactivated 或明确封禁。
3. 需要人工授权。
4. 失败或 `manual_required` 的 Operation。
5. 7d 满且满足轮转条件。
6. vacancy 不安全。
7. HME、号码或代理资源异常。

每条只显示：

```text
对象
一句原因
工作区或来源
一个行动词
```

示例：

```text
alice@example.com
Sub2API Binding 与本地邮箱不一致
Team 2026.08                              查看
```

不要在一条异常里铺“重试、授权、踢出、删除、忽略”等多个按钮。

## 8.3 正在运行

最多 4 条：

```text
状态 · 操作类型 · 对象 · 当前步骤 · 更新时间
```

示例：

```text
进行中  重新授权  alice@...  等待邮箱验证码  8 秒前
```

- 有 active Operation 时 5～8 秒刷新摘要。
- 页面不可见或没有 active Operation 时停止高频轮询。
- 点击打开任务 Sheet。
- 不在首页展示所有历史成功任务。

## 8.4 工作区健康

显示 6～8 个最值得关注的 Workspace：

```text
Team A   4/5 席   正常
Team B   5/5 席   母号需授权
Team C   3/5 席   vacancy 未确认
```

不要用巨大绿色“健康”标签。正常状态用小圆点和普通文字。

## 8.5 首页空状态

若完全正常：

```text
当前没有需要人工处理的事项
6 个工作区运行正常，最近一次官方额度探测在 3 分钟前。
```

仍应保留：

- 摘要条。
- 最近运行任务（若有）。
- 工作区健康。
- 数据更新时间。

不能只剩一块大白板和“暂时没什么异常”。

---

## 9. 工作区页规格

## 9.1 Toolbar

```text
[搜索名称、母号或 Workspace ID] [健康状态] [席位状态] [自动化状态]   共 6 个
```

要求：

- 搜索框必须真实工作。
- 搜索、筛选写入 URL query。
- 刷新页面后保持。
- “清除筛选”明确可见。
- 显示结果数。
- 搜索中有 loading 状态。
- 无结果与无数据使用不同文案。

## 9.2 主表列

推荐：

| 列 | 展示 |
|---|---|
| 工作区 | 名称；次行可显示缩短后的官方 ID |
| 母号 | 邮箱 |
| 席位 | `当前占用 / 上限` |
| 健康 | 正常、需授权、身份冲突、空席、账单不清 |
| 自动化 | 全局策略、暂停、等待安全门闩 |
| 官方 7d | 峰值或母号摘要；没有可靠数据时不要假装有 |
| 最近同步 | 相对时间 |
| 操作 | `…` |

当前 API 已有 `members` 与 `seat_limit`，应至少先改成：

```text
4 / 5
```

而不是只显示：

```text
4
```

## 9.3 行行为

- 点击行打开 Workspace Sheet。
- 邮箱文本仍可选择复制。
- 行本身可键盘聚焦。
- `Enter` 打开详情。
- 最右仅常驻一个 `…`。
- 危险动作放到菜单底部，并用分隔线隔开。

## 9.4 Workspace Sheet

```text
运行摘要
  健康
  自动化策略
  席位
  官方 Workspace ID
  最近同步

成员
  当前 joined / invited

本地纳管
  Account 与 Membership

Vacancy
  官方占用
  本地占用
  账单回执
  是否允许补位
  阻断原因

最近任务

操作
  同步成员
  邀请子号
  请求轮转
  暂停自动化
```

危险动作确认必须写清：

- 踢出后账号进入 standby，不删除。
- 是否会从 Sub2API 下架。
- 是否会尝试补位。
- vacancy 是否通过。
- 若失败，账号与工作区会处于什么状态。

---

## 10. 账号池页规格

## 10.1 主表列

| 列 | 展示 |
|---|---|
| 账号 | 邮箱；次行显示官方角色或计划摘要 |
| 工作区 | 当前 Workspace |
| 本地用途 | 母号、子号、待命、停用 |
| 授权 | 正常、需刷新、需 OAuth、需人工 |
| 官方额度 | `5h / 7d` 紧凑摘要 |
| Sub2API | 已验证、待确认、冲突、缺失、未绑定 |
| 运行状态 | 在用、待命、停用、归档 |
| 操作 | `…` |

推荐账号单元格：

```text
alice@example.com
owner · Pro
```

推荐额度单元格：

```text
5h  21%
7d  73% · 3分钟前
```

不要一开始做复杂环形图。两行文字或极细进度条已经足够。

## 10.2 筛选

```text
用途
Membership
授权
官方额度
Binding
运行状态
```

现有的一个 purpose select 可以保留为第一阶段，但搜索必须绑定，筛选要进入 URL。

## 10.3 账号 Sheet

只在这里显示：

- Official plan。
- Official user/account ID。
- Workspace UUID。
- Membership role/state。
- Binding remote ID、verified email、last error。
- 5h/7d 用量、reset_at、queried_at。
- AT/RT 是否存在，不显示内容。
- 代理名称、地区、出口 IP，不显示密码。
- Identity audit result 与 reasons。
- 最近 Operation。

操作：

- 刷新官方额度。
- 请求重新授权。
- 修改本地用途。
- 移入 standby。
- 归档。
- 恢复归档。

---

## 11. 任务页与任务抽屉规格

## 11.1 当前 P0 问题

当前顶部“任务”按钮会打开抽屉，但 `operations-drawer-body` 没有渲染逻辑。这属于失信控件。

P0 必须二选一：

1. 实现任务摘要。
2. 在实现前隐藏顶部任务按钮。

不能继续保留空抽屉。

## 11.2 抽屉摘要

```text
任务
1 个运行中 · 1 个需人工

进行中
重新授权 alice@...
等待邮箱验证码 · 8 秒前

需人工
轮转 bob@...
代理验证失败 · 12 分钟前

查看全部任务
```

## 11.3 任务主表

| 列 | 展示 |
|---|---|
| 状态 | 排队、运行、等待、完成、失败、需人工 |
| 类型 | 额度刷新、重新授权、拉人、轮转 |
| 对象 | 邮箱 |
| 工作区 | 名称 |
| 当前步骤 | 可读中文 |
| 更新时间 | 相对时间 |
| 操作 | `…` |

不要把所有步骤预加载到列表。详情打开后再请求。

## 11.4 任务 Sheet

- 公共 Operation ID。
- 状态。
- 当前步骤。
- 错误码和安全说明。
- 步骤时间线。
- 最近有限日志。
- 结果摘要。
- 关联资源租约。
- 取消是否允许及原因。

不得显示：

- 原始秘密 input。
- Token。
- 密码。
- 完整代理认证串。
- 未脱敏外部响应。

---

## 12. 资源页规格

最终合并为一个“资源”页面：

```text
手机号 | HME | 代理
```

每个 Tab 使用独立全宽表格和筛选，不做三类卡片墙。

### 手机号

```text
号码
状态
成功次数 / 最大次数
风险次数
当前租约
冷却到
最后结果
```

点击查看 PhoneAttempt 历史。

### HME

```text
别名
本地状态
目标标签
同步状态
任务租约
到期
最后错误
```

冲突项优先。

### 代理

```text
名称
地区
出口 IP
状态
绑定账号数
最后检测
```

密码永不回显。

---

## 13. 真实、可解释的异步状态

## 13.1 必须区分四种状态

### Loading

```text
正在加载账号…
```

可以使用 3～5 行轻量 skeleton。

### Empty

```text
还没有账号
账号导入或创建后会显示在这里。
```

### No results

```text
没有符合当前筛选的账号
清除筛选或换一个关键词。
```

### Error

```text
账号加载失败
请求超时，请重试。                         [重试]
```

这四种状态不能共用“还没有账号”。

## 13.2 页面级错误

当前 `bootPage()` 捕获后只 `console.warn`。应加统一错误容器：

```html
<div class="page-feedback" role="alert" hidden>
  <strong>页面加载失败</strong>
  <span></span>
  <button type="button">重试</button>
</div>
```

错误不要把外部原始 JSON 直接显示给用户。

## 13.3 保存错误

保存失败应同时提供：

- 顶部错误摘要。
- 字段旁错误。
- 保留用户输入。
- 聚焦错误摘要。
- 摘要链接到具体字段。

## 13.4 Toast 的使用范围

Toast 适合：

- 已复制。
- 已保存。
- 请求已创建。
- 已取消。

Toast 不适合：

- 需要用户修正的表单错误。
- 长篇任务失败原因。
- 危险操作确认。
- 页面加载失败的唯一提示。

---

## 14. 搜索与筛选

### 14.1 P0 原则

当前团队页与账号页搜索框存在，但 JS 没有绑定。必须优先处理。

### 14.2 小数据阶段

若数据量很小，可先做客户端过滤：

- 输入 150～250ms debounce。
- 过滤现有行。
- 显示结果数。
- 支持清空。
- 高亮不是必须。

### 14.3 稳定阶段

改为服务端参数：

```text
/api/accounts?q=&purpose=&auth=&binding=&state=
/api/workspaces?q=&health=&seat=&automation=
```

URL 示例：

```text
/accounts?q=gmail&purpose=standby&binding=conflict
```

### 14.4 命令菜单

当前 `Cmd/Ctrl + K` 只有静态导航。最终应支持：

- 搜账号。
- 搜工作区。
- 搜任务。
- 快速跳转。
- 执行低风险动作。

第一阶段不必做复杂命令，但静态导航可以保留。

---

## 15. 页面副标题

在 route 或模板上下文中维护：

```python
PAGE_META = {
    "overview": {
        "title": "总览",
        "subtitle": "优先查看异常、运行中的任务和工作区健康。",
    },
    "workspaces": {
        "title": "工作区",
        "subtitle": "查看席位、母号、自动化策略和最近同步。",
    },
    "accounts": {
        "title": "账号池",
        "subtitle": "按用途、授权、官方额度和 Binding 管理账号。",
    },
    "operations": {
        "title": "任务",
        "subtitle": "查看持久化任务的当前步骤、结果和人工处理项。",
    },
    "phones": {
        "title": "手机号",
        "subtitle": "查看号码余量、冷却、租约和尝试历史。",
    },
    "hme": {
        "title": "HME",
        "subtitle": "查看别名占用、标签同步和任务租约。",
    },
    "proxies": {
        "title": "代理",
        "subtitle": "查看代理地区、出口、绑定和最近检测。",
    },
    "settings": {
        "title": "设置",
        "subtitle": "管理外部服务、自动化开关和资源策略。",
    },
}
```

不要让 JS 在加载后再改标题，避免闪烁。

---

## 16. 状态表达规范

只使用三层：

### 16.1 普通文本 / 小圆点

用于：

- 正常。
- 在用。
- 已验证。
- 已连接。
- 已同步。

### 16.2 轻量 Badge

用于有限枚举：

- 母号。
- 子号。
- 待命。
- 排队。
- 等待。
- 未绑定。

每一行尽量不超过 1～2 个 Badge。

### 16.3 Warning / Danger

用于：

- 身份冲突。
- Binding conflict。
- 失败。
- 需人工。
- deactivated。
- 7d 已满。
- vacancy 不安全。

状态不能只靠颜色。文字必须完整。

推荐 class：

```css
.status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 24px;
  font-size: 12px;
}

.status::before {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  content: "";
  background: var(--muted);
}

.status[data-tone="success"]::before { background: var(--success); }
.status[data-tone="warning"]::before { background: var(--warning); }
.status[data-tone="danger"]::before { background: var(--danger); }
```

---

## 17. 表格规范

### 17.1 尺寸

- 表头：36～40px。
- 行高：44～48px。
- Toolbar：44～48px。
- 左右 padding：12～16px。
- 正文：12～13px。
- 技术 ID：11px monospace，只在详情。

### 17.2 容器

```html
<div class="table-shell">
  <div class="toolbar">...</div>
  <div class="table-scroll">
    <table class="data-table">...</table>
  </div>
</div>
```

```css
.table-scroll {
  overflow: auto;
  overscroll-behavior: contain;
}

.data-table {
  min-width: 760px;
}
```

不要让整个页面横向滚动。

### 17.3 表头

- `th` 使用 `scope="col"`。
- 可排序列使用真实 button。
- 当前排序有文字或 aria 状态。
- 数字列右对齐。
- 时间列固定较窄。
- 操作列固定 40～48px。

### 17.4 行交互

- Hover 只使用极浅背景。
- 选中行使用单一浅背景，不按状态染整行。
- 行可键盘聚焦时要有可见 focus。
- 不要在每行永久摆六个图标按钮。
- 单行操作使用 `…` ActionMenu。

---

## 18. Sheet、Menu 与 Dialog

## 18.1 Sheet

适合：

- 账号详情。
- 工作区详情。
- 任务详情。
- PhoneAttempt 历史。

桌面宽度：

```text
480～540px
```

行为：

- 从右侧覆盖，不永久压缩主内容。
- 同时只打开一个。
- Escape 关闭。
- 关闭后焦点返回触发行。
- 标题清晰。
- 内容可独立滚动。
- 窄屏全屏。

## 18.2 ActionMenu

每行只显示一个 `…`：

```text
查看详情
刷新官方额度
请求重新授权
复制邮箱
────────
移入待命
归档
```

危险操作在底部，不与日常操作混排。

## 18.3 Dialog

用于：

- 归档确认。
- 踢出/轮转影响确认。
- 修改高风险策略。
- 不可逆或计费风险操作。

要求：

- 有明确标题。
- 文案写对象和后果。
- 初始焦点优先放在取消或最低风险按钮。
- Tab 焦点限制在 Dialog 内。
- Escape 关闭。
- 关闭后焦点返回触发点。
- 移动端可全屏。

---

## 19. 视觉 Token 微调

当前色彩可保留，新增结构 Token：

```css
:root {
  --content-max: 1280px;
  --settings-max: 1200px;
  --readable-max: 760px;
  --sheet-w: 520px;

  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 24px;
  --space-6: 32px;

  --control-h: 36px;
  --control-h-comfortable: 40px;
  --table-row-h: 46px;

  --focus-ring: 0 0 0 3px rgba(37, 99, 235, .22);
}
```

### 19.1 页面宽度

- 表格页：允许接近全宽，最大 1280～1440px。
- 设置页：最大约 1200px，避免输入框拉到 800px。
- 长解释文字：最大 680～760px。
- 不要让所有页面机械使用同一个 max-width。

### 19.2 页面标题

当前 Topbar 标题 15px 偏弱。建议：

```css
.topbar h1 {
  font-size: 16px;
  font-weight: 650;
}

.page-heading h2 {
  font-size: 20px;
}
```

若 Topbar 已承担唯一页面标题，则不必再重复 h2。可以把 Topbar h1 提到 18px，并保持简洁。

### 19.3 输入框

- 高度 38～40px。
- Label 12～13px。
- Hint 12px。
- 错误文本 12px。
- Focus 必须清晰。
- Disabled 状态要与只读状态区分。
- 密钥字段可加“已保存/未设置”状态，不要塞假值。

### 19.4 边框与圆角

继续使用：

- 常规圆角 6～8px。
- Sheet/Dialog 10～12px。
- 1px 边框。
- 只有 Sheet、Popover、Dialog 使用轻阴影。

明确不采用：

- 20px 大圆角。
- 玻璃拟态。
- 渐变光晕。
- 卡片悬浮抬升。
- 超大彩色数字。
- 彩色背景铺满状态卡。

---

## 20. 响应式规则

### ≥ 1280px

- 232px 左侧导航。
- 设置双列。
- 表格全列展示。
- Sheet 520px。

### 1040～1279px

- 设置仍可双列，但右列最小 320px。
- 页面 padding 缩小到 20px。
- 次要表格列可隐藏或进入详情。
- Sheet 480px。

### 720～1039px

- 设置改单列。
- 侧栏可折叠。
- Toolbar 可换行。
- 表格水平滚动。
- Sheet 宽度 `min(520px, 100vw)`。

### < 720px

- 侧栏改为抽屉或底部导航。
- Sheet 全屏。
- Topbar 只保留页面名和任务入口。
- 表格保留最关键 3～4 列。
- 其他字段在行详情中展示。
- 保存条固定底部，但不能遮挡最后一个输入框。

### 横向溢出规则

- 页面主体不得出现整体横向滚动。
- 表格自身滚动。
- URL、邮箱可截断，但 tooltip/复制能力可用。
- 完整技术 ID 只在 Sheet。
- 按钮文字不强行挤成两行。

---

## 21. 可访问性与键盘

### 21.1 Focus

全局增加：

```css
:where(
  a,
  button,
  input,
  select,
  textarea,
  [tabindex]:not([tabindex="-1"])
):focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}
```

不要只靠浏览器默认焦点，也不要完全移除 outline 而不替代。

### 21.2 点击目标

- 所有独立按钮至少 24×24 CSS px。
- 常用按钮与图标按钮推荐 32px。
- `…` 菜单按钮不能只让三个点本身可点。
- Checkbox 行可以整行作为 label 点击。

### 21.3 表单

- 每个 input 有真实 label。
- Hint 用 `aria-describedby` 关联。
- 错误文本与 input 关联。
- 错误摘要使用 `role="alert"`。
- 密码修改的三字段放在语义清晰的 fieldset。

### 21.4 Dialog/Sheet

- 有 `role="dialog"` 或使用原生 `<dialog>`。
- 有 `aria-labelledby`。
- Modal 才标 `aria-modal="true"`。
- 打开后移动焦点。
- Modal 内 Tab 循环。
- Escape 关闭。
- 关闭后返回触发点。

### 21.5 表格

- 表头 `scope="col"`。
- 交互行不要用不可聚焦的 click-only `<tr>`。
- 可以在第一列放详情 link，或让行带可聚焦控制。
- 状态同时有文字，不只颜色。
- 排序状态可被读屏识别。

### 21.6 动效

```css
@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    scroll-behavior: auto !important;
    animation-duration: .01ms !important;
    transition-duration: .01ms !important;
  }
}
```

---

## 22. 数据合同建议

## 22.1 设置密钥状态

当前不应继续通过固定 `••••••` 推断密钥存在。

推荐返回：

```json
{
  "connections": {
    "sub2api": {
      "configured": true,
      "auth_mode": "api_key",
      "base_url": "http://sub2api-canary:8080"
    },
    "hme": {
      "configured": true,
      "base_url": "http://icloud-hme:8081",
      "account_id": "acc_..."
    },
    "mail": {
      "configured": true,
      "base_url": "https://...",
      "address": "..."
    }
  },
  "secret_state": {
    "sub2api_api_key": "stored",
    "sub2api_admin_password": "missing",
    "hme_service_token": "stored",
    "cf_mail_admin_password": "stored"
  }
}
```

允许值：

```text
stored
missing
```

前端 input 保持空值。

## 22.2 检测结果

推荐统一：

```json
{
  "service": "sub2api",
  "ok": true,
  "checked_at": "2026-09-01T12:34:56Z",
  "latency_ms": 183,
  "summary": {
    "group_count": 3,
    "account_count": 42
  },
  "error": null
}
```

失败：

```json
{
  "service": "sub2api",
  "ok": false,
  "checked_at": "2026-09-01T12:34:56Z",
  "latency_ms": 5000,
  "summary": null,
  "error": {
    "code": "auth_failed",
    "message": "认证失败，请检查 Key 或管理员账号"
  }
}
```

页面不显示原始外部响应。

## 22.3 总览

建议补充聚合字段：

```json
{
  "summary": {
    "workspaces": 6,
    "accounts": 31,
    "attention": 3,
    "running_operations": 1,
    "identity_conflicts": 1
  },
  "attention": [],
  "running_operations": [],
  "workspace_health": [],
  "freshness": {
    "official_quota_latest_at": "...",
    "identity_audit_at": "...",
    "resources_checked_at": "..."
  }
}
```

## 22.4 列表数据

后端尽量直接提供 UI 能稳定使用的状态：

```json
{
  "status": {
    "code": "manual_required",
    "label": "需要人工",
    "tone": "danger",
    "reason": "OAuth Session 已失效"
  }
}
```

不要让模板或 JS 到处维护重复状态映射。

---

## 23. 文件级修改建议

## 23.1 `app/web/templates/console.html`

P0：

- 设置页改为 `settings-columns` + 两个 `settings-column`。
- Sub2API/HME/临时邮箱拆卡片。
- 密码表单独立。
- 所有表格增加 `.table-scroll` wrapper。
- 初始 tbody 文案由“还没有”改为“正在加载”。
- 为页面错误增加容器。
- 若任务抽屉暂未实现，隐藏顶部任务按钮。
- 表头添加 `scope="col"`。
- 页面 subtitle 改为 route 提供。

P1：

- 总览增加 summary strip、running operations、workspace health 容器。
- 行增加详情入口与 `…`。
- 增加统一 Sheet 容器。
- 资源页改 Tabs。

## 23.2 `app/web/static/css/components.css`

P0：

- 删除旧 `.settings-grid` 自动四卡布局。
- 加 `.settings-page`、`.settings-columns`、`.settings-column`。
- 加响应式断点。
- 加 sticky save bar。
- 加 loading/error/empty/no-results 状态。
- 加 table scroll。
- 加真实 focus/hover/disabled 状态。
- 修 Panel 内长 URL 断行。
- 防止 Grid/Flex 子项因 `min-width:auto` 溢出。

P1：

- Status、Badge、Summary Strip。
- Sheet、ActionMenu、Toast。
- Skeleton。
- Tabs。
- Relative time 的次要样式。

## 23.3 `app/web/static/css/app.css`

P0：

- 页面宽度策略。
- Topbar 标题层级。
- 输入框 38～40px。
- `focus-visible`。
- Reduced motion。
- 窄屏侧栏和 page padding。
- `tabular-nums` utility。

## 23.4 `app/web/static/js/app.js`

P0：

- 绑定搜索，或在未实现时移除搜索 UI。
- 为 bootPage 添加可见 loading/error。
- 保存期间禁用按钮。
- 实现 dirty state。
- 离页未保存提示。
- 任务抽屉实现摘要，或先隐藏。
- 日期格式化。
- No-data / no-results 分开。
- Probe 结果写入对应服务卡。
- Escape 关闭顺序明确。

P1：

- 行打开 Sheet。
- Sheet focus return。
- ActionMenu。
- active Operation 自适应轮询。
- URL query 筛选。
- 全局实体搜索。

## 23.5 `app/web/routes/pages.py`

- 将 `PAGES` 扩展为包含 subtitle 和 nav group。
- 最终将三个资源入口合并。
- 不在模板里写大量 page 分支文案。

## 23.6 `app/application/settings.py`

- 不再无条件返回假掩码。
- 返回每个 secret 的 stored/missing。
- 返回 mail configured。
- 可选持久化最近检测时间，但不要持久化敏感检测输入。
- 普通设置与密码修改可以拆成两个 command/endpoint。

## 23.7 `app/application/queries/console.py`

- 总览真正提供 UI 所需摘要。
- 工作区显示席位占用/上限。
- 账号表提供 5h/7d 与 freshness。
- 任务列表不加载完整 steps。
- 所有页面 query 保持不调用外部 API。

---

## 24. 分阶段实施计划

## Phase UI-0：只修失真和空白

提交建议：

```text
fix(ui): remove settings grid whitespace and false controls
```

内容：

- 设置页双列独立栈。
- 密码区域独立。
- 响应式单列。
- 搜索实现或隐藏。
- 任务抽屉实现或隐藏。
- Loading/empty/error 分离。
- 保存按钮防重复提交。
- 页面副标题。

不做：

- 不改数据库结构。
- 不改自动化策略。
- 不执行真实踢拉。
- 不部署。
- 不引入新前端框架。

## Phase UI-1：让关键参数可见

提交建议：

```text
feat(ui): surface operational summaries and connection health
```

内容：

- 设置服务状态卡。
- 已配置/未配置。
- 分服务检测与时间。
- 总览 summary strip。
- 运行任务。
- 工作区席位。
- 账号 5h/7d、Binding、授权摘要。
- 相对时间。

## Phase UI-2：详情与操作路径

提交建议：

```text
feat(ui): add entity sheets and row action menus
```

内容：

- 账号 Sheet。
- 工作区 Sheet。
- 任务 Sheet。
- `…` ActionMenu。
- 危险操作确认。
- Focus 管理。
- 局部刷新。

## Phase UI-3：导航与资源收束

提交建议：

```text
refactor(ui): consolidate resources and persist table views
```

内容：

- 资源 Tabs。
- 六项导航。
- URL query 筛选。
- 分页。
- 命令菜单实体搜索。
- 视图状态持久化。

## Phase UI-4：回归与可访问性

提交建议：

```text
test(ui): cover responsive states, forms, sheets and async feedback
```

内容：

- Playwright 页面导航。
- 搜索与筛选。
- 保存状态。
- 错误定位。
- Sheet 焦点恢复。
- Escape。
- 390/768/1024/1440 宽度截图。
- Axe serious/critical。
- 密钥不回显。
- 未实现控件检查。

---

## 25. P0 可直接交给 Codex 的执行清单

```text
目标仓库：hixz12d/48team-manager
基线：main@e67e25288231fa4e03e08a61644c303b0199667d

只做 UI-0，不改自动化业务，不部署。

1. 阅读 README.md、AGENTS.md、VPS.md。
2. 修改 console.html：
   - settings-grid 改成双独立 column stack。
   - Sub2API/HME/临时邮箱分卡片，但字段名与 API payload 不变。
   - 后台密码改为独立视觉区域；若暂不拆 endpoint，至少不能让大 Grid 继续制造空白。
   - tbody 初始状态改为“正在加载…”。
   - 增加页面级错误容器。
   - 为 table 增加横向滚动 wrapper。
3. 修改 components.css：
   - 新 settings-columns/settings-column。
   - max-width 1200px。
   - 1040px 以下单列。
   - sticky settings-actions。
   - loading/error/empty/no-results 样式。
4. 修改 app.css：
   - focus-visible。
   - reduced-motion。
   - 窄屏 page padding。
5. 修改 app.js：
   - 保存期间按钮 disabled。
   - 显示保存中/成功/失败。
   - 请求失败不能只 console.warn。
   - 搜索框必须可用；若本提交不实现则从模板隐藏。
   - 任务抽屉必须有内容；若本提交不实现则隐藏入口。
6. 不返回或打印任何 secret。
7. 不触发真实踢人、拉人、轮转、OpenAI billing/seat 操作。
8. 加测试，确认 settings 页 1720×960、1280×800、1024×768 下没有 Grid 造成的大空洞。
9. 单独 commit，不部署。
```

---

## 26. 验收标准

### 26.1 设置页

- [ ] 1720×960 下，自动化、手机号池连续排列，没有由左侧卡片高度制造的大空白。
- [ ] 1040px 以下自动变为单列。
- [ ] 输入框不会超出 Panel。
- [ ] Sub2API/HME/临时邮箱边界清晰。
- [ ] Secret 不回显。
- [ ] 用户能看出已配置还是未设置。
- [ ] 连接检测有独立 loading/success/error。
- [ ] 检测不会自动保存。
- [ ] 保存时按钮不可重复点击。
- [ ] 未保存修改清晰可见。
- [ ] 密码修改与普通设置风险边界清晰。
- [ ] 错误定位到字段。

### 26.2 列表页

- [ ] 搜索输入有真实效果，或不存在。
- [ ] 任务入口有真实内容，或不存在。
- [ ] Loading、Empty、No results、Error 四种状态不同。
- [ ] 表格不推动整个页面横向滚动。
- [ ] 工作区席位显示 `当前 / 上限`。
- [ ] 账号页能快速看到授权、7d、Binding、状态。
- [ ] 时间显示相对值并可查看精确值。
- [ ] 未实现按钮不会以可点击样式出现。

### 26.3 总览

- [ ] 首页 10 秒内能判断待处理数与运行任务。
- [ ] 正常时不是一整块空白。
- [ ] 不使用五张超大 KPI 卡。
- [ ] 不显示原始错误 JSON。
- [ ] 不显示完整 UUID。
- [ ] 正常状态不过度染色。

### 26.4 可访问性

- [ ] Tab 可以访问所有功能。
- [ ] Focus 清晰可见。
- [ ] Escape 能关闭顶层 Overlay。
- [ ] Dialog/Sheet 关闭后焦点返回触发点。
- [ ] 独立点击目标至少 24×24px，常用控件优先 32px。
- [ ] 状态不只靠颜色。
- [ ] 表单错误有摘要和字段错误。
- [ ] `prefers-reduced-motion` 生效。

### 26.5 安全

- [ ] HTML/JSON 不包含原始 Token、API Key、密码或代理认证。
- [ ] Operation input 不进入任务详情。
- [ ] 危险操作有明确后果说明。
- [ ] 前端状态不能绕过后端 identity/vacancy 门闩。
- [ ] UI 改动不改变自动化默认开关。

---

## 27. 不应做的事情

- 不要为了填空白硬塞统计卡。
- 不要用 CSS masonry 或 multi-column 排表单。
- 不要只加 `align-items:start` 就认为修好了 Grid。
- 不要为每个状态做彩色胶囊。
- 不要把完整错误 JSON 展开在主页面。
- 不要把所有操作永久显示在每一行。
- 不要把 Secret 的“空”和“已存”都显示为同一串圆点。
- 不要保留无行为的搜索框和抽屉。
- 不要用页面刷新代替实体级更新。
- 不要在本轮引入 React、Vue、Node 构建链。
- 不要顺手重写成熟 Playwright 流程。
- 不要改 `/opt/sub2api` 或其资源。
- 不要部署，除非单独获得明确批准。

---

## 28. 研究来源与本项目转化

### Linear

借鉴点：

- 重新平衡 sidebar、header、panel，降低视觉噪音。
- 在保留高信息密度的同时，避免所有元素争抢注意力。
- 操作位置保持可预测。
- 主内容获得主要视觉权重。

来源：

- [How we redesigned the Linear UI (part II)](https://linear.app/now/how-we-redesigned-the-linear-ui)
- [A calmer interface for a product in motion](https://linear.app/now/behind-the-latest-design-refresh)

### GitHub Primer

借鉴点：

- 表单按稳定模式提交与保存。
- 保存结果要快速、明确、可信。
- 多个行操作进入 ActionMenu。
- 页面按内容区域、侧栏、详情上下文建立稳定布局。

来源：

- [Primer — Forms](https://primer.style/product/ui-patterns/forms/)
- [Primer — Saving](https://primer.style/product/ui-patterns/saving/)
- [Primer — ActionMenu](https://primer.style/product/components/action-menu/)
- [Primer — Layout](https://primer.style/product/getting-started/foundations/layout/)

### IBM Carbon

借鉴点：

- 搜索、筛选、主操作放在统一 table toolbar。
- DataTable 负责高密度信息，不用卡片墙替代。
- Loading、No data、No results、Error 要有不同状态。
- 紧凑表格配紧凑 toolbar。

来源：

- [Carbon — Data table usage](https://carbondesignsystem.com/components/data-table/usage/)
- [Carbon — Data table style](https://carbondesignsystem.com/components/data-table/style/)
- [Carbon — Empty states](https://v10.carbondesignsystem.com/patterns/empty-states-pattern/)
- [Carbon — Loading](https://v10.carbondesignsystem.com/patterns/loading-pattern/)
- [Carbon — Search](https://v10.carbondesignsystem.com/patterns/search-pattern/)

### Vercel Geist

借鉴点：

- Sheet 用于保留主列表上下文的详情查看。
- Command Menu 用于全局、键盘优先的导航和资源查找。
- Badge 只作为短、可扫读的元数据，不把整页变成标签墙。

来源：

- [Geist — Sheet](https://vercel.com/geist/sheet)
- [Geist — Command Menu](https://vercel.com/geist/command-menu)
- [Geist — Badge](https://vercel.com/geist/badge)

### Nielsen Norman Group

借鉴点：

- 表单通过结构、透明、清晰和支持降低认知负担。
- 高级或低频内容渐进披露。
- 空状态必须说明系统状态，并给出下一步。
- 不要在加载时先错误地宣称“没有记录”。

来源：

- [4 Principles to Reduce Cognitive Load in Forms](https://www.nngroup.com/articles/4-principles-reduce-cognitive-load/)
- [Progressive Disclosure](https://www.nngroup.com/articles/progressive-disclosure/)
- [Designing Empty States in Complex Applications](https://www.nngroup.com/articles/empty-state-interface-design/)

### W3C 与 GOV.UK

借鉴点：

- Modal 打开后移动焦点，Tab 限制在内部，Escape 关闭，关闭后返回触发点。
- 指针目标至少 24×24 CSS px，重要控件更大。
- 表单错误同时显示错误摘要与字段级错误。

来源：

- [WAI-ARIA — Dialog Modal Pattern](https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/)
- [WCAG 2.2 — Target Size Minimum](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html)
- [GOV.UK — Error summary](https://design-system.service.gov.uk/components/error-summary/)

---

## 29. 最终设计判断

当前截图里最不舒服的地方，不是“白色太多”，而是：

- 空白没有承担分组作用。
- 右侧信息密度断裂。
- 页面看起来像有一半功能未完成。
- 左侧是长字段录入，右侧却只有一个 checkbox，重要性比例失真。

最终不应该通过“往右边塞东西”来解决，而应该通过：

1. 两个独立纵向栈消除 Grid 行锁定。
2. 把集成按服务拆开。
3. 把连接状态、关键计数和新鲜度放在卡片头部。
4. 把不常改的字段后置到“编辑配置”。
5. 把保存、错误、检测状态表达清楚。
6. 让任何控件都不欺骗用户。
7. 在总览和表格中展示判断所需参数，把技术细节放到 Sheet。

这会保留当前“简约、冷静、像工具”的味道，同时显著提高人性化、可管理性和可信度。
