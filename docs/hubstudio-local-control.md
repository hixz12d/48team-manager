# 本机 HubStudio 原版插件接入验证

2026-09-19 用户确认其成功注册发生在当前 Windows 的 HubStudio ChroBrowser 无痕窗口，代理出口和授权链接来源与项目一致。此轮只验证控制能力，未注册、领HME、申请OAuth或变更团队成员。

## 已确认

- HubStudio 安装路径 `C:\Application\Hubstudio\Hubstudio.exe`，运行中的浏览器内核可通过本机回环 CDP 连接，无需另起 Playwright Chromium。
- 实际验证内核 `Chrome/150.0.7871.123`。
- 原版扩展 `Team48 ChatGPT 注册助手` 0.3.6，扩展ID `nigimejknaadfcbpegojdnajfpomegcg`。
- 已验证一套启用无痕权限且正在运行 split 无痕后台的环境；普通环境与其它窗口未修改。不能仅凭版本号认定其与用户某次成功记录完全一致，正式对照需核对对应环境。
- 通过扩展浏览器API `chrome.windows.create({incognito:true,url:'about:blank',focused:false})` 创建临时原生无痕窗口。
- 在临时窗口加载原扩展 `popup.html`，确认文档加载、`chrome.extension.inIncognitoContext=true`、版本及邮箱输入框/开始注册按钮可读取。使用的是原扩展页面和后台，不是将扩展脚本复制注入注册页面。
- 只读取界面和任务是否活动，未读输出密码/Cookie/OTP，未点击开始。最后仅关闭本次创建窗口，保留既有窗口。
- 证据 `dist/hubstudio-probe.json`。临时窗口已关闭、`signup_started=false`、`smoke_passed=true`。

## 重用

`python scripts/hubstudio_probe.py --port <当前环境CDP端口>` 只读检查。附加 `--smoke --output dist/hubstudio-probe.json` 执行上述空白窗口测试。使用项目已有Python环境和websockets；不新增依赖。端口随环境重启变化，本轮曾观察到连接重启及端口变化，必须先重新识别现有进程和端口，不能将示例端口长期固定。

脚本仅允许回环调试地址，不用 `Target.createBrowserContext` 模拟无痕，不调用 Browser.close。烟测要求唯一匹配的、无活动任务的原版无痕后台。若扩展弹窗页不唯一则停止，临时窗口在finally中关闭。

## 尚未验证

- 本轮以标签页加载扩展弹窗页面，未验证自动展开工具栏原生弹窗。尚未自动填写或点击注册开始，也未执行真实站点流程。
- 同一环境的多个无痕窗口共用无痕会话；新建窗口不是清空会话。不能擅自关闭用户既有无痕窗口或清除Cookie。全新注册对照需要明确空闲且无旧无痕会话的目标环境。
- 仍需确认用户实际使用的工作空间选择、标签页切换、授权链接打开顺序；用这些步骤建立基准，再逐步自动化。
- CDP接管、原生无痕、原版插件并不证明全部行为与物理键鼠一致，也不保证OpenAI手机验证结果相同。当前证据只证明本机这条控制路线可用。
- Jupiter 新子账号305仍已入组但OAuth需要手机，不重复注册；没有在本轮操作其凭据或授权。
