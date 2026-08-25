# 母号 / 席位术语

这是自用轮转工厂，不是客户席位后台。下面这些词不要混用。

## 身份

- **母号 / Team**：有管理权限的 Workspace 登录身份。`Team` 表是操作单元。
- **子号 / ChildAccount**：被拉进 Team 的邮箱资产。踢人不删记录，进入 `standby` 后可再拉。撤回未加入的邀请回到 `unused`，不要标 standby。入组后的 `account_id` 必须是母号 workspace UUID，不是 `user-xxx`。
- **成员关系**：上游当前已加入或待接受邀请的人。不等于子号资产，也不等于计费席位。

## 四个数字

- **本地占用** `current_members`：已加入 + 待接受邀请。用来做兑换和轮转操作。
- **操作上限** `max_members`：本系统自己设的拉人上限。默认 6 只是导入配置，不是上游订阅容量。
- **上游占用**：当前成员列表 + 待处理邀请。只在席位页能读到实时名单时展示。
- **账单数量**：OpenAI 这期实际在收的钱。系统看不到就标未知，不要用 6 去填。

踢掉一个人 ≠ 计费席位已释放。新邀请也可能按比例加钱。

## 踢人回执

`policy_notice` / `billing_notice` 没有稳定公开语义，只当排查证据。

- `ordinal < threshold` 且没有 `billing_notice`：才允许自动补位。
- `policy_notice` 为 null、字段缺失、或带回执账单：显示未知 / 有风险，停止自动 1 踢 1 拉。
- 核对 Billing 后，可以勾选强制补位。

## 凭证

- **Access Token** 过期：先用 RT / Session Token 换票，不要当成母号封禁。
- **RT / Session Token** 失效：才把母号标成过期或封禁。
