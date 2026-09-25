# 注册扩展 0.4.1 验证记录

本次修改：CDP 分段滚轮、原生日期/单选下拉键盘操作、密码整段文本插入、Tab/回车、表单观察等待、偶发页面指针移动、隐藏网页卡片与弹窗暂停、焦点模拟调用去重及中断检查。

## 验证结果

- `node --check`：background.js、content.js、popup.js 通过。
- `node --test tests/signup_extension.test.cjs`：25 项通过。
- `tests.browser_signup_flow`：9 个完整模拟流程通过；新增失焦校验流程另行通过，共 10 种。提交前增加 Tab 失焦后，对受影响的 6 个自动流程再次验证，全通过。
- `tests.browser_signup_extension tests.browser_managed_signup tests.test_managed_signup`：共 98 项通过。
- 检查实际弹窗截图：隐藏选项、暂停按钮显示正常，选项保存/手工模式禁用有浏览器断言。
- 安装包完整性、27 个源码/ZIP/解压目录文件的字节一致性通过，版本为 0.4.1。
- 已删除用户授权清理的 0.3.9 安装包，保留 0.4.0 包。

## 关键覆盖

- 浏览器实际年月日分段顺序，日期最终值一致。
- 年月日下拉框的键盘选择与输入事件可信性。
- 密码只产生一次整段输入；不操作系统剪贴板。
- 滚轮实际触发、隐藏卡片时弹窗暂停/继续、强制覆盖 Tab/回车分支。
- 密码 change/blur 校验后才允许提交的表单。
- 暂停/超时拒绝输入；移动过程中暂停不再按下；Shift 按下后暂停会释放。
- 现有防重复提交、手工模式、旧合成输入及托管流程回归。

## 范围

测试仅运行本地模拟页面和虚构邮箱配置；未创建真实账号，未测试真实 ChatGPT 注册、HubStudio 的遮挡/最小化和启动参数效果。CDP 的 isTrusted 不证明实体用户操作。

CDP 模式下原生日期/下拉选择结果不一致会暂停；无法建立 CDP 连接时仍走已有合成事件兼容路径。

详细日志保存在 dist/signup-041-flow.log、dist/signup-041-auto-recheck.log、dist/signup-041-regression.log。
